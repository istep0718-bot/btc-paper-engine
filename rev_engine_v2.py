#!/usr/bin/env python3
"""
REV ENGINE V2 -- reversal shadow measurement (MEASUREMENT ONLY).

Purpose
-------
REV V1 (rev_engine.py, running unchanged in parallel) detects only
the "selling-pressure peak" pattern: a climax down bar plus a
structure shift. On 2026-11-21 the 55,170 bottom printed a
maximum-volume *bullish absorption* bar instead, and V1 scored the
reversal 58 (< alpha 65) and did not fire. V2 generalises the
detector into three INDEPENDENT systems per side, adds a full
candidate ledger (including candidates that never fire, for
missed-move analysis), separates RSI level from RSI dynamics, and
records multi-timeframe context built only from closed 15m bars.

V2 is, like V1, a completely independent shadow engine:

  * It NEVER opens a real paper position, never touches the house
    equity / risk state / daily stats, and never influences any
    house entry or exit. It reads only the candles and market data
    the house cycle already fetched, and writes only state["rev2"].
  * It imports nothing from bot.py and nothing from rev_engine.py:
    V1 keeps running byte-identical, V1 statistics and V2
    statistics are never mixed, and a failure in either engine
    cannot affect the other or the house.
  * Its virtual book uses its own virtual equity (snapshotted from
    the house equity once, at first initialisation).
  * All failures are swallowed by update_rev2(); the caller also
    wraps the call. There is deliberately NO entry-stop / kill
    switch for REV2 (warnings only), per the owner's standing rule.

Systems (each side; SHORT is the exact mirror)
----------------------------------------------
  L  legacy pressure-peak system. The V1 definitions, component
     scores, normalisation and thresholds (alpha 65 / beta 75)
     are copied verbatim so that V2's own ledger carries the same
     judgement V1 makes, while V1 itself continues untouched.
  A  bullish-absorption CANDIDATE system. After a sharp decline,
     a high-volume bullish bar that closes in the upper part of
     its range is scored as an "absorption candidate". OHLCV alone
     cannot prove real order absorption, so the system name and
     all records say "candidate" -- no claim of actual absorption.
     A bearish climax bar is NOT required (that is system L).
  F  fake-break system. The bar's wick breaks the recent swing
     low, but the close recovers back above that level. A plain
     new low without recovery is never treated as a reversal.

Threshold design (fixed at measurement start; NOT fitted to the
2026-11-21 bottom -- that episode was used only to find the
structural blind spot, per the owner's instruction)
------------------------------------------------------------------------------
  L keeps V1's 65 / 75 because its mandatory gates (impulse +
  climax + deceleration + structure shift) are already strict.
  A and F are single-bar pattern detectors with weaker mandatory
  gates, so their 100-point scales weight the three pillars
  (decline premise / volume anomaly / bar shape) plus dynamic
  confirmation (RSI recovery, structure shift). Alpha = 60 is the
  level where the three pillars are substantially present; beta
  = 72 additionally requires dynamic confirmation. Beta still
  enters only on a separate confirmation bar, exactly like V1.

RSI handling
------------
RSI is never used as a hard exclusion by level. Four quantities
are scored separately: current level, 3-bar change, recovery from
the recent RSI trough (LONG) / peak (SHORT), and price/RSI
divergence. RSI at 65 with a fast recovery does NOT exclude a
candidate; RSI <= 30 with price still making new lows and RSI
still falling does NOT confirm one either (the structure-shift /
recovery terms simply score zero).

Multi-timeframe context
-----------------------
Built ONLY from closed 15m bars: 30m = last 2 bars, 1h = last 4,
4h = last 16. Snapshots are recorded on candidates and trades for
later analysis; they do not gate entries (upper-timeframe
counter-trend is allowed by design, as in V1). No forming bar is
ever read: bot.parse_candles drops the unfinished candle, and
passing it in would require changing house fetch code, which is
out of bounds for this engine.

PRE-ALERT
---------
Because only closed bars are available (see above), a PRE-ALERT
in V2 means: on closed bars, a candidate's premise and volume
conditions are substantially formed but no system has confirmed
yet. PRE-ALERTs are recorded in the ledger stream and are never
counted as alpha fires or trades.

Candidate ledger / missed-move analysis
---------------------------------------
A candidate starts when a loose impulse premise holds (peak-to-
trough >= 1.2% or >= 1.2 x ATR within 6 bars, trough within 2
bars) -- looser than system L's gate, so A/F patterns and even
complete non-events are tracked. Each candidate is tracked for
24 bars (6h) and records: detection time/price, per-system best
scores and breakdowns, fired / not fired per system and mode,
not-fired reasons, close-to-close returns at +15m / +30m / +1h /
+4h, MFE / MAE over the window and over the first 16 bars, the
virtual entry that would have applied, and the reversal outcome.

"Reversal" evaluation definition (FIXED for V2 measurement;
used only for scoring outcomes, never to change a past
judgement): within the first 16 bars after detection, price
reaches +2.0% in the candidate's favour BEFORE it first moves
-1.0% against it (same-bar ambiguity resolves against the
candidate). Capture rate = fired candidates among established
reversals; miss rate = unfired among established reversals;
false-fire rate = fired candidates that did not establish a
reversal. Missed-move buckets additionally count unfired
candidates whose 16-bar MFE reached +1% / +2% / +3%.

Duplicate control
-----------------
One active candidate per side. Multiple systems firing inside
one candidate open separate virtual trades (per-system
measurement), but detection statistics count the candidate once.
The same (system, mode) can fire at most once per candidate.

Exits for virtual trades are IDENTICAL to V1 (comparability):
+1.0R closes 25%, +1.5R closes 25%, remainder trails at 1.5 x
ATR from +1.0R, initial stop beyond the candidate extreme by
0.15 x ATR, time expiry after 8 bars, stop wins same-bar ties.

Retro / forward separation
--------------------------
The first update_rev2() call backfills the fetched candle window
(about 200 closed bars); candidates started during that call are
phase "retro". Candidates started afterwards are "forward" and
forward_start_t records the first forward bar. Statistics are
kept per phase and never merged in the payload.

Definition versioning
---------------------
DEFINITION_VERSION is stored in state["rev2"]. If a future
module carries a different definition string, the old section is
archived untouched under state["rev2_archive"] and a fresh
section is started -- definitions are never silently re-tuned
inside a running measurement.

Cost / sizing model mirrors the house paper model exactly
(taker fee 0.05%/side, base slippage 0.02%/side, +0.05% extra
slippage on stop fills; risk 5% of virtual equity, leverage
40x, final margin clamped into the 25%-50% band), duplicated
here on purpose for isolation.

Standard library only.
"""

import copy
import math
from datetime import datetime, timezone

REV2_VERSION = "REV_ENGINE_V2_2026-11-22"
DEFINITION_VERSION = "REV2_DEF_2026-11-22_V1"

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

# ---- shared indicator parameters ----
ATR_PERIOD = 14
RSI_PERIOD = 7
VOL_PERIOD = 20               # volume baseline (prior bars)

# ---- system L: V1 parameters, copied verbatim (do not change) ----
L_IMPULSE_BARS = 4
L_IMPULSE_PCT = 0.018
L_IMPULSE_ATR_MULT = 1.8
L_VOL_CLIMAX = 1.5
L_WICK_FULL = 0.35
L_RSI_EXTREME_LONG = 20.0
L_RSI_EXTREME_SHORT = 80.0
L_SCORE_ALPHA = 65.0
L_SCORE_BETA = 75.0
L_OI_DROP_FULL = -0.03
L_OI_DROP_MID = -0.015
_L_SCORE_MAX = {
    "pressure": 20.0,
    "structure": 25.0,
    "volume": 15.0,
    "wick": 15.0,
    "rsi": 10.0,
    "oi": 10.0,
    "funding": 5.0,
}

