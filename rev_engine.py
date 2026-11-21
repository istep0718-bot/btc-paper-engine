#!/usr/bin/env python3
"""
REV ENGINE V1 -- reversal shadow measurement (MEASUREMENT ONLY).

Purpose
-------
Capture the *initial move* of violent reversals that the house
trend-following setups (A / B / C / D / E / F) structurally cannot
take: a sharp rebound long inside a downtrend (REV-L) and a sharp
drop short inside an uptrend (REV-S). REV is a completely
independent shadow engine:

  * It NEVER opens a real paper position, never touches the house
    equity / risk state / daily stats, and never influences any
    house entry or exit. It reads only the candles and market data
    the house cycle already fetched, and writes only state["rev"].
  * Its virtual book uses its own virtual equity (snapshotted from
    the house equity once, at first initialisation) so REV results
    are never mixed up with the house sizing problem.
  * All failures are swallowed by update_rev(); the caller also
    wraps the call, so a REV failure can never break the house
    cycle. There is deliberately NO entry-stop / kill switch for
    REV (warnings only), per the owner's standing rule.

Design notes / decisions (REV ENGINE V1 spec, 2026-11-21)
---------------------------------------------------------
  * Timeframe: bot.fetch_candles supports only 15m / 1h / 4h
    (_INTERVAL_MINUTES has no "5m"), so the short-term structure
    shift is confirmed on 15m bars only. The spec allows "5m OR
    15m" for the structure shift, so 15m-only is spec-compliant
    and keeps the bot.py diff at zero extra API calls. If 5m
    candles are ever passed in, they are ignored in V1.
  * Only CLOSED candles are evaluated: bot.parse_candles already
    drops the unfinished candle, and REV additionally tracks
    last_bar_t so a bar is never evaluated twice.
  * OI: bot.fetch_market() already returns open_interest from
    metaAndAssetCtxs, so no new API call is needed. REV keeps its
    own short OI history inside state["rev"] (one sample per
    cycle) to measure the ~1h change. While that history is too
    short, the OI score component is treated as UNAVAILABLE and
    the total score is normalised over the available components
    (spec: safe handling of missing data).
  * Funding: already fetched by the house cycle (ctx["funding"]).
    No liquidation data source exists in the house fetcher, so
    liquidations are not used in V1 (spec: "if available").
  * OI interpretation: a sharp OI contraction is treated as
    capitulation / position flush for BOTH directions (longs
    flushed after a crash for REV-L; a leveraged flush after a
    spike top for REV-S). Funding is mirrored properly (REV-L
    wants funding < 0, REV-S wants funding > 0).
  * Persistence: the btc-paper workflow commits only state.json
    and docs/data.json, so the full REV ledger lives inside
    state["rev"] (capped) and a summary is exported into
    data.json["rev"] by the caller. A docs/rev_shadow.jsonl file
    would not survive the workflow's `git add` list, so it is
    intentionally not used.
  * Cost model mirrors the house paper model exactly (taker fee
    0.05%/side, base slippage 0.02%/side, +0.05% extra slippage
    on stop fills), duplicated here on purpose: REV imports
    nothing from bot.py, so house changes can never break it
    and REV can never break the house.
  * Sizing mirrors the house size_position(): risk fraction 5%
    of (virtual) equity, leverage 40x, final margin clamped into
    the 25%-50% of equity band. This is a virtual size used only
    to convert R multiples into virtual dollars.

REV-L (crash-reversal long) -- mandatory conditions
---------------------------------------------------
  1. Impulse: within the last 4 bars (peak before trough), price
     fell >= 1.8% OR the peak-to-trough width is >= 1.8 x ATR(14).
  2. Selling-pressure peak: a down bar in the impulse window with
     volume >= 1.5x the prior-20-bar mean (climax), AND the
     latest bar shows deceleration (body shrinkage, a lower wick
     >= 20% of the bar range, or a close >= 0.25 x ATR off the low).
  3. Short-term structure shift (15m): the latest bar does not
     make a meaningful new low versus the prior 5 bars and closes
     above the previous bar's high with a bullish close.
  Upper timeframes (1h / 4h) being in a downtrend does NOT block
  REV-L. REV-S is the exact mirror image.

Score (100 points, normalised over available components)
--------------------------------------------------------
  pressure peak 20 / structure shift 25 / volume 15 /
  wick & price rejection 15 / RSI(7) extreme 10 / OI 10 /
  funding 5. >= 65 normalised -> alpha candidate (early entry
  on the shift bar). >= 75 normalised -> beta candidate; beta
  enters only after a confirmation bar (a later bar closing
  above the shift bar's high without breaking the extreme).
  Alpha and beta are booked as separate virtual trades tied to
  the same event_id.

Exits (initial spec)
--------------------
  +1.0R: close 25%. +1.5R: close 25%. Remaining 50% trails:
  trail activates at +1.0R (by close), width 1.5 x ATR at entry,
  ratcheted by bar closes and applied from the next bar. Initial
  stop: beyond the reversal extreme by 0.15 x ATR. After 8 bars
  the remainder is closed at the bar close (TIME_EXPIRE). If a
  bar touches both the stop and a take-profit, the stop is
  assumed to fill first (conservative).

Events that meet the impulse + pressure conditions but never
fire (no structure shift, or score too low, or beta never
confirmed) are recorded as UNFILLED events with the reason and
their subsequent 16-bar MFE / MAE, so missed moves can be
analysed later.

Standard library only.
"""

import copy
import math
from datetime import datetime, timezone

REV_VERSION = "REV_ENGINE_V1_2026-11-21"