# ---- candidate premise (looser than L, for ledger coverage) ----
PREMISE_BARS = 6
PREMISE_PCT = 0.012
PREMISE_ATR_MULT = 1.2
CANDIDATE_MAX_BARS = 24       # 6h tracking window
REVERSAL_HORIZON_BARS = 16    # 4h evaluation window
REVERSAL_TARGET_PCT = 0.02    # +2.0% favourable first ...
REVERSAL_INVALID_PCT = 0.01   # ... before -1.0% adverse
MISS_THRESHOLDS = (0.01, 0.02, 0.03)

# ---- system A: absorption candidate ----
A_VOL_MIN = 1.2               # mandatory volume anomaly floor
A_CLOSE_POS_MIN = 0.55        # mandatory close position floor
A_SCORE_ALPHA = 60.0
A_SCORE_BETA = 72.0
_A_SCORE_MAX = {
    "decline": 15.0,
    "volume": 20.0,
    "shape": 25.0,
    "rsi_dyn": 15.0,
    "structure": 15.0,
    "oi": 5.0,
    "funding": 5.0,
}

# ---- system F: fake break ----
F_LOOKBACK = 10               # prior swing reference bars
F_BREAK_MIN_ATR = 0.05        # mandatory wick break depth
F_CLOSE_POS_MIN = 0.50        # mandatory close position floor
F_SCORE_ALPHA = 60.0
F_SCORE_BETA = 72.0
_F_SCORE_MAX = {
    "break": 15.0,
    "recovery": 20.0,
    "close_pos": 15.0,
    "volume": 15.0,
    "structure": 20.0,
    "rsi_dyn": 10.0,
    "reclaim": 5.0,
}

# ---- exits (identical to V1) ----
BETA_CONFIRM_BARS = 4
TP1_R = 1.0
TP1_FRAC = 0.25
TP2_R = 1.5
TP2_FRAC = 0.25
TRAIL_START_R = 1.0
TRAIL_ATR_MULT = 1.5
STOP_EXTREME_ATR = 0.15
TIME_BARS = 8
MAX_STOP_ATR = 4.0

# ---- OI history (own samples, same approach as V1) ----
OI_HISTORY_MAX = 64
OI_MIN_SAMPLES = 5

LEDGER_LIMIT = 1000
PREALERT_LIMIT = 200
CURVE_LIMIT = 2000

SYSTEMS = ("L", "A", "F")
_ALPHA = {"L": L_SCORE_ALPHA, "A": A_SCORE_ALPHA, "F": F_SCORE_ALPHA}
_BETA = {"L": L_SCORE_BETA, "A": A_SCORE_BETA, "F": F_SCORE_BETA}


# ============================================================
# small numeric helpers (self-contained)
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
    """Wilder ATR over candle dicts; list aligned to candles."""
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


def _vol_ratio(volumes, idx):
    base = _vol_baseline(volumes, idx)
    if base is None or base <= 0:
        return None
    return volumes[idx] / base


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


def _bucket_keys_for(pos):
    keys = [
        "ALL", pos["side"], pos["mode"].upper(),
        "SYS_" + pos["system"],
        pos["system"] + "_" + pos["mode"].upper(),
        pos["side"] + "_" + pos["mode"].upper(),
        pos["side"] + "_" + pos["system"],
    ]
    return keys


_ALL_BUCKET_KEYS = ["ALL", "LONG", "SHORT", "ALPHA", "BETA"]
for _s in SYSTEMS:
    _ALL_BUCKET_KEYS.append("SYS_" + _s)
    for _m in ("ALPHA", "BETA"):
        _ALL_BUCKET_KEYS.append(_s + "_" + _m)
    for _d in ("LONG", "SHORT"):
        _ALL_BUCKET_KEYS.append(_d + "_" + _s)
for _d in ("LONG", "SHORT"):
    for _m in ("ALPHA", "BETA"):
        _ALL_BUCKET_KEYS.append(_d + "_" + _m)


def _new_detection():
    return {
        "candidates": 0,
        "fired": 0,
        "reversals": 0,
        "captured": 0,
        "missed": 0,
        "false_fires": 0,
        "miss_mfe_1pct": 0,
        "miss_mfe_2pct": 0,
        "miss_mfe_3pct": 0,
        "systems": {
            s: {"detected": 0, "fired": 0, "captured": 0}
            for s in SYSTEMS
        },
    }


def _new_rev2_state(start_equity):
    return {
        "version": REV2_VERSION,
        "definition_version": DEFINITION_VERSION,
        "enabled": True,
        "start_equity": float(start_equity),
        "virtual_equity": float(start_equity),
        "peak_equity": float(start_equity),
        "last_bar_t": 0,
        "oi_history": [],
        "initialized": False,
        "forward_start_t": None,
        "active_candidates": {"LONG": None, "SHORT": None},
        "open_positions": [],
        "ledger": [],
        "pre_alerts": [],
        "stats": {b: _new_bucket() for b in _ALL_BUCKET_KEYS},
        "detection": {
            "retro": _new_detection(),
            "forward": _new_detection(),
        },
        "equity_curve": [],
        "candidates_total": 0,
        "last_error": None,
        "updated_at": None,
    }


def _ensure_rev2(state):
    rev2 = state.get("rev2")
    if isinstance(rev2, dict) and "stats" in rev2:
        if rev2.get("definition_version") != DEFINITION_VERSION:
            # Definition changed: archive the old measurement
            # untouched and start fresh. Never mix definitions.
            archive = state.get("rev2_archive")
            if not isinstance(archive, dict):
                archive = {}
            old_def = rev2.get("definition_version") or "unknown"
            archive[old_def] = rev2
            state["rev2_archive"] = archive
            rev2 = None
        else:
            return rev2
    if not isinstance(rev2, dict) or "stats" not in rev2:
        start = _f(state.get("equity"), 100.0)
        if start <= 0:
            start = 100.0
        rev2 = _new_rev2_state(start)
        state["rev2"] = rev2
    return rev2


# ============================================================
# RSI dynamics (level is never a hard exclusion in V2)
# ============================================================

def _rsi_eval(candles, rsi_s, idx, side):
    """Separate RSI quantities for one bar / side.

    Returns dict with:
      value          current RSI(7)
      delta3         change over the last 3 bars
      recovery       LONG: rise from the RSI trough of the last
                     7 bars; SHORT: fall from the RSI peak
      divergence     price made a new extreme vs the prior swing
                     bar while RSI did not (bullish for LONG)
      still_falling  LONG: price is still printing new lows and
                     RSI is still falling (no reversal evidence)
    """
    val = rsi_s[idx] if idx < len(rsi_s) else None
    if val is None:
        return {
            "value": None, "delta3": None, "recovery": None,
            "divergence": False, "still_falling": False,
        }
    delta3 = None
    if idx >= 3 and rsi_s[idx - 3] is not None:
        delta3 = val - rsi_s[idx - 3]
    window = [
        rsi_s[j] for j in range(max(0, idx - 6), idx + 1)
        if rsi_s[j] is not None
    ]
    recovery = None
    if window:
        if side == "LONG":
            recovery = val - min(window)
        else:
            recovery = max(window) - val
    divergence = False
    still_falling = False
    if idx >= 11:
        cd = candles[idx]
        if side == "LONG":
            j = min(
                range(idx - 10, idx - 1),
                key=lambda k: candles[k]["l"],
            )
            tol = 1e-9
            if cd["l"] <= candles[j]["l"] + tol \
                    and rsi_s[j] is not None \
                    and val > rsi_s[j] + 2.0:
                divergence = True
            recent_low = min(
                candles[k]["l"] for k in range(idx - 1, idx + 1)
            )
            prior_low = min(
                candles[k]["l"] for k in range(idx - 6, idx - 1)
            )
            still_falling = (
                recent_low < prior_low
                and delta3 is not None and delta3 < 0
            )
        else:
            j = max(
                range(idx - 10, idx - 1),
                key=lambda k: candles[k]["h"],
            )
            tol = 1e-9
            if cd["h"] >= candles[j]["h"] - tol \
                    and rsi_s[j] is not None \
                    and val < rsi_s[j] - 2.0:
                divergence = True
            recent_high = max(
                candles[k]["h"] for k in range(idx - 1, idx + 1)
            )
            prior_high = max(
                candles[k]["h"] for k in range(idx - 6, idx - 1)
            )
            still_falling = (
                recent_high > prior_high
                and delta3 is not None and delta3 > 0
            )
    return {
        "value": val, "delta3": delta3, "recovery": recovery,
        "divergence": divergence, "still_falling": still_falling,
    }


def _mtf_snapshot(candles, idx):
    """Multi-timeframe context aggregated from CLOSED 15m bars
    only: 30m = last 2 bars, 1h = last 4, 4h = last 16."""
    out = {}
    for label, span in (("30m", 2), ("1h", 4), ("4h", 16)):
        if idx + 1 < span:
            out[label] = None
            continue
        window = candles[idx - span + 1:idx + 1]
        o = window[0]["o"]
        c = window[-1]["c"]
        pct = (c / o - 1.0) * 100.0 if o > 0 else 0.0
        if pct > 0.05:
            direction = "UP"
        elif pct < -0.05:
            direction = "DOWN"
        else:
            direction = "FLAT"
        out[label] = {"change_pct": pct, "direction": direction}
    return out


# ============================================================
# impulse / premise (generalised; L uses the V1 constants)
# ============================================================

def _impulse(candles, idx, atr_val, side, bars, pct_min, atr_mult):
    """Peak-to-trough impulse inside the last `bars` bars.

    For LONG the peak must come before the trough and the trough
    must be recent (within 2 bars of idx). SHORT is the mirror.
    """
    lo_i = max(0, idx - bars)
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
        ok = (pct <= -pct_min or width >= atr_mult * atr_val)
    else:
        trough_i0 = min(range(len(window)), key=lambda j: window[j]["l"])
        after = window[trough_i0:]
        peak_i = max(range(len(after)), key=lambda j: after[j]["h"])
        trough = window[trough_i0]["l"]
        peak = after[peak_i]["h"]
        trough_abs = lo_i + trough_i0 + peak_i
        width = peak - trough
        pct = (peak - trough) / trough if trough > 0 else 0.0
        ok = (pct >= pct_min or width >= atr_mult * atr_val)
    if idx - trough_abs > 2:
        ok = False
    return {
        "ok": bool(ok), "peak": peak, "trough": trough,
        "width": width, "pct": pct,
        "extreme": trough if side == "LONG" else peak,
        "extreme_idx": trough_abs,
        "peak_idx": (
            lo_i + peak_i if side == "LONG" else trough_abs
        ),
    }


def _premise(candles, idx, atr_val, side):
    """Loose candidate premise (ledger coverage, not an entry)."""
    return _impulse(
        candles, idx, atr_val, side,
        PREMISE_BARS, PREMISE_PCT, PREMISE_ATR_MULT,
    )


# ============================================================
# system L -- V1 logic copied verbatim (definitions unchanged)
# ============================================================