# ---- cost / sizing model (mirrors bot.py paper model) ----
LEVERAGE = 40.0
FEE_RATE = 0.0005             # taker fee per side
SLIPPAGE_RATE = 0.0002        # base slippage per side
STOP_EXTRA_SLIPPAGE = 0.0005  # extra adverse slippage on stop fills
ROUND_TRIP_COST_RATE = (
    2 * FEE_RATE + 2 * SLIPPAGE_RATE + STOP_EXTRA_SLIPPAGE
)
RISK_PER_TRADE = 0.05
MIN_MARGIN_FRACTION = 0.25
MAX_MARGIN_FRACTION = 0.50

# ---- REV parameters (fixed for V1; do not re-tune silently) ----
IMPULSE_BARS = 4              # peak-to-trough window (bars)
IMPULSE_PCT = 0.018           # >= 1.8% impulse move
IMPULSE_ATR_MULT = 1.8        # or >= 1.8 x ATR(14) width
ATR_PERIOD = 14
RSI_PERIOD = 7
RSI_EXTREME_LONG = 20.0       # RSI(7) <= 20 is the long extreme
RSI_EXTREME_SHORT = 80.0      # RSI(7) >= 80 is the short extreme
VOL_PERIOD = 20               # volume baseline (prior bars)
VOL_CLIMAX = 1.5              # climax volume multiple
WICK_FULL = 0.35              # wick ratio for full wick points
SCORE_ALPHA = 65.0
SCORE_BETA = 75.0
BETA_CONFIRM_BARS = 4         # confirmation must arrive within 4 bars
EVENT_MAX_BARS = 16           # event tracking horizon
TP1_R = 1.0
TP1_FRAC = 0.25
TP2_R = 1.5
TP2_FRAC = 0.25
TRAIL_START_R = 1.0
TRAIL_ATR_MULT = 1.5
STOP_EXTREME_ATR = 0.15
TIME_BARS = 8                 # time-expiry evaluation after 8 bars
MAX_STOP_ATR = 4.0            # data-sanity guard on stop distance
OI_HISTORY_MAX = 64           # samples kept (1 per cycle)
OI_MIN_SAMPLES = 5            # samples needed before OI is scored
OI_DROP_FULL = -0.03          # ~1h OI change for full OI points
OI_DROP_MID = -0.015
LEDGER_LIMIT = 1000
CURVE_LIMIT = 2000

_SCORE_MAX = {
    "pressure": 20.0,
    "structure": 25.0,
    "volume": 15.0,
    "wick": 15.0,
    "rsi": 10.0,
    "oi": 10.0,
    "funding": 5.0,
}

_BUCKETS = (
    "ALL", "LONG", "SHORT", "ALPHA", "BETA",
    "LONG_ALPHA", "LONG_BETA", "SHORT_ALPHA", "SHORT_BETA",
)


# ============================================================
# small numeric helpers (self-contained; nothing imported
# from bot.py on purpose)
# ============================================================