def _l_pressure(candles, idx, volumes, atr_val, side, imp):
    """V1 selling/buying pressure peak: climax + deceleration."""
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
            if mult >= L_VOL_CLIMAX:
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
    """15m short-term structure shift (V1 definition, shared by
    all three V2 systems as one scoring component).

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


def _l_score(candles, idx, volumes, atr_val, rsi_val, side,
             pressure, shift_ok, oi_change, funding):
    """V1 component scores; returns (breakdown, raw, avail_max,
    vol_ratio). Copied verbatim from rev_engine.py V1."""
    bd = {}
    mult = pressure["climax_mult"]
    if mult >= 2.0:
        bd["pressure"] = 20.0
    elif mult >= L_VOL_CLIMAX:
        bd["pressure"] = 14.0 + 6.0 * (mult - L_VOL_CLIMAX) / 0.5
    elif mult >= 1.0:
        bd["pressure"] = 6.0 + 8.0 * (mult - 1.0) / 0.5
    else:
        bd["pressure"] = 6.0 * max(mult, 0.0)
    if not pressure["decelerating"]:
        bd["pressure"] *= 0.5
    bd["structure"] = 25.0 if shift_ok else 0.0
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
    wick_pts = 10.0 * min(pressure["wick_ratio"] / L_WICK_FULL, 1.0)
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
    if rsi_val is None:
        bd["rsi"] = 0.0
    elif side == "LONG":
        if rsi_val <= L_RSI_EXTREME_LONG:
            bd["rsi"] = 10.0
        elif rsi_val < 30.0:
            bd["rsi"] = 10.0 * (30.0 - rsi_val) / 10.0
        else:
            bd["rsi"] = 0.0
    else:
        if rsi_val >= L_RSI_EXTREME_SHORT:
            bd["rsi"] = 10.0
        elif rsi_val > 70.0:
            bd["rsi"] = 10.0 * (rsi_val - 70.0) / 10.0
        else:
            bd["rsi"] = 0.0
    available = dict(_L_SCORE_MAX)
    if oi_change is None:
        bd["oi"] = 0.0
        available.pop("oi")
    elif oi_change <= L_OI_DROP_FULL:
        bd["oi"] = 10.0
    elif oi_change <= L_OI_DROP_MID:
        bd["oi"] = 6.0
    elif oi_change < 0:
        bd["oi"] = 3.0
    else:
        bd["oi"] = 0.0
    if side == "LONG":
        bd["funding"] = 5.0 if funding < 0 else (
            2.0 if funding == 0 else 0.0)
    else:
        bd["funding"] = 5.0 if funding > 0 else (
            2.0 if funding == 0 else 0.0)
    raw = sum(bd.values())
    avail_max = sum(available.values())
    return bd, raw, avail_max, vol_ratio


def _eval_l(candles, idx, volumes, atr_s, rsi_s, side,
            oi_change, funding):
    """System L evaluation for one bar (V1 semantics)."""
    atr_val = atr_s[idx]
    if atr_val is None or atr_val <= 0:
        return None
    imp = _impulse(
        candles, idx, atr_val, side,
        L_IMPULSE_BARS, L_IMPULSE_PCT, L_IMPULSE_ATR_MULT,
    )
    if imp is None or not imp["ok"]:
        return {"detected": False, "stage": "no_impulse",
                "score": None, "breakdown": None}
    pressure = _l_pressure(candles, idx, volumes, atr_val, side, imp)
    if not pressure["ok"]:
        return {"detected": False, "stage": "no_pressure",
                "score": None, "breakdown": None,
                "impulse": imp, "pressure": pressure}
    shift_ok = _structure_shift(candles, idx, atr_val, side)
    bd, raw, avail_max, vol_ratio = _l_score(
        candles, idx, volumes, atr_val, rsi_s[idx], side,
        pressure, shift_ok, oi_change, funding,
    )
    norm = (raw / avail_max * 100.0) if avail_max > 0 else 0.0
    return {
        "detected": bool(shift_ok), "stage": (
            "candidate" if shift_ok else "no_shift"),
        "score": norm, "raw_score": raw, "breakdown": bd,
        "impulse": imp, "pressure": pressure, "shift": shift_ok,
        "vol_ratio": vol_ratio, "atr": atr_val,
    }


# ============================================================
# system A -- bullish absorption candidate (SHORT = mirror)
# ============================================================

def _eval_a(candles, idx, volumes, atr_s, rsi_s, side,
            oi_change, funding, premise):
    """Absorption-candidate evaluation for one bar.

    Mandatory pattern (LONG): the premise decline holds, the bar
    is bullish, its volume is at least A_VOL_MIN x the prior-20
    mean, and it closes at or above A_CLOSE_POS_MIN of its range.
    No bearish climax is required. The score never claims real
    order absorption -- it grades an OHLCV pattern only.
    """
    atr_val = atr_s[idx]
    if atr_val is None or atr_val <= 0 or premise is None \
            or not premise["ok"]:
        return {"detected": False, "stage": "no_premise",
                "score": None, "breakdown": None}
    cd = candles[idx]
    rng = max(cd["h"] - cd["l"], 1e-9)
    if side == "LONG":
        bullish = cd["c"] > cd["o"]
        close_pos = (cd["c"] - cd["l"]) / rng
        body_ratio = (cd["c"] - cd["o"]) / rng if bullish else 0.0
        wick_ratio = (min(cd["o"], cd["c"]) - cd["l"]) / rng
    else:
        bullish = cd["c"] < cd["o"]
        close_pos = (cd["h"] - cd["c"]) / rng
        body_ratio = (cd["o"] - cd["c"]) / rng if bullish else 0.0
        wick_ratio = (cd["h"] - max(cd["o"], cd["c"])) / rng
    vr = _vol_ratio(volumes, idx)
    if not bullish:
        return {"detected": False, "stage": "not_counter_bar",
                "score": None, "breakdown": None}
    mandatory = (
        vr is not None and vr >= A_VOL_MIN
        and close_pos >= A_CLOSE_POS_MIN
    )
    # --- decline premise strength: 15 ---
    pct_pts = min(abs(premise["pct"]) / 0.025, 1.0) * 15.0
    atr_pts = min(
        premise["width"] / (2.5 * atr_val), 1.0) * 15.0
    bd = {"decline": max(pct_pts, atr_pts)}
    # --- volume anomaly: 20 ---
    if vr is None:
        bd["volume"] = 0.0
    elif vr >= 2.0:
        bd["volume"] = 20.0
    elif vr >= 1.5:
        bd["volume"] = 16.0
    elif vr >= 1.3:
        bd["volume"] = 12.0
    elif vr >= A_VOL_MIN:
        bd["volume"] = 8.0
    else:
        bd["volume"] = 0.0
    # --- bar shape: 25 (close position / body / wick) ---
    if close_pos >= 0.80:
        shape = 12.0
    elif close_pos >= 0.65:
        shape = 9.0
    elif close_pos >= A_CLOSE_POS_MIN:
        shape = 6.0
    else:
        shape = 0.0
    if body_ratio >= 0.50:
        shape += 8.0
    elif body_ratio >= 0.35:
        shape += 5.0
    if wick_ratio >= 0.30:
        shape += 5.0
    elif wick_ratio >= 0.20:
        shape += 3.0
    bd["shape"] = min(shape, 25.0)
    # --- RSI dynamics: 15 (recovery + divergence; the level
    # itself is NOT scored and never excludes) ---
    rsi_ev = _rsi_eval(candles, rsi_s, idx, side)
    dyn = 0.0
    if rsi_ev["recovery"] is not None:
        if rsi_ev["recovery"] >= 10.0:
            dyn += 8.0
        elif rsi_ev["recovery"] >= 5.0:
            dyn += 5.0
        elif rsi_ev["recovery"] >= 2.0:
            dyn += 2.0
    if rsi_ev["divergence"]:
        dyn += 7.0
    bd["rsi_dyn"] = min(dyn, 15.0)
    # --- short-term structure shift: 15 ---
    shift_ok = _structure_shift(candles, idx, atr_val, side)
    if shift_ok:
        bd["structure"] = 15.0
    elif idx >= 1 and (
        (side == "LONG" and cd["c"] > candles[idx - 1]["c"])
        or (side == "SHORT" and cd["c"] < candles[idx - 1]["c"])
    ):
        bd["structure"] = 6.0
    else:
        bd["structure"] = 0.0
    # --- OI sharp change: 5 (either direction counts as a flush
    # signature for an absorption candidate; unavailable -> the
    # component is excluded and the score is normalised) ---
    available = dict(_A_SCORE_MAX)
    if oi_change is None:
        bd["oi"] = 0.0
        available.pop("oi")
    elif abs(oi_change) >= 0.03:
        bd["oi"] = 5.0
    elif abs(oi_change) >= 0.015:
        bd["oi"] = 3.0
    else:
        bd["oi"] = 0.0
    # --- funding extreme bias: 5 ---
    if side == "LONG":
        favourable = funding < 0
    else:
        favourable = funding > 0
    if favourable and abs(funding) >= 0.0002:
        bd["funding"] = 5.0
    elif favourable:
        bd["funding"] = 3.0
    else:
        bd["funding"] = 0.0
    raw = sum(bd.values())
    avail_max = sum(available.values())
    norm = (raw / avail_max * 100.0) if avail_max > 0 else 0.0
    return {
        "detected": bool(mandatory), "stage": (
            "absorption_candidate" if mandatory else "weak_bar"),
        "score": norm, "raw_score": raw, "breakdown": bd,
        "vol_ratio": vr, "close_pos": close_pos,
        "rsi_eval": rsi_ev, "shift": shift_ok, "atr": atr_val,
    }


# ============================================================
# system F -- fake break (SHORT = mirror)
# ============================================================

def _eval_f(candles, idx, volumes, atr_s, rsi_s, side,
            oi_change, funding, premise):
    """Fake-break evaluation for one bar.

    Mandatory pattern (LONG): the bar's low breaks the prior
    F_LOOKBACK-bar low by at least F_BREAK_MIN_ATR x ATR, and the
    close recovers back to at least that prior-low level with a
    close position >= F_CLOSE_POS_MIN. A new low that stays low
    is NOT a fake break.
    """
    atr_val = atr_s[idx]
    if atr_val is None or atr_val <= 0 or idx < F_LOOKBACK:
        return {"detected": False, "stage": "insufficient",
                "score": None, "breakdown": None}
    cd = candles[idx]
    rng = max(cd["h"] - cd["l"], 1e-9)
    if side == "LONG":
        prior_extreme = min(
            x["l"] for x in candles[idx - F_LOOKBACK:idx])
        break_depth = (prior_extreme - cd["l"]) / atr_val
        recovered = cd["c"] >= prior_extreme
        reclaim_margin = (cd["c"] - prior_extreme) / atr_val
        close_pos = (cd["c"] - cd["l"]) / rng
    else:
        prior_extreme = max(
            x["h"] for x in candles[idx - F_LOOKBACK:idx])
        break_depth = (cd["h"] - prior_extreme) / atr_val
        recovered = cd["c"] <= prior_extreme
        reclaim_margin = (prior_extreme - cd["c"]) / atr_val
        close_pos = (cd["h"] - cd["c"]) / rng
    if break_depth < F_BREAK_MIN_ATR:
        return {"detected": False, "stage": "no_break",
                "score": None, "breakdown": None,
                "prior_extreme": prior_extreme}
    mandatory = recovered and close_pos >= F_CLOSE_POS_MIN
    # --- break depth: 15 ---
    if break_depth >= 0.30:
        bd = {"break": 15.0}
    elif break_depth >= 0.15:
        bd = {"break": 11.0}
    else:
        bd = {"break": 7.0}
    # --- recovery back above the broken level: 20 ---
    if recovered and reclaim_margin >= 0.10:
        bd["recovery"] = 20.0
    elif recovered:
        bd["recovery"] = 14.0
    else:
        bd["recovery"] = 0.0
    # --- close position: 15 ---
    if close_pos >= 0.80:
        bd["close_pos"] = 15.0
    elif close_pos >= 0.65:
        bd["close_pos"] = 11.0
    elif close_pos >= F_CLOSE_POS_MIN:
        bd["close_pos"] = 7.0
    else:
        bd["close_pos"] = 0.0
    # --- volume anomaly: 15 ---
    vr = _vol_ratio(volumes, idx)
    if vr is None:
        bd["volume"] = 0.0
    elif vr >= 2.0:
        bd["volume"] = 15.0
    elif vr >= 1.5:
        bd["volume"] = 12.0
    elif vr >= 1.3:
        bd["volume"] = 9.0
    elif vr >= 1.2:
        bd["volume"] = 6.0
    else:
        bd["volume"] = 0.0
    # --- structure shift on the recovery: 20 ---
    shift_ok = _structure_shift(candles, idx, atr_val, side)
    if shift_ok:
        bd["structure"] = 20.0
    elif idx >= 1 and (
        (side == "LONG" and cd["c"] > candles[idx - 1]["c"])
        or (side == "SHORT" and cd["c"] < candles[idx - 1]["c"])
    ):
        bd["structure"] = 8.0
    else:
        bd["structure"] = 0.0
    # --- RSI dynamics: 10 ---
    rsi_ev = _rsi_eval(candles, rsi_s, idx, side)
    dyn = 0.0
    if rsi_ev["recovery"] is not None and rsi_ev["recovery"] >= 5.0:
        dyn += 6.0
    if rsi_ev["divergence"]:
        dyn += 4.0
    bd["rsi_dyn"] = min(dyn, 10.0)
    # --- reclaim margin: 5 ---
    if reclaim_margin >= 0.20:
        bd["reclaim"] = 5.0
    elif reclaim_margin >= 0.05:
        bd["reclaim"] = 3.0
    else:
        bd["reclaim"] = 0.0
    raw = sum(bd.values())
    norm = raw  # all components always available (max = 100)
    return {
        "detected": bool(mandatory), "stage": (
            "fake_break" if mandatory else "unrecovered_break"),
        "score": norm, "raw_score": raw, "breakdown": bd,
        "vol_ratio": vr, "close_pos": close_pos,
        "break_depth_atr": break_depth,
        "prior_extreme": prior_extreme,
        "rsi_eval": rsi_ev, "shift": shift_ok, "atr": atr_val,
    }


# ============================================================
# virtual positions (exit logic identical to V1)
# ============================================================

def _fill_pnl(side, qty, entry, exit_px):
    """Net PnL of one fill, fee charged on the fill notional."""
    if side == "LONG":
        gross = qty * (exit_px - entry)
    else:
        gross = qty * (entry - exit_px)
    fee = qty * exit_px * FEE_RATE
    return gross - fee, fee


def _open_virtual(rev2, cand, system, mode, side, bar, atr_val, ev):
    """Open a virtual position at the signal bar's close
    (slippage-adjusted, same convention as V1)."""
    close = bar["c"]
    if side == "LONG":
        entry = close * (1 + SLIPPAGE_RATE)
        stop = cand["extreme"] - STOP_EXTREME_ATR * atr_val
    else:
        entry = close * (1 - SLIPPAGE_RATE)
        stop = cand["extreme"] + STOP_EXTREME_ATR * atr_val
    r_dist = abs(entry - stop)
    if r_dist <= 0 or r_dist > MAX_STOP_ATR * atr_val:
        cand["systems"][system].setdefault(
            "fire_failures", []).append({
                "mode": mode, "bar_t": bar["t"],
                "reason": "invalid_stop_distance",
            })
        return None
    sizing = _size_position(rev2["virtual_equity"], entry, stop)
    if sizing is None:
        cand["systems"][system].setdefault(
            "fire_failures", []).append({
                "mode": mode, "bar_t": bar["t"],
                "reason": "sizing_failed",
            })
        return None
    qty = sizing["quantity"]
    entry_fee = qty * entry * FEE_RATE
    rev2["virtual_equity"] -= entry_fee
    pos = {
        "candidate_id": cand["candidate_id"],
        "system": system,
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
        "score": ev.get("score"),
        "score_breakdown": ev.get("breakdown"),
        "phase": cand["phase"],
        "status": "OPEN",
    }
    rev2["open_positions"].append(pos)
    return pos


def _close_fill(rev2, pos, qty_fill, exit_px, reason, bar):
    pnl, fee = _fill_pnl(pos["side"], qty_fill, pos["entry"], exit_px)
    pos["realized_pnl"] += pnl
    pos["fees_paid"] += fee
    pos["qty_remaining"] = max(0.0, pos["qty_remaining"] - qty_fill)
    rev2["virtual_equity"] += pnl
    return pnl


def _update_stats(rev2, pos, net):
    for key in _bucket_keys_for(pos):
        b = rev2["stats"][key]
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


def _finalize_position(rev2, pos, reason, bar):
    pos["status"] = "CLOSED"
    pos["exit_reason"] = reason
    pos["exit_bar_t"] = bar["t"]
    pos["exit_time"] = _iso(bar["t"] / 1000)
    net = pos["realized_pnl"]
    pos["net_pnl"] = net
    pos["net_r"] = (net / pos["risk_usd"]) if pos["risk_usd"] > 0 else 0.0
    rev2["ledger"].append({
        "kind": "trade",
        "candidate_id": pos["candidate_id"],
        "system": pos["system"],
        "mode": pos["mode"],
        "side": pos["side"],
        "phase": pos["phase"],
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
        "hit_1r_time": (
            _iso(pos["hit_1r_t"] / 1000) if pos["hit_1r_t"] else None),
        "hit_15r_time": (
            _iso(pos["hit_15r_t"] / 1000) if pos["hit_15r_t"] else None),
        "bars_held": pos["bars_held"],
        "fees_paid": pos["fees_paid"],
        "score": pos["score"],
        "score_breakdown": pos["score_breakdown"],
    })
    _update_stats(rev2, pos, net)
    rev2["peak_equity"] = max(
        rev2["peak_equity"], rev2["virtual_equity"])
    rev2["equity_curve"].append({
        "t": bar["t"], "equity": rev2["virtual_equity"],
    })
    if len(rev2["equity_curve"]) > CURVE_LIMIT:
        rev2["equity_curve"] = rev2["equity_curve"][-CURVE_LIMIT:]


def _manage_position(rev2, pos, bar):
    """Advance one virtual position by one closed bar.
    Identical rules to V1. Returns True when fully closed."""
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
        _close_fill(rev2, pos, pos["qty_remaining"], eff_stop * adj,
                    reason, bar)
        _finalize_position(rev2, pos, reason, bar)
        return True

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
            _close_fill(rev2, pos, qty_fill, level * adj,
                        f"TP{level_r}", bar)

    _tp(TP1_R, TP1_FRAC, "tp1_done", "hit_1r_t")
    _tp(TP2_R, TP2_FRAC, "tp2_done", "hit_15r_t")
    if pos["qty_remaining"] <= 1e-12:
        _finalize_position(rev2, pos, "TP_FULL", bar)
        return True

    if side == "LONG":
        if bar["c"] >= entry + TRAIL_START_R * r:
            pos["trail_active"] = True
            cand_t = bar["c"] - TRAIL_ATR_MULT * pos["atr_at_entry"]
            pos["trail_stop"] = max(pos["trail_stop"] or cand_t, cand_t)
    else:
        if bar["c"] <= entry - TRAIL_START_R * r:
            pos["trail_active"] = True
            cand_t = bar["c"] + TRAIL_ATR_MULT * pos["atr_at_entry"]
            pos["trail_stop"] = min(pos["trail_stop"] or cand_t, cand_t)

    if pos["bars_held"] >= TIME_BARS and pos["qty_remaining"] > 0:
        adj = 1 - SLIPPAGE_RATE if side == "LONG" \
            else 1 + SLIPPAGE_RATE
        _close_fill(rev2, pos, pos["qty_remaining"], bar["c"] * adj,
                    "TIME_EXPIRE", bar)
        _finalize_position(rev2, pos, "TIME_EXPIRE", bar)
        return True
    return False


# ============================================================
# candidate lifecycle
# ============================================================

def _new_system_state():
    return {
        "detected": False,
        "first_detected_t": None,
        "best_score": None,
        "best_breakdown": None,
        "best_stage": None,
        "alpha_attempted": False,
        "alpha_fired": False,
        "beta_pending": False,
        "beta_info": None,
        "beta_resolved": False,
        "beta_fired": False,
        "beta_cancel_reason": None,
        "fire_failures": [],
    }


def _new_candidate(rev2, side, bar, phase, premise, mtf):
    tag = "L" if side == "LONG" else "S"
    cand = {
        "candidate_id": f"REV2-{tag}-{bar['t']}",
        "side": side,
        "phase": phase,
        "started_bar_t": bar["t"],
        "start_time": _iso(bar["t"] / 1000),
        "ref_price": bar["c"],
        "premise": {
            "pct": premise["pct"], "width": premise["width"],
            "peak": premise["peak"], "trough": premise["trough"],
        },
        "extreme": premise["extreme"],
        "bars_active": 0,
        "track_high": bar["h"],
        "track_low": bar["l"],
        "fav16": 0.0,          # best favourable % within 16 bars
        "adv16": 0.0,          # worst adverse % within 16 bars
        "outcome": None,       # reversal / failed / none (16 bars)
        "returns": {
            "ret_15m_pct": None, "ret_30m_pct": None,
            "ret_1h_pct": None, "ret_4h_pct": None,
        },
        "systems": {s: _new_system_state() for s in SYSTEMS},
        "fired_any": False,
        "pre_alert": None,
        "mtf_at_start": mtf,
        "trend_1h": None,
        "trend_4h": None,
    }
    rev2["candidates_total"] += 1
    rev2["active_candidates"][side] = cand
    return cand


def _candidate_returns(cand, bar):
    """Fill close-to-close forward returns at fixed offsets."""
    n = cand["bars_active"]  # 1-based count incl. current bar
    ref = cand["ref_price"]
    if not ref:
        return
    pct = (bar["c"] / ref - 1.0) * 100.0
    if cand["side"] == "SHORT":
        pct = -pct
    mapping = {1: "ret_15m_pct", 2: "ret_30m_pct",
               4: "ret_1h_pct", 16: "ret_4h_pct"}
    key = mapping.get(n)
    if key and cand["returns"][key] is None:
        cand["returns"][key] = pct


def _candidate_outcome(cand, bar):
    """Track favourable / adverse excursion and the fixed
    reversal outcome over the first REVERSAL_HORIZON_BARS bars.
    Judgements are never revised afterwards."""
    if cand["bars_active"] > REVERSAL_HORIZON_BARS:
        return
    ref = cand["ref_price"]
    if not ref:
        return
    if cand["side"] == "LONG":
        fav = (bar["h"] / ref - 1.0) * 100.0
        adv = (bar["l"] / ref - 1.0) * 100.0
    else:
        fav = (1.0 - bar["l"] / ref) * 100.0
        adv = (1.0 - bar["h"] / ref) * 100.0
    cand["fav16"] = max(cand["fav16"], fav)
    cand["adv16"] = min(cand["adv16"], adv)
    if cand["outcome"] is None:
        # conservative: an adverse breach inside the same bar is
        # resolved first
        if adv <= -REVERSAL_INVALID_PCT * 100.0:
            cand["outcome"] = "failed"
        elif fav >= REVERSAL_TARGET_PCT * 100.0:
            cand["outcome"] = "reversal"


def _fire_alpha(rev2, cand, system, side, bar, atr_val, ev):
    st = cand["systems"][system]
    st["alpha_attempted"] = True
    pos = _open_virtual(rev2, cand, system, "alpha", side, bar,
                        atr_val, ev)
    if pos is not None:
        st["alpha_fired"] = True
        cand["fired_any"] = True
    return pos


def _arm_beta(cand, system, side, bar, ev):
    st = cand["systems"][system]
    st["beta_pending"] = True
    st["beta_info"] = {
        "bar_t": bar["t"],
        "signal_extreme_bar": (
            bar["h"] if side == "LONG" else bar["l"]),
        "bars": 0,
        "score": ev.get("score"),
        "breakdown": ev.get("breakdown"),
        "atr": ev.get("atr"),
    }


def _progress_beta(rev2, cand, system, side, bar, atr_val):
    st = cand["systems"][system]
    info = st["beta_info"]
    if not st["beta_pending"] or not info or bar["t"] == info["bar_t"]:
        return
    info["bars"] += 1
    atr_use = atr_val or info["atr"] or 0.0
    tol = 0.05 * atr_use
    if side == "LONG":
        invalid = bar["l"] < cand["extreme"] - STOP_EXTREME_ATR * atr_use
        confirmed = (
            bar["c"] > info["signal_extreme_bar"]
            and bar["l"] > cand["extreme"] - tol
        )
    else:
        invalid = bar["h"] > cand["extreme"] + STOP_EXTREME_ATR * atr_use
        confirmed = (
            bar["c"] < info["signal_extreme_bar"]
            and bar["h"] < cand["extreme"] + tol
        )
    if invalid:
        st["beta_pending"] = False
        st["beta_resolved"] = True
        st["beta_cancel_reason"] = "beta_invalidated"
    elif confirmed:
        ev_beta = {
            "score": info["score"], "breakdown": info["breakdown"],
            "atr": info["atr"],
        }
        pos = _open_virtual(
            rev2, cand, system, "beta", side, bar,
            atr_val or info["atr"], ev_beta)
        st["beta_pending"] = False
        st["beta_resolved"] = True
        if pos is not None:
            st["beta_fired"] = True
            cand["fired_any"] = True
        else:
            st["beta_cancel_reason"] = "beta_fire_failed"
    elif info["bars"] > BETA_CONFIRM_BARS:
        st["beta_pending"] = False
        st["beta_resolved"] = True
        st["beta_cancel_reason"] = "beta_unconfirmed"


def _system_not_fired_reasons(cand):
    reasons = {}
    for system in SYSTEMS:
        st = cand["systems"][system]
        if st["alpha_fired"] or st["beta_fired"]:
            continue
        r = []
        if not st["detected"]:
            r.append("pattern_not_detected")
        else:
            if st["best_score"] is not None \
                    and st["best_score"] < _ALPHA[system]:
                r.append("score_below_alpha")
            if st["alpha_attempted"]:
                r.append("alpha_fire_failed")
            if st["beta_cancel_reason"]:
                r.append(st["beta_cancel_reason"])
            elif st["best_score"] is not None \
                    and st["best_score"] >= _BETA[system]:
                r.append("beta_not_armed_or_expired")
        if st["fire_failures"]:
            r.append("fire_failed")
        reasons[system] = r or ["not_fired"]
    return reasons


def _close_candidate(rev2, cand):
    side = cand["side"]
    ref = cand["ref_price"]
    if side == "LONG":
        mfe = (cand["track_high"] / ref - 1.0) * 100 if ref else 0.0
        mae = (cand["track_low"] / ref - 1.0) * 100 if ref else 0.0
    else:
        mfe = (1.0 - cand["track_low"] / ref) * 100 if ref else 0.0
        mae = (1.0 - cand["track_high"] / ref) * 100 if ref else 0.0
    outcome = cand["outcome"] or "none"
    record = {
        "kind": "candidate",
        "candidate_id": cand["candidate_id"],
        "side": side,
        "phase": cand["phase"],
        "start_time": cand["start_time"],
        "ref_price": ref,
        "premise": cand["premise"],
        "extreme": cand["extreme"],
        "bars_tracked": cand["bars_active"],
        "fired_any": cand["fired_any"],
        "systems": {
            s: {
                "detected": cand["systems"][s]["detected"],
                "best_score": cand["systems"][s]["best_score"],
                "best_breakdown": cand["systems"][s]["best_breakdown"],
                "best_stage": cand["systems"][s]["best_stage"],
                "alpha_fired": cand["systems"][s]["alpha_fired"],
                "beta_fired": cand["systems"][s]["beta_fired"],
            } for s in SYSTEMS
        },
        "not_fired_reasons": _system_not_fired_reasons(cand),
        "returns": cand["returns"],
        "track_mfe_pct": mfe,
        "track_mae_pct": mae,
        "fav16_pct": cand["fav16"],
        "adv16_pct": cand["adv16"],
        "outcome": outcome,
        "pre_alert": cand["pre_alert"],
        "mtf_at_start": cand["mtf_at_start"],
        "trend_1h": cand["trend_1h"],
        "trend_4h": cand["trend_4h"],
    }
    rev2["ledger"].append(record)
    # --- detection aggregates (candidate counted once) ---
    det = rev2["detection"][cand["phase"]]
    det["candidates"] += 1
    if cand["fired_any"]:
        det["fired"] += 1
        if outcome != "reversal":
            det["false_fires"] += 1
    if outcome == "reversal":
        det["reversals"] += 1
        if cand["fired_any"]:
            det["captured"] += 1
        else:
            det["missed"] += 1
            for thr, key in (
                (1.0, "miss_mfe_1pct"), (2.0, "miss_mfe_2pct"),
                (3.0, "miss_mfe_3pct"),
            ):
                if cand["fav16"] >= thr:
                    det[key] += 1
    for s in SYSTEMS:
        st = cand["systems"][s]
        ds = det["systems"][s]
        if st["detected"]:
            ds["detected"] += 1
        if st["alpha_fired"] or st["beta_fired"]:
            ds["fired"] += 1
            if outcome == "reversal":
                ds["captured"] += 1
    rev2["active_candidates"][side] = None


def _process_side(rev2, candles, idx, volumes, atr_s, rsi_s, side,
                  oi_change, funding, phase, trend_1h, trend_4h):
    bar = candles[idx]
    atr_val = atr_s[idx]
    cand = rev2["active_candidates"].get(side)
    premise = _premise(candles, idx, atr_val, side)
    if cand is None:
        if premise is None or not premise["ok"]:
            return
        cand = _new_candidate(
            rev2, side, bar, phase, premise,
            _mtf_snapshot(candles, idx),
        )
        cand["trend_1h"] = trend_1h
        cand["trend_4h"] = trend_4h

    cand["bars_active"] += 1
    cand["track_high"] = max(cand["track_high"], bar["h"])
    cand["track_low"] = min(cand["track_low"], bar["l"])
    if side == "LONG":
        cand["extreme"] = min(cand["extreme"], bar["l"])
    else:
        cand["extreme"] = max(cand["extreme"], bar["h"])
    _candidate_returns(cand, bar)
    _candidate_outcome(cand, bar)

    evals = {
        "L": _eval_l(candles, idx, volumes, atr_s, rsi_s, side,
                     oi_change, funding),
        "A": _eval_a(candles, idx, volumes, atr_s, rsi_s, side,
                     oi_change, funding, premise),
        "F": _eval_f(candles, idx, volumes, atr_s, rsi_s, side,
                     oi_change, funding, premise),
    }
    for system in SYSTEMS:
        ev = evals[system]
        if ev is None:
            continue
        st = cand["systems"][system]
        if ev.get("detected"):
            if not st["detected"]:
                st["first_detected_t"] = bar["t"]
            st["detected"] = True
        if ev.get("score") is not None:
            if st["best_score"] is None \
                    or ev["score"] > st["best_score"]:
                st["best_score"] = ev["score"]
                st["best_breakdown"] = ev.get("breakdown")
                st["best_stage"] = ev.get("stage")
        if ev.get("detected"):
            ev_full = dict(ev)
            ev_full["atr"] = ev.get("atr") or atr_val
            if not st["alpha_attempted"] \
                    and ev["score"] is not None \
                    and ev["score"] >= _ALPHA[system]:
                _fire_alpha(rev2, cand, system, side, bar,
                            ev_full["atr"], ev_full)
            if not st["beta_fired"] and not st["beta_pending"] \
                    and not st["beta_resolved"] \
                    and ev["score"] is not None \
                    and ev["score"] >= _BETA[system]:
                _arm_beta(cand, system, side, bar, ev_full)
        # beta progression runs on later bars regardless of
        # whether this bar re-detected the pattern
        _progress_beta(rev2, cand, system, side, bar, atr_val)

    # --- PRE-ALERT: premise + volume substantially formed on
    # closed bars, nothing confirmed yet. Recorded once per
    # candidate; never a fire, never a trade. ---
    if cand["pre_alert"] is None and not cand["fired_any"]:
        vr = _vol_ratio(volumes, idx)
        best = max(
            (cand["systems"][s]["best_score"] or 0.0 for s in SYSTEMS),
            default=0.0,
        )
        if (premise is not None and premise["ok"]
                and vr is not None and vr >= A_VOL_MIN) \
                or best >= 40.0:
            cand["pre_alert"] = {
                "bar_t": bar["t"],
                "time": _iso(bar["t"] / 1000),
                "price": bar["c"],
                "vol_ratio": vr,
                "best_score_so_far": best,
                "note": "closed-bar conditions substantially "
                        "formed; awaiting confirmation",
            }
            rev2["pre_alerts"].append({
                "candidate_id": cand["candidate_id"],
                "side": side, "phase": cand["phase"],
                **cand["pre_alert"],
            })
            if len(rev2["pre_alerts"]) > PREALERT_LIMIT:
                rev2["pre_alerts"] = rev2["pre_alerts"][-PREALERT_LIMIT:]

    if cand["bars_active"] >= CANDIDATE_MAX_BARS:
        _close_candidate(rev2, cand)


# ============================================================
# public entry points (called from bot.py main cycle)
# ============================================================

def _oi_change_1h(rev2):
    hist = rev2.get("oi_history") or []
    if len(hist) < OI_MIN_SAMPLES:
        return None
    ref = _f(hist[-OI_MIN_SAMPLES].get("oi"))
    now = _f(hist[-1].get("oi"))
    if ref <= 0 or now <= 0:
        return None
    return (now - ref) / ref


def _update(rev2, state, c15, c1, c4, market, ctx):
    if not isinstance(market, dict):
        market = {}
    if not isinstance(ctx, dict):
        ctx = {}
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
        hist = rev2["oi_history"]
        if not hist or hist[-1].get("ts", 0) < last_t // 1000:
            hist.append({"ts": last_t // 1000, "oi": oi_now})
            rev2["oi_history"] = hist[-OI_HISTORY_MAX:]
    oi_change = _oi_change_1h(rev2)

    first_call = not rev2.get("initialized")
    phase = "retro" if first_call else "forward"
    new_idx = [
        i for i, cd in enumerate(c15) if cd["t"] > rev2["last_bar_t"]
    ]
    for idx in new_idx:
        bar = c15[idx]
        still_open = []
        for pos in rev2["open_positions"]:
            if _manage_position(rev2, pos, bar):
                continue
            still_open.append(pos)
        rev2["open_positions"] = still_open
        for side in ("LONG", "SHORT"):
            _process_side(
                rev2, c15, idx, volumes, atr_s, rsi_s, side,
                oi_change, funding, phase, trend_1h, trend_4h,
            )
        rev2["last_bar_t"] = bar["t"]

    if first_call and new_idx:
        rev2["initialized"] = True
        rev2["forward_start_t"] = rev2["last_bar_t"]
    if len(rev2["ledger"]) > LEDGER_LIMIT:
        rev2["ledger"] = rev2["ledger"][-LEDGER_LIMIT:]
    rev2["last_error"] = None
    rev2["updated_at"] = _iso(last_t / 1000)
    return {
        "ok": True,
        "bars": len(new_idx),
        "phase": phase,
        "open_positions": len(rev2["open_positions"]),
        "candidates_total": rev2["candidates_total"],
        "virtual_equity": rev2["virtual_equity"],
    }


def update_rev2(state, c15, c1, c4, market, ctx):
    """Advance the REV2 shadow engine by one house cycle.

    Isolation contract: works on a deep copy of state["rev2"] and
    commits it back only on success; on any error the house state
    (and state["rev"], owned by V1) is left untouched. Never
    raises.
    """
    try:
        _ensure_rev2(state)
        work = copy.deepcopy(state["rev2"])
        summary = _update(work, state, c15, c1, c4, market, ctx)
        state["rev2"] = work
        return summary
    except Exception as exc:  # noqa: BLE001 - isolation by design
        try:
            cur = state.get("rev2")
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


def _detection_view(det):
    rev = det["reversals"]
    fired = det["fired"]
    return {
        "candidates": det["candidates"],
        "fired_candidates": fired,
        "reversals": rev,
        "captured": det["captured"],
        "missed": det["missed"],
        "false_fires": det["false_fires"],
        "capture_rate": (
            det["captured"] / rev if rev else None),
        "miss_rate": (det["missed"] / rev if rev else None),
        "false_fire_rate": (
            det["false_fires"] / fired if fired else None),
        "miss_mfe_buckets": {
            "ge_1pct": det["miss_mfe_1pct"],
            "ge_2pct": det["miss_mfe_2pct"],
            "ge_3pct": det["miss_mfe_3pct"],
        },
        "systems": det["systems"],
    }


def dashboard_payload(state):
    """Read-only REV2 summary for docs/data.json (fully separated
    from the house sections and from data.json["rev"] of V1)."""
    rev2 = state.get("rev2") if isinstance(state, dict) else None
    base = {
        "enabled": True,
        "version": REV2_VERSION,
        "definition_version": DEFINITION_VERSION,
        "measurement_only": True,
        "note": "REV2 shadow: virtual trades only; no effect on "
                "house positions, equity or risk. Statistics are "
                "separate from REV V1 (data.json['rev']).",
        "systems": {
            "L": "legacy pressure-peak (V1 definitions, "
                 "alpha 65 / beta 75)",
            "A": "bullish absorption CANDIDATE pattern "
                 "(alpha 60 / beta 72)",
            "F": "fake-break recovery pattern "
                 "(alpha 60 / beta 72)",
        },
        "reversal_definition": (
            "within 16 bars after detection: +2.0% favourable "
            "before -1.0% adverse (same-bar ties resolve adverse)"
        ),
    }
    if not isinstance(rev2, dict) or "stats" not in rev2:
        base["initialized"] = False
        return base
    start = rev2.get("start_equity") or 100.0
    stats = {
        k: _bucket_view(b, start) for k, b in rev2["stats"].items()
    }
    peak = rev2.get("start_equity") or 0.0
    max_dd_pct = 0.0
    for pt in rev2.get("equity_curve", []):
        eq = _f(pt.get("equity"))
        peak = max(peak, eq)
        if peak > 0:
            max_dd_pct = max(
                max_dd_pct, (peak - eq) / peak * 100.0)
    stats["ALL"]["max_dd_pct"] = max_dd_pct
    stats["ALL"]["stage0"]["dd_ok"] = max_dd_pct <= 15.0
    stats["ALL"]["stage0"]["passed"] = (
        stats["ALL"]["stage0"]["trades_ok"]
        and stats["ALL"]["stage0"]["expectancy_ok"]
        and stats["ALL"]["stage0"]["pf_ok"]
        and stats["ALL"]["stage0"]["dd_ok"]
    )
    base.update({
        "initialized": bool(rev2.get("initialized")),
        "forward_start_time": (
            _iso(rev2["forward_start_t"] / 1000)
            if rev2.get("forward_start_t") else None),
        "virtual_equity": rev2.get("virtual_equity"),
        "start_equity": start,
        "candidates_total": rev2.get("candidates_total", 0),
        "open_positions": [
            {
                "candidate_id": p["candidate_id"],
                "system": p["system"], "mode": p["mode"],
                "side": p["side"], "entry": p["entry"],
                "bars_held": p["bars_held"],
                "mfe_r": p["mfe_r"], "mae_r": p["mae_r"],
                "score": p["score"],
            }
            for p in rev2.get("open_positions", [])
        ],
        "active_candidates": {
            k: (
                {
                    "candidate_id": v["candidate_id"],
                    "phase": v["phase"],
                    "bars_active": v["bars_active"],
                    "fired_any": v["fired_any"],
                    "best_scores": {
                        s: v["systems"][s]["best_score"]
                        for s in SYSTEMS
                    },
                } if v else None
            )
            for k, v in rev2.get("active_candidates", {}).items()
        },
        "detection": {
            phase: _detection_view(rev2["detection"][phase])
            for phase in ("retro", "forward")
        },
        "stats": stats,
        "pre_alerts_recent": list(
            reversed(rev2.get("pre_alerts", [])[-5:])),
        "recent_ledger": list(reversed(rev2.get("ledger", [])[-8:])),
        "last_bar_time": (
            _iso(rev2["last_bar_t"] / 1000)
            if rev2.get("last_bar_t") else None),
        "updated_at": rev2.get("updated_at"),
        "last_error": rev2.get("last_error"),
    })
    return base