def _f(v, default=0.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _iso(ts_seconds):
    return datetime.fromtimestamp(
        int(ts_seconds), tz=timezone.utc
    ).isoformat()


def _atr_series(candles, period=ATR_PERIOD):
    """Wilder ATR over candle dicts; returns list aligned to candles
    (None until enough bars)."""
    n = len(candles)
    out = [None] * n
    if n == 0:
        return out
    trs = []
    for idx, cd in enumerate(candles):
        h, l, c = cd["h"], cd["l"], cd["c"]
        if idx == 0:
            trs.append(h - l)
        else:
            pc = candles[idx - 1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if n <= period:
        return out
    atr_val = sum(trs[1:period + 1]) / period
    out[period] = atr_val
    for idx in range(period + 1, n):
        atr_val = (atr_val * (period - 1) + trs[idx]) / period
        out[idx] = atr_val
    return out


def _rsi_series(closes, period=RSI_PERIOD):
    """Wilder RSI; list aligned to closes (None until seeded)."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains, losses = [], []
    for idx in range(1, n):
        d = closes[idx] - closes[idx - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def _rsi(ag, al):
        if al <= 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_g, avg_l)
    for idx in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[idx]) / period
        avg_l = (avg_l * (period - 1) + losses[idx]) / period
        out[idx + 1] = _rsi(avg_g, avg_l)
    return out


def _vol_baseline(volumes, idx, period=VOL_PERIOD):
    """Mean volume of the `period` bars BEFORE idx (None if short)."""
    if idx < period:
        return None
    window = volumes[idx - period:idx]
    if not window:
        return None
    return sum(window) / len(window)


def _size_position(equity, entry, stop):
    """Mirror of the house size_position(): risk-based quantity
    with the final margin clamped into the 25%-50% band."""
    distance = abs(entry - stop)
    if equity <= 0 or distance <= 0 or entry <= 0:
        return None
    cost_per_unit = distance + entry * ROUND_TRIP_COST_RATE
    if cost_per_unit <= 0:
        return None
    risk_qty = (equity * RISK_PER_TRADE) / cost_per_unit
    max_q = equity * MAX_MARGIN_FRACTION * LEVERAGE / entry
    min_q = equity * MIN_MARGIN_FRACTION * LEVERAGE / entry
    qty = max(min(risk_qty, max_q), min_q)
    if qty <= 0:
        return None
    return {
        "quantity": qty,
        "notional": qty * entry,
        "margin": qty * entry / LEVERAGE,
        "risk_usd": qty * cost_per_unit,
    }


# ============================================================
# state
# ============================================================

def _new_bucket():
    return {
        "trades": 0, "wins": 0, "losses": 0,
        "gross_win_usd": 0.0, "gross_loss_usd": 0.0,
        "total_r": 0.0, "pnl_curve": [],
    }


def _new_rev_state(start_equity):
    return {
        "version": REV_VERSION,
        "enabled": True,
        "start_equity": float(start_equity),
        "virtual_equity": float(start_equity),
        "peak_equity": float(start_equity),
        "last_bar_t": 0,
        "oi_history": [],
        "active_events": {"LONG": None, "SHORT": None},
        "open_positions": [],
        "ledger": [],
        "stats": {b: _new_bucket() for b in _BUCKETS},
        "equity_curve": [],
        "events_total": 0,
        "unfilled_total": 0,
        "last_error": None,
        "updated_at": None,
    }


def _ensure_rev(state):
    rev = state.get("rev")
    if not isinstance(rev, dict) or "stats" not in rev:
        start = _f(state.get("equity"), 100.0)
        if start <= 0:
            start = 100.0
        rev = _new_rev_state(start)
        state["rev"] = rev
    return rev


# ============================================================
# signal evaluation (single bar, single side)
# ============================================================

def _impulse(candles, idx, atr_val, side):
    """Peak-to-trough impulse inside the last IMPULSE_BARS bars.

    Returns dict with ok / peak / trough / width / pct, or None.
    For LONG the peak must come before the trough and the trough
    must be recent (within 2 bars of idx). SHORT is the mirror.
    """
    lo_i = max(0, idx - IMPULSE_BARS)
    window = candles[lo_i:idx + 1]
    if len(window) < 3 or atr_val is None or atr_val <= 0:
        return None
    if side == "LONG":
        peak_i = max(range(len(window)), key=lambda j: window[j]["h"])
        after = window[peak_i:]
        trough_i = min(range(len(after)), key=lambda j: after[j]["l"])
        peak = window[peak_i]["h"]
        trough = after[trough_i]["l"]
        trough_abs = lo_i + peak_i + trough_i
        width = peak - trough
        pct = (trough - peak) / peak if peak > 0 else 0.0
        ok = (pct <= -IMPULSE_PCT or width >= IMPULSE_ATR_MULT * atr_val)
    else:
        trough_i0 = min(range(len(window)), key=lambda j: window[j]["l"])
        after = window[trough_i0:]
        peak_i = max(range(len(after)), key=lambda j: after[j]["h"])
        trough = window[trough_i0]["l"]
        peak = after[peak_i]["h"]
        trough_abs = lo_i + trough_i0 + peak_i
        width = peak - trough
        pct = (peak - trough) / trough if trough > 0 else 0.0
        ok = (pct >= IMPULSE_PCT or width >= IMPULSE_ATR_MULT * atr_val)
    if idx - trough_abs > 2:
        ok = False
    return {
        "ok": bool(ok), "peak": peak, "trough": trough,
        "width": width, "pct": pct, "extreme": (
            trough if side == "LONG" else peak
        ),
        "extreme_idx": trough_abs,
    }


def _pressure(candles, idx, volumes, atr_val, side, imp):
    """Selling/buying pressure peak: climax bar + deceleration."""
    lo_i = max(0, imp["extreme_idx"] - 2)
    climax_mult = 0.0
    climax_found = False
    for j in range(lo_i, idx + 1):
        base = _vol_baseline(volumes, j)
        if base is None or base <= 0:
            continue
        cd = candles[j]
        is_pressure_bar = (
            cd["c"] < cd["o"] if side == "LONG" else cd["c"] > cd["o"]
        )
        if is_pressure_bar:
            mult = cd["v"] / base
            climax_mult = max(climax_mult, mult)
            if mult >= VOL_CLIMAX:
                climax_found = True
    cd = candles[idx]
    prev = candles[idx - 1] if idx >= 1 else cd
    body = abs(cd["c"] - cd["o"])
    prev_body = abs(prev["c"] - prev["o"])
    rng = max(cd["h"] - cd["l"], 1e-9)
    if side == "LONG":
        wick_ratio = (min(cd["o"], cd["c"]) - cd["l"]) / rng
        off_extreme = cd["c"] - cd["l"]
    else:
        wick_ratio = (cd["h"] - max(cd["o"], cd["c"])) / rng
        off_extreme = cd["h"] - cd["c"]
    decelerating = (
        body <= 0.5 * prev_body
        or wick_ratio >= 0.20
        or (atr_val is not None and off_extreme >= 0.25 * atr_val)
    )
    return {
        "ok": bool(climax_found and decelerating),
        "climax_mult": climax_mult,
        "decelerating": bool(decelerating),
        "wick_ratio": wick_ratio,
    }


def _structure_shift(candles, idx, atr_val, side):
    """15m short-term structure shift.

    LONG: no meaningful new low vs the prior 5 bars, bullish close
    above the previous bar's high. SHORT is the mirror.
    """
    if idx < 6 or atr_val is None:
        return False
    cd = candles[idx]
    prev = candles[idx - 1]
    tol = 0.05 * atr_val
    if side == "LONG":
        prior_low = min(x["l"] for x in candles[idx - 5:idx])
        return (
            cd["l"] >= prior_low - tol
            and cd["c"] > prev["h"]
            and cd["c"] > cd["o"]
        )
    prior_high = max(x["h"] for x in candles[idx - 5:idx])
    return (
        cd["h"] <= prior_high + tol
        and cd["c"] < prev["l"]
        and cd["c"] < cd["o"]
    )


def _score(candles, idx, volumes, atr_val, rsi_val, side,
           pressure, shift_ok, oi_change, funding):
    """Component scores; returns (breakdown, raw, available_max)."""
    bd = {}
    # pressure peak: 20
    mult = pressure["climax_mult"]
    if mult >= 2.0:
        bd["pressure"] = 20.0
    elif mult >= VOL_CLIMAX:
        bd["pressure"] = 14.0 + 6.0 * (mult - VOL_CLIMAX) / 0.5
    elif mult >= 1.0:
        bd["pressure"] = 6.0 + 8.0 * (mult - 1.0) / 0.5
    else:
        bd["pressure"] = 6.0 * max(mult, 0.0)
    if not pressure["decelerating"]:
        bd["pressure"] *= 0.5
    # structure shift: 25 (binary; also a mandatory condition)
    bd["structure"] = 25.0 if shift_ok else 0.0
    # volume of the signal bar: 15
    base = _vol_baseline(volumes, idx)
    if base is None or base <= 0:
        bd["volume"] = 0.0
        vol_ratio = None
    else:
        vol_ratio = candles[idx]["v"] / base
        if vol_ratio >= 1.5:
            bd["volume"] = 15.0
        elif vol_ratio <= 0.8:
            bd["volume"] = 0.0
        else:
            bd["volume"] = 15.0 * (vol_ratio - 0.8) / 0.7
    # wick & price rejection: 15 (wick up to 10 + failed new
    # extreme up to 5)
    wick_pts = 10.0 * min(pressure["wick_ratio"] / WICK_FULL, 1.0)
    if idx >= 10:
        if side == "LONG":
            prior_extreme = min(
                x["l"] for x in candles[idx - 10:idx]
            )
            failed = candles[idx]["l"] >= prior_extreme
        else:
            prior_extreme = max(
                x["h"] for x in candles[idx - 10:idx]
            )
            failed = candles[idx]["h"] <= prior_extreme
        if failed:
            wick_pts += 5.0
    bd["wick"] = min(wick_pts, 15.0)
    # RSI extreme: 10
    if rsi_val is None:
        bd["rsi"] = 0.0
    elif side == "LONG":
        if rsi_val <= RSI_EXTREME_LONG:
            bd["rsi"] = 10.0
        elif rsi_val < 30.0:
            bd["rsi"] = 10.0 * (30.0 - rsi_val) / 10.0
        else:
            bd["rsi"] = 0.0
    else:
        if rsi_val >= RSI_EXTREME_SHORT:
            bd["rsi"] = 10.0
        elif rsi_val > 70.0:
            bd["rsi"] = 10.0 * (rsi_val - 70.0) / 10.0
        else:
            bd["rsi"] = 0.0
    # OI: 10 (may be unavailable -> excluded from available max)
    available = dict(_SCORE_MAX)
    if oi_change is None:
        bd["oi"] = 0.0
        available.pop("oi")
    elif oi_change <= OI_DROP_FULL:
        bd["oi"] = 10.0
    elif oi_change <= OI_DROP_MID:
        bd["oi"] = 6.0
    elif oi_change < 0:
        bd["oi"] = 3.0
    else:
        bd["oi"] = 0.0
    # funding: 5
    if side == "LONG":
        bd["funding"] = 5.0 if funding < 0 else (2.0 if funding == 0 else 0.0)
    else:
        bd["funding"] = 5.0 if funding > 0 else (2.0 if funding == 0 else 0.0)
    raw = sum(bd.values())
    avail_max = sum(available.values())
    return bd, raw, avail_max, vol_ratio


def _evaluate_side(candles, idx, volumes, atr_s, rsi_s, side,
                   oi_change, funding):
    """Full single-bar evaluation for one side. Returns a dict
    describing impulse / pressure / shift / score, or None when
    there is not even an impulse (nothing to track)."""
    atr_val = atr_s[idx]
    if atr_val is None or atr_val <= 0:
        return None
    imp = _impulse(candles, idx, atr_val, side)
    if imp is None or not imp["ok"]:
        return None
    pressure = _pressure(candles, idx, volumes, atr_val, side, imp)
    if not pressure["ok"]:
        return {"impulse": imp, "pressure": pressure,
                "shift": False, "stage": "no_pressure"}
    shift_ok = _structure_shift(candles, idx, atr_val, side)
    bd, raw, avail_max, vol_ratio = _score(
        candles, idx, volumes, atr_val, rsi_s[idx], side,
        pressure, shift_ok, oi_change, funding,
    )
    norm = (raw / avail_max * 100.0) if avail_max > 0 else 0.0
    return {
        "impulse": imp, "pressure": pressure, "shift": shift_ok,
        "stage": "candidate" if shift_ok else "no_shift",
        "breakdown": bd, "raw_score": raw,
        "available_max": avail_max, "score": norm,
        "vol_ratio": vol_ratio, "atr": atr_val,
        "rsi7": rsi_s[idx], "oi_change_1h": oi_change,
        "funding": funding,
    }


# ============================================================
# virtual positions
# ============================================================

def _fill_pnl(side, qty, entry, exit_px):
    """Net PnL of one fill, fee charged on the fill notional."""
    if side == "LONG":
        gross = qty * (exit_px - entry)
    else:
        gross = qty * (entry - exit_px)
    fee = qty * exit_px * FEE_RATE
    return gross - fee, fee


def _open_virtual(rev, event, mode, side, bar, atr_val, ev):
    """Open a virtual position at the signal bar's close
    (slippage-adjusted, same convention as the house)."""
    close = bar["c"]
    if side == "LONG":
        entry = close * (1 + SLIPPAGE_RATE)
        stop = event["extreme"] - STOP_EXTREME_ATR * atr_val
    else:
        entry = close * (1 - SLIPPAGE_RATE)
        stop = event["extreme"] + STOP_EXTREME_ATR * atr_val
    r_dist = abs(entry - stop)
    if r_dist <= 0 or r_dist > MAX_STOP_ATR * atr_val:
        event.setdefault("fire_failures", []).append({
            "mode": mode, "bar_t": bar["t"],
            "reason": "invalid_stop_distance",
        })
        return None
    sizing = _size_position(rev["virtual_equity"], entry, stop)
    if sizing is None:
        event.setdefault("fire_failures", []).append({
            "mode": mode, "bar_t": bar["t"], "reason": "sizing_failed",
        })
        return None
    qty = sizing["quantity"]
    entry_fee = qty * entry * FEE_RATE
    rev["virtual_equity"] -= entry_fee
    pos = {
        "event_id": event["event_id"],
        "mode": mode,
        "side": side,
        "entry": entry,
        "entry_bar_t": bar["t"],
        "initial_stop": stop,
        "trail_stop": None,
        "trail_active": False,
        "atr_at_entry": atr_val,
        "r_distance": r_dist,
        "risk_usd": sizing["risk_usd"],
        "quantity": qty,
        "qty_remaining": qty,
        "margin_used": sizing["margin"],
        "leverage": LEVERAGE,
        "tp1_done": False,
        "tp2_done": False,
        "hit_1r_t": None,
        "hit_15r_t": None,
        "bars_held": 0,
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "realized_pnl": -entry_fee,
        "fees_paid": entry_fee,
        "score": ev["score"],
        "score_raw": ev["raw_score"],
        "score_breakdown": ev["breakdown"],
        "trend_1h": ev.get("trend_1h"),
        "trend_4h": ev.get("trend_4h"),
        "rsi7": ev.get("rsi7"),
        "vol_ratio": ev.get("vol_ratio"),
        "oi_change_1h": ev.get("oi_change_1h"),
        "funding": ev.get("funding"),
        "status": "OPEN",
    }
    rev["open_positions"].append(pos)
    return pos


def _close_fill(rev, pos, qty_fill, exit_px, reason, bar):
    pnl, fee = _fill_pnl(pos["side"], qty_fill, pos["entry"], exit_px)
    pos["realized_pnl"] += pnl
    pos["fees_paid"] += fee
    pos["qty_remaining"] = max(0.0, pos["qty_remaining"] - qty_fill)
    rev["virtual_equity"] += pnl
    return pnl


def _finalize_position(rev, pos, reason, bar):
    pos["status"] = "CLOSED"
    pos["exit_reason"] = reason
    pos["exit_bar_t"] = bar["t"]
    pos["exit_time"] = _iso(bar["t"] / 1000)
    net = pos["realized_pnl"]
    pos["net_pnl"] = net
    pos["net_r"] = (net / pos["risk_usd"]) if pos["risk_usd"] > 0 else 0.0
    record = {
        "kind": "trade",
        "event_id": pos["event_id"],
        "mode": pos["mode"],
        "side": pos["side"],
        "entry": pos["entry"],
        "entry_time": _iso(pos["entry_bar_t"] / 1000),
        "exit_time": pos["exit_time"],
        "exit_reason": reason,
        "initial_stop": pos["initial_stop"],
        "r_distance": pos["r_distance"],
        "quantity": pos["quantity"],
        "margin_used": pos["margin_used"],
        "net_pnl": net,
        "net_r": pos["net_r"],
        "mfe_r": pos["mfe_r"],
        "mae_r": pos["mae_r"],
        "hit_1r_time": _iso(pos["hit_1r_t"] / 1000) if pos["hit_1r_t"] else None,
        "hit_15r_time": _iso(pos["hit_15r_t"] / 1000) if pos["hit_15r_t"] else None,
        "bars_held": pos["bars_held"],
        "fees_paid": pos["fees_paid"],
        "score": pos["score"],
        "score_breakdown": pos["score_breakdown"],
        "trend_1h": pos["trend_1h"],
        "trend_4h": pos["trend_4h"],
        "rsi7": pos["rsi7"],
        "vol_ratio": pos["vol_ratio"],
        "oi_change_1h": pos["oi_change_1h"],
        "funding": pos["funding"],
    }
    rev["ledger"].append(record)
    _update_stats(rev, pos, net)
    rev["peak_equity"] = max(rev["peak_equity"], rev["virtual_equity"])
    rev["equity_curve"].append({
        "t": bar["t"], "equity": rev["virtual_equity"],
    })
    if len(rev["equity_curve"]) > CURVE_LIMIT:
        rev["equity_curve"] = rev["equity_curve"][-CURVE_LIMIT:]


def _bucket_keys(pos):
    keys = ["ALL", pos["side"], pos["mode"].upper(),
            f"{pos['side']}_{pos['mode'].upper()}"]
    return keys


def _update_stats(rev, pos, net):
    for key in _bucket_keys(pos):
        b = rev["stats"][key]
        b["trades"] += 1
        if net > 0:
            b["wins"] += 1
            b["gross_win_usd"] += net
        elif net < 0:
            b["losses"] += 1
            b["gross_loss_usd"] += abs(net)
        b["total_r"] += pos["net_r"]
        curve = b["pnl_curve"]
        prev = curve[-1] if curve else 0.0
        curve.append(prev + net)
        if len(curve) > CURVE_LIMIT:
            b["pnl_curve"] = curve[-CURVE_LIMIT:]


def _manage_position(rev, pos, bar):
    """Advance one virtual position by one closed bar.
    Returns True when the position is fully closed."""
    side = pos["side"]
    entry = pos["entry"]
    r = pos["r_distance"]
    pos["bars_held"] += 1
    if side == "LONG":
        pos["mfe_r"] = max(pos["mfe_r"], (bar["h"] - entry) / r)
        pos["mae_r"] = min(pos["mae_r"], (bar["l"] - entry) / r)
    else:
        pos["mfe_r"] = max(pos["mfe_r"], (entry - bar["l"]) / r)
        pos["mae_r"] = min(pos["mae_r"], (entry - bar["h"]) / r)

    eff_stop = pos["initial_stop"]
    if pos["trail_active"] and pos["trail_stop"] is not None:
        eff_stop = (
            max(eff_stop, pos["trail_stop"]) if side == "LONG"
            else min(eff_stop, pos["trail_stop"])
        )

    # 1) stop first (conservative when a bar spans stop and TP)
    stop_hit = (
        bar["l"] <= eff_stop if side == "LONG"
        else bar["h"] >= eff_stop
    )
    if stop_hit and pos["qty_remaining"] > 0:
        adj = 1 - (SLIPPAGE_RATE + STOP_EXTRA_SLIPPAGE) \
            if side == "LONG" else 1 + (SLIPPAGE_RATE + STOP_EXTRA_SLIPPAGE)
        reason = (
            "TRAIL_STOP"
            if pos["trail_active"] and eff_stop != pos["initial_stop"]
            else "STOP"
        )
        _close_fill(rev, pos, pos["qty_remaining"], eff_stop * adj,
                    reason, bar)
        _finalize_position(rev, pos, reason, bar)
        return True

    # 2) staged take-profits
    def _tp(level_r, frac, flag, hit_key):
        if pos[flag] or pos["qty_remaining"] <= 0:
            return
        level = entry + level_r * r if side == "LONG" \
            else entry - level_r * r
        touched = bar["h"] >= level if side == "LONG" \
            else bar["l"] <= level
        if touched:
            pos[flag] = True
            pos[hit_key] = bar["t"]
            adj = 1 - SLIPPAGE_RATE if side == "LONG" \
                else 1 + SLIPPAGE_RATE
            qty_fill = min(
                pos["quantity"] * frac, pos["qty_remaining"]
            )
            _close_fill(rev, pos, qty_fill, level * adj,
                        f"TP{level_r}", bar)

    _tp(TP1_R, TP1_FRAC, "tp1_done", "hit_1r_t")
    _tp(TP2_R, TP2_FRAC, "tp2_done", "hit_15r_t")
    if pos["qty_remaining"] <= 1e-12:
        _finalize_position(rev, pos, "TP_FULL", bar)
        return True

    # 3) trailing stop update from this bar's close (applies
    # from the next bar)
    if side == "LONG":
        if bar["c"] >= entry + TRAIL_START_R * r:
            pos["trail_active"] = True
            cand = bar["c"] - TRAIL_ATR_MULT * pos["atr_at_entry"]
            pos["trail_stop"] = max(pos["trail_stop"] or cand, cand)
    else:
        if bar["c"] <= entry - TRAIL_START_R * r:
            pos["trail_active"] = True
            cand = bar["c"] + TRAIL_ATR_MULT * pos["atr_at_entry"]
            pos["trail_stop"] = min(pos["trail_stop"] or cand, cand)

    # 4) time expiry evaluation after TIME_BARS bars
    if pos["bars_held"] >= TIME_BARS and pos["qty_remaining"] > 0:
        adj = 1 - SLIPPAGE_RATE if side == "LONG" \
            else 1 + SLIPPAGE_RATE
        _close_fill(rev, pos, pos["qty_remaining"], bar["c"] * adj,
                    "TIME_EXPIRE", bar)
        _finalize_position(rev, pos, "TIME_EXPIRE", bar)
        return True
    return False


# ============================================================
# event lifecycle
# ============================================================

def _new_event(rev, side, bar, ev):
    tag = "L" if side == "LONG" else "S"
    event = {
        "event_id": f"REV-{tag}-{bar['t']}",
        "side": side,
        "started_bar_t": bar["t"],
        "start_time": _iso(bar["t"] / 1000),
        "ref_price": bar["c"],
        "extreme": ev["impulse"]["extreme"],
        "bars_active": 0,
        "alpha_fired": False,
        "alpha_attempted": False,
        "beta_fired": False,
        "beta_pending": False,
        "beta_resolved": False,
        "beta_pending_info": None,
        "beta_cancel_reason": None,
        "best_score": 0.0,
        "best_breakdown": None,
        "stages_seen": [],
        "track_high": bar["h"],
        "track_low": bar["l"],
        "fire_failures": [],
    }
    rev["events_total"] += 1
    rev["active_events"][side] = event
    return event


def _close_event(rev, event):
    side = event["side"]
    if not event["alpha_fired"] and not event["beta_fired"]:
        ref = event["ref_price"]
        if side == "LONG":
            mfe = (event["track_high"] / ref - 1.0) * 100 if ref else 0.0
            mae = (event["track_low"] / ref - 1.0) * 100 if ref else 0.0
        else:
            mfe = (1.0 - event["track_low"] / ref) * 100 if ref else 0.0
            mae = (1.0 - event["track_high"] / ref) * 100 if ref else 0.0
        reasons = []
        stages = event["stages_seen"]
        if "no_pressure" in stages and "candidate" not in stages \
                and "no_shift" not in stages:
            reasons.append("no_pressure_peak")
        if "no_shift" in stages and "candidate" not in stages:
            reasons.append("no_structure_shift")
        if "candidate" in stages and not event["alpha_attempted"]:
            reasons.append("score_below_alpha")
        if event["beta_cancel_reason"]:
            reasons.append(event["beta_cancel_reason"])
        if event["fire_failures"]:
            reasons.append("fire_failed")
        if not reasons:
            reasons.append("expired")
        rev["ledger"].append({
            "kind": "unfilled",
            "event_id": event["event_id"],
            "side": side,
            "start_time": event["start_time"],
            "ref_price": ref,
            "extreme": event["extreme"],
            "best_score": event["best_score"],
            "best_breakdown": event["best_breakdown"],
            "stages_seen": event["stages_seen"],
            "reasons": reasons,
            "track_mfe_pct": mfe,
            "track_mae_pct": mae,
            "bars_tracked": event["bars_active"],
            "fire_failures": event["fire_failures"],
        })
        rev["unfilled_total"] += 1
    rev["active_events"][side] = None


def _process_side(rev, candles, idx, volumes, atr_s, rsi_s, side,
                  oi_change, funding, trend_1h, trend_4h):
    bar = candles[idx]
    ev = _evaluate_side(
        candles, idx, volumes, atr_s, rsi_s, side, oi_change, funding
    )
    if ev is not None:
        ev["trend_1h"] = trend_1h
        ev["trend_4h"] = trend_4h
    event = rev["active_events"].get(side)
    if event is None:
        if ev is None:
            return
        event = _new_event(rev, side, bar, ev)

    event["bars_active"] += 1
    event["track_high"] = max(event["track_high"], bar["h"])
    event["track_low"] = min(event["track_low"], bar["l"])
    if side == "LONG":
        event["extreme"] = min(event["extreme"], bar["l"])
    else:
        event["extreme"] = max(event["extreme"], bar["h"])

    if ev is not None:
        stage = ev["stage"]
        if stage not in event["stages_seen"]:
            event["stages_seen"].append(stage)
        if stage == "candidate":
            if ev["score"] > event["best_score"]:
                event["best_score"] = ev["score"]
                event["best_breakdown"] = ev["breakdown"]

            # --- alpha: early entry on the shift bar ---
            if not event["alpha_attempted"] \
                    and ev["score"] >= SCORE_ALPHA:
                event["alpha_attempted"] = True
                pos = _open_virtual(
                    rev, event, "alpha", side, bar, ev["atr"], ev
                )
                if pos is not None:
                    event["alpha_fired"] = True

            # --- beta: arm the confirmation watch ---
            if not event["beta_fired"] and not event["beta_pending"] \
                    and not event["beta_resolved"] \
                    and ev["score"] >= SCORE_BETA:
                event["beta_pending"] = True
                event["beta_pending_info"] = {
                    "bar_t": bar["t"],
                    "shift_extreme_bar": (
                        bar["h"] if side == "LONG" else bar["l"]
                    ),
                    "bars": 0,
                    "score": ev["score"],
                    "raw_score": ev["raw_score"],
                    "breakdown": ev["breakdown"],
                    "atr": ev["atr"],
                    "rsi7": ev["rsi7"],
                    "vol_ratio": ev["vol_ratio"],
                    "oi_change_1h": ev["oi_change_1h"],
                    "funding": ev["funding"],
                    "trend_1h": trend_1h,
                    "trend_4h": trend_4h,
                }

    # --- beta confirmation progression (later bars only) ---
    info = event.get("beta_pending_info")
    if event["beta_pending"] and info and bar["t"] != info["bar_t"]:
        info["bars"] += 1
        atr_val = atr_s[idx] or info["atr"]
        tol = 0.05 * atr_val
        if side == "LONG":
            invalid = bar["l"] < event["extreme"] - STOP_EXTREME_ATR * atr_val
            confirmed = (
                bar["c"] > info["shift_extreme_bar"]
                and bar["l"] > event["extreme"] - tol
            )
        else:
            invalid = bar["h"] > event["extreme"] + STOP_EXTREME_ATR * atr_val
            confirmed = (
                bar["c"] < info["shift_extreme_bar"]
                and bar["h"] < event["extreme"] + tol
            )
        if invalid:
            event["beta_pending"] = False
            event["beta_resolved"] = True
            event["beta_cancel_reason"] = "beta_invalidated"
        elif confirmed:
            ev_beta = {
                "score": info["score"], "raw_score": info["raw_score"],
                "breakdown": info["breakdown"], "atr": info["atr"],
                "rsi7": info["rsi7"], "vol_ratio": info["vol_ratio"],
                "oi_change_1h": info["oi_change_1h"],
                "funding": info["funding"],
                "trend_1h": info["trend_1h"],
                "trend_4h": info["trend_4h"],
            }
            pos = _open_virtual(
                rev, event, "beta", side, bar, atr_s[idx] or info["atr"],
                ev_beta,
            )
            event["beta_pending"] = False
            event["beta_resolved"] = True
            if pos is not None:
                event["beta_fired"] = True
            else:
                event["beta_cancel_reason"] = "beta_fire_failed"
        elif info["bars"] > BETA_CONFIRM_BARS:
            event["beta_pending"] = False
            event["beta_resolved"] = True
            event["beta_cancel_reason"] = "beta_unconfirmed"

    # --- event expiry ---
    if event["bars_active"] >= EVENT_MAX_BARS or (
        event["alpha_attempted"]
        and not event["beta_pending"]
        and event["bars_active"] >= 8
    ):
        _close_event(rev, event)


# ============================================================
# public entry points (called from bot.py main cycle)
# ============================================================

def _oi_change_1h(rev):
    hist = rev.get("oi_history") or []
    if len(hist) < OI_MIN_SAMPLES:
        return None
    ref = _f(hist[-OI_MIN_SAMPLES].get("oi"))
    now = _f(hist[-1].get("oi"))
    if ref <= 0 or now <= 0:
        return None
    return (now - ref) / ref


def _update(rev, state, c15, c1, c4, market, ctx):
    if not c15 or len(c15) < 60:
        return {"ok": True, "note": "insufficient_candles",
                "bars": 0}
    closes = [x["c"] for x in c15]
    volumes = [x["v"] for x in c15]
    atr_s = _atr_series(c15)
    rsi_s = _rsi_series(closes)

    funding = 0.0
    if isinstance(ctx, dict) and ctx.get("funding") is not None:
        funding = _f(ctx.get("funding"))
    elif isinstance(market, dict):
        funding = _f(market.get("funding"))
    trend_1h = ctx.get("trend_1h") if isinstance(ctx, dict) else None
    trend_4h = ctx.get("trend_4h") if isinstance(ctx, dict) else None

    last_t = c15[-1]["t"]
    oi_now = _f((market or {}).get("open_interest"))
    if oi_now > 0:
        hist = rev["oi_history"]
        if not hist or hist[-1].get("ts", 0) < last_t // 1000:
            hist.append({"ts": last_t // 1000, "oi": oi_now})
            rev["oi_history"] = hist[-OI_HISTORY_MAX:]
    oi_change = _oi_change_1h(rev)

    new_idx = [
        i for i, cd in enumerate(c15) if cd["t"] > rev["last_bar_t"]
    ]
    for idx in new_idx:
        bar = c15[idx]
        # manage existing virtual positions first (positions
        # opened on this bar are managed from the next bar)
        still_open = []
        for pos in rev["open_positions"]:
            if _manage_position(rev, pos, bar):
                continue
            still_open.append(pos)
        rev["open_positions"] = still_open
        for side in ("LONG", "SHORT"):
            _process_side(
                rev, c15, idx, volumes, atr_s, rsi_s, side,
                oi_change, funding, trend_1h, trend_4h,
            )
        rev["last_bar_t"] = bar["t"]

    if len(rev["ledger"]) > LEDGER_LIMIT:
        rev["ledger"] = rev["ledger"][-LEDGER_LIMIT:]
    rev["last_error"] = None
    rev["updated_at"] = _iso(last_t / 1000)
    return {
        "ok": True,
        "bars": len(new_idx),
        "open_positions": len(rev["open_positions"]),
        "active_events": {
            k: (v["event_id"] if v else None)
            for k, v in rev["active_events"].items()
        },
        "closed_trades": rev["stats"]["ALL"]["trades"],
        "unfilled_total": rev["unfilled_total"],
        "virtual_equity": rev["virtual_equity"],
    }


def update_rev(state, c15, c1, c4, market, ctx):
    """Advance the REV shadow engine by one house cycle.

    Isolation contract: works on a deep copy of state["rev"] and
    commits it back only on success; on any error the house state
    is left untouched (only last_error is noted). Never raises.
    """
    try:
        _ensure_rev(state)
        work = copy.deepcopy(state["rev"])
        summary = _update(work, state, c15, c1, c4, market, ctx)
        state["rev"] = work
        return summary
    except Exception as exc:  # noqa: BLE001 - isolation by design
        try:
            cur = state.get("rev")
            if isinstance(cur, dict):
                cur["last_error"] = f"{type(exc).__name__}: {exc}"
        except Exception:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _bucket_view(b, start_equity):
    trades = b["trades"]
    gw, gl = b["gross_win_usd"], b["gross_loss_usd"]
    pf = (gw / gl) if gl > 0 else None
    expectancy = (b["total_r"] / trades) if trades else 0.0
    # max drawdown of the bucket's cumulative pnl curve, as a
    # percentage of the REV start equity (approximation, labelled)
    peak = 0.0
    max_dd = 0.0
    for v in b["pnl_curve"]:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    dd_pct = (max_dd / start_equity * 100.0) if start_equity > 0 else 0.0
    return {
        "trades": trades,
        "wins": b["wins"],
        "losses": b["losses"],
        "win_rate": (b["wins"] / trades) if trades else 0.0,
        "net_usd": gw - gl,
        "total_r": b["total_r"],
        "expectancy_r": expectancy,
        "profit_factor": pf,
        "max_dd_pct_of_start": dd_pct,
        "stage0": {
            "trades_ok": trades >= 100,
            "expectancy_ok": expectancy > 0,
            "pf_ok": (pf is not None and pf >= 1.2),
            "dd_ok": dd_pct <= 15.0,
            "passed": (
                trades >= 100 and expectancy > 0
                and pf is not None and pf >= 1.2 and dd_pct <= 15.0
            ),
        },
    }


def dashboard_payload(state):
    """Read-only REV summary for docs/data.json (fully separated
    from the house performance sections)."""
    rev = state.get("rev") if isinstance(state, dict) else None
    base = {
        "enabled": True,
        "version": REV_VERSION,
        "measurement_only": True,
        "note": "REV shadow: virtual trades only; no effect on "
                "house positions, equity or risk.",
    }
    if not isinstance(rev, dict) or "stats" not in rev:
        base["initialized"] = False
        return base
    start = rev.get("start_equity") or 100.0
    stats = {
        k: _bucket_view(b, start) for k, b in rev["stats"].items()
    }
    # true equity-curve drawdown for the ALL bucket
    peak = rev.get("start_equity") or 0.0
    max_dd_pct = 0.0
    for pt in rev.get("equity_curve", []):
        eq = _f(pt.get("equity"))
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100.0)
    stats["ALL"]["max_dd_pct"] = max_dd_pct
    stats["ALL"]["stage0"]["dd_ok"] = max_dd_pct <= 15.0
    stats["ALL"]["stage0"]["passed"] = (
        stats["ALL"]["stage0"]["trades_ok"]
        and stats["ALL"]["stage0"]["expectancy_ok"]
        and stats["ALL"]["stage0"]["pf_ok"]
        and stats["ALL"]["stage0"]["dd_ok"]
    )
    base.update({
        "initialized": True,
        "virtual_equity": rev.get("virtual_equity"),
        "start_equity": start,
        "events_total": rev.get("events_total", 0),
        "unfilled_total": rev.get("unfilled_total", 0),
        "open_positions": [
            {
                "event_id": p["event_id"], "mode": p["mode"],
                "side": p["side"], "entry": p["entry"],
                "bars_held": p["bars_held"],
                "mfe_r": p["mfe_r"], "mae_r": p["mae_r"],
                "qty_remaining_frac": (
                    p["qty_remaining"] / p["quantity"]
                    if p["quantity"] else 0.0
                ),
                "score": p["score"],
            }
            for p in rev.get("open_positions", [])
        ],
        "active_events": {
            k: (
                {
                    "event_id": v["event_id"],
                    "best_score": v["best_score"],
                    "bars_active": v["bars_active"],
                    "alpha_fired": v["alpha_fired"],
                    "beta_fired": v["beta_fired"],
                    "beta_pending": v["beta_pending"],
                } if v else None
            )
            for k, v in rev.get("active_events", {}).items()
        },
        "stats": stats,
        "recent_ledger": list(reversed(rev.get("ledger", [])[-8:])),
        "last_bar_time": (
            _iso(rev["last_bar_t"] / 1000) if rev.get("last_bar_t")
            else None
        ),
        "updated_at": rev.get("updated_at"),
        "last_error": rev.get("last_error"),
    })
    return base
