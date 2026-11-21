"""REV ENGINE evaluation layer (V20.18, measurement only).

Purpose
-------
REV V1 (rev_engine.py) and REV V2 (rev_engine_v2.py) already
detect reversal candidates and run virtual trades.  What they do
not provide is a single, separated view that answers two different
questions independently:

  1. Detection quality -- when a system fired (or did not fire),
     did price actually reverse afterwards?
  2. Profitability -- did the virtual trades that belong to that
     detection actually make money (in R)?

This module is that view.  It is a pure observer:

  * It NEVER changes V1 / V2 detection logic, thresholds or the
    house trading logic.  It only reads state["rev"] and
    state["rev2"] ledgers (read-only) plus the confirmed 15m
    candles the house already fetched.
  * It writes only state["rev_eval"] and exposes
    dashboard_payload() for data.json["rev_eval"].
  * Detection-time facts (score, price, thresholds, era, regime)
    are snapshotted once, when a source record is first ingested,
    and are never rewritten afterwards.  Only forward-tracking
    fields (horizon returns, MFE / MAE, outcome) are filled in as
    new confirmed bars arrive, and a record is frozen
    ("finalized") once its full tracking window has been observed.
  * Unfinalized records are excluded from every win/loss
    aggregate.

Fixed definitions (REV_EVAL_DEF_2026-11-22_V1 -- do not change
retroactively; a change means a new definition version):

  * "Reversal" outcome: inherited verbatim from REV V2's fixed
    definition -- within 16 bars (4h) after the detection bar,
    price first reaches +2.0% in the trade direction before
    -1.0% adverse.  If both happen inside one bar, the adverse
    side wins (conservative).  If neither happens, outcome is
    "none".  Judgements are never revised.
  * Tracking window: bars detect+1 .. detect+16 (15m bars),
    the same convention REV V2 uses for its candidate returns
    (ret_15m is the close one bar after the start bar).
  * Horizons: +15m = 1 bar, +30m = 2 bars, +60m = 4 bars,
    +120m = 8 bars.  Per horizon we store the close-to-close
    directional return and whether the reversal target had
    already been reached by that horizon (first passage).
  * MFE / MAE: maximum favourable / adverse excursion (%) over
    the tracking window, plus the time (minutes from detection)
    at which each extreme was first reached.
  * Categories (each eval record belongs to exactly one):
      A_ONLY   V2 candidate fired REV-A but not REV-F
      F_ONLY   V2 candidate fired REV-F but not REV-A
      AF_BOTH  V2 candidate fired both REV-A and REV-F
      L_ONLY   V2 candidate fired only REV-L (supplementary;
               REV-L's authoritative statistics remain V1's)
      V1       a fired REV V1 event (virtual trade in V1 ledger)
      NONE     nothing fired (V2 candidate with no system fire,
               or an unfilled V1 event)
  * Regime at detection (confirmed data only, from 15m bars):
      trend:  chg_1h = close vs close 4 bars earlier,
              chg_4h = close vs close 16 bars earlier.
              UP   if chg_1h >= +0.30% and chg_4h >= +0.50%
              DOWN if chg_1h <= -0.30% and chg_4h <= -0.50%
              RANGE otherwise.
      vol:    ATR(14) on 15m bars (simple average of true
              range) as % of close.
              HIGH if >= 0.45%, LOW if <= 0.25%, else MID.
      Records whose detection bar has < 16 bars of history are
      labelled UNKNOWN and excluded from regime aggregates.
  * Era separation: V2 records carry their own phase
    ("retro" / "forward") which is copied verbatim.  V1 has no
    era field; V1's retro backfill is separated by time --
    V1 went live at 2026-11-21T14:30:00Z and its first cycle
    processed the whole fetched window, so V1 detections with
    detect_t < 1795271400000 ms are "retro", the rest
    "forward".  Aggregates are always reported per era and
    never pooled.
  * Statistics: expectancy CI is the normal-approximation 95%
    confidence interval of the mean net R,
        mean +/- 1.96 * (sample_stdev / sqrt(n)),  n >= 2,
    with the sample stdev using ddof=1.  The success-rate CI is
    the Wilson score interval (z = 1.96).  Profit factor is
    R-based: sum(positive net R) / |sum(negative net R)|.
    A trade is a win iff net_r > 0; max consecutive losses
    counts runs of net_r < 0 in detection-time order.

Isolation contract (same as V1 / V2): update_eval() works on a
deep copy of state["rev_eval"], commits only on success, and
never raises.
"""

import copy
import math
from datetime import datetime, timezone

EVAL_VERSION = "REV_EVAL_V1"
EVAL_DEFINITION_VERSION = "REV_EVAL_DEF_2026-11-22_V1"

# --- fixed evaluation definitions (see docstring) ---
TRACK_BARS = 16
REVERSAL_TARGET_PCT = 2.0
REVERSAL_INVALID_PCT = 1.0
HORIZON_BARS = {"15m": 1, "30m": 2, "60m": 4, "120m": 8}
TREND_1H_MIN_PCT = 0.30
TREND_4H_MIN_PCT = 0.50
VOL_HIGH_ATR_PCT = 0.45
VOL_LOW_ATR_PCT = 0.25
ATR_PERIOD = 14

# V1 live start: first V20.16 cycle ran 2026-11-21T14:30:00Z and
# backfilled the fetched window; detections before this are retro.
V1_FORWARD_START_T = 1795271400000

# Thresholds in force at detection time, snapshotted onto every
# record so later threshold changes never rewrite history.
THRESHOLDS_SNAPSHOT = {
    "L": {"alpha": 65.0, "beta": 75.0},
    "A": {"alpha": 60.0, "beta": 72.0},
    "F": {"alpha": 60.0, "beta": 72.0},
    "V1": {"alpha": 65.0, "beta": 75.0},
}

CATEGORIES = ("A_ONLY", "F_ONLY", "AF_BOTH", "L_ONLY", "V1", "NONE")
RECORD_LIMIT = 1200
SEEN_LIMIT = 4000
PENDING_TRADE_LIMIT = 400


# ============================================================
# small helpers
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


def _parse_iso_ms(text):
    """Parse an ISO timestamp produced by the engines' _iso()
    (e.g. '2026-11-21T14:15:00+00:00') into epoch milliseconds.
    Returns None when unparseable."""
    if not text or not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ============================================================
# state
# ============================================================

def _new_eval_state():
    return {
        "version": EVAL_VERSION,
        "definition_version": EVAL_DEFINITION_VERSION,
        "enabled": True,
        "records": [],
        "seen": {
            "v2_candidates": [],
            "v2_trades": [],
            "v1_trades": [],
            "v1_unfilled": [],
        },
        "pending_trades": {},   # candidate_id -> [trade summaries]
        "last_bar_t": 0,
        "last_error": None,
        "updated_at": None,
    }


def _ensure_eval(state):
    ev = state.get("rev_eval")
    if not isinstance(ev, dict) or "records" not in ev:
        ev = _new_eval_state()
        state["rev_eval"] = ev
    return ev


def _seen_add(ev, bucket, key):
    seen = ev["seen"][bucket]
    if key not in seen:
        seen.append(key)
        if len(seen) > SEEN_LIMIT:
            ev["seen"][bucket] = seen[-SEEN_LIMIT:]


# ============================================================
# regime classification (detection bar, confirmed data only)
# ============================================================

def _atr_pct_at(candles, idx, period=ATR_PERIOD):
    if idx < period or idx >= len(candles):
        return None
    trs = []
    for i in range(idx - period + 1, idx + 1):
        bar = candles[i]
        prev_close = candles[i - 1]["c"] if i > 0 else bar["c"]
        tr = max(
            bar["h"] - bar["l"],
            abs(bar["h"] - prev_close),
            abs(bar["l"] - prev_close),
        )
        trs.append(tr)
    close = candles[idx]["c"]
    if not close:
        return None
    return (sum(trs) / len(trs)) / close * 100.0


def _regime_at(candles, idx):
    """Fixed regime labels for the detection bar (see module
    docstring).  UNKNOWN when history is insufficient."""
    if idx is None or idx < TRACK_BARS or idx >= len(candles):
        return {"trend": "UNKNOWN", "vol": "UNKNOWN",
                "chg_1h_pct": None, "chg_4h_pct": None,
                "atr_pct": None}
    close = candles[idx]["c"]
    if not close:
        return {"trend": "UNKNOWN", "vol": "UNKNOWN",
                "chg_1h_pct": None, "chg_4h_pct": None,
                "atr_pct": None}
    chg_1h = (close / candles[idx - 4]["c"] - 1.0) * 100.0
    chg_4h = (close / candles[idx - 16]["c"] - 1.0) * 100.0
    if chg_1h >= TREND_1H_MIN_PCT and chg_4h >= TREND_4H_MIN_PCT:
        trend = "UP"
    elif chg_1h <= -TREND_1H_MIN_PCT and chg_4h <= -TREND_4H_MIN_PCT:
        trend = "DOWN"
    else:
        trend = "RANGE"
    atr_pct = _atr_pct_at(candles, idx)
    if atr_pct is None:
        vol = "UNKNOWN"
    elif atr_pct >= VOL_HIGH_ATR_PCT:
        vol = "HIGH"
    elif atr_pct <= VOL_LOW_ATR_PCT:
        vol = "LOW"
    else:
        vol = "MID"
    return {"trend": trend, "vol": vol,
            "chg_1h_pct": chg_1h, "chg_4h_pct": chg_4h,
            "atr_pct": atr_pct}


# ============================================================
# forward tracking over the fixed 16-bar window
# ============================================================

def _track(candles, idx, side, ref_price):
    """Compute horizon returns, MFE / MAE (+ time to each) and
    the fixed-definition outcome from bars idx+1 .. idx+16.

    Returns None when the detection bar itself is unavailable.
    Fields that need bars beyond the available history stay None;
    "complete" is True only when all 16 bars were observed."""
    if idx is None or idx < 0 or idx >= len(candles) or not ref_price:
        return None
    horizons = {k: {"ret_pct": None, "success": None}
                for k in HORIZON_BARS}
    mfe = None
    mae = None
    t_mfe = None
    t_mae = None
    outcome = None
    outcome_bar = None
    observed = 0
    for step in range(1, TRACK_BARS + 1):
        j = idx + step
        if j >= len(candles):
            break
        bar = candles[j]
        observed = step
        if side == "LONG":
            fav = (bar["h"] / ref_price - 1.0) * 100.0
            adv = (bar["l"] / ref_price - 1.0) * 100.0
        else:
            fav = (1.0 - bar["l"] / ref_price) * 100.0
            adv = (1.0 - bar["h"] / ref_price) * 100.0
        if mfe is None or fav > mfe:
            mfe = fav
            t_mfe = step * 15
        if mae is None or adv < mae:
            mae = adv
            t_mae = step * 15
        if outcome is None:
            # conservative: adverse breach inside the same bar
            # is resolved first (same rule as REV V2).
            if adv <= -REVERSAL_INVALID_PCT:
                outcome = "failed"
                outcome_bar = step
            elif fav >= REVERSAL_TARGET_PCT:
                outcome = "reversal"
                outcome_bar = step
        for key, bars in HORIZON_BARS.items():
            if step == bars:
                ret = (bar["c"] / ref_price - 1.0) * 100.0
                if side == "SHORT":
                    ret = -ret
                horizons[key]["ret_pct"] = ret
                if outcome is not None and outcome_bar <= bars:
                    horizons[key]["success"] = (
                        outcome == "reversal"
                    )
    complete = observed >= TRACK_BARS
    if complete:
        if outcome is None:
            outcome = "none"
        for key, bars in HORIZON_BARS.items():
            if horizons[key]["success"] is None:
                # window fully observed and the target was not
                # reached by this horizon -> definitively False
                horizons[key]["success"] = bool(
                    outcome == "reversal" and outcome_bar is not None
                    and outcome_bar <= bars
                )
    return {
        "horizons": horizons,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "t_mfe_min": t_mfe,
        "t_mae_min": t_mae,
        "outcome": outcome,
        "outcome_bar": outcome_bar,
        "bars_observed": observed,
        "complete": complete,
    }


def _apply_tracking(rec, candles, idx):
    tr = _track(candles, idx, rec["side"], rec["ref_price"])
    if tr is None:
        return
    rec["horizons"] = tr["horizons"]
    rec["mfe_pct"] = tr["mfe_pct"]
    rec["mae_pct"] = tr["mae_pct"]
    rec["t_mfe_min"] = tr["t_mfe_min"]
    rec["t_mae_min"] = tr["t_mae_min"]
    rec["outcome"] = tr["outcome"]
    rec["bars_observed"] = tr["bars_observed"]
    if tr["complete"]:
        rec["tracking_complete"] = True
        rec["finalized"] = True


# ============================================================
# record construction (detection facts are written once)
# ============================================================

def _base_record(rec_id, source, category, era, side, detect_t,
                 ref_price):
    return {
        "rec_id": rec_id,
        "source": source,
        "category": category,
        "era": era,
        "side": side,
        "detect_t": detect_t,
        "detect_time": _iso(detect_t / 1000) if detect_t else None,
        "ref_price": ref_price,
        "scores": {},
        "thresholds": copy.deepcopy(THRESHOLDS_SNAPSHOT),
        "fired": {},
        "regime": {"trend": "UNKNOWN", "vol": "UNKNOWN",
                   "chg_1h_pct": None, "chg_4h_pct": None,
                   "atr_pct": None},
        "horizons": {k: {"ret_pct": None, "success": None}
                     for k in HORIZON_BARS},
        "mfe_pct": None,
        "mae_pct": None,
        "t_mfe_min": None,
        "t_mae_min": None,
        "outcome": None,
        "bars_observed": 0,
        "tracking_complete": False,
        "finalized": False,
        "tracking_lost": False,
        "trades": [],
    }


def _classify_v2_candidate(ledger_rec):
    systems = ledger_rec.get("systems") or {}

    def fired(sys_key):
        st = systems.get(sys_key) or {}
        return bool(st.get("alpha_fired") or st.get("beta_fired"))

    fa, ff, fl = fired("A"), fired("F"), fired("L")
    if fa and ff:
        category = "AF_BOTH"
    elif fa:
        category = "A_ONLY"
    elif ff:
        category = "F_ONLY"
    elif fl:
        category = "L_ONLY"
    else:
        category = "NONE"
    return category, {"L": fl, "A": fa, "F": ff}


def _trade_summary(ledger_rec, with_system):
    out = {
        "mode": ledger_rec.get("mode"),
        "side": ledger_rec.get("side"),
        "entry_time": ledger_rec.get("entry_time"),
        "exit_time": ledger_rec.get("exit_time"),
        "exit_reason": ledger_rec.get("exit_reason"),
        "net_r": _f(ledger_rec.get("net_r")),
        "net_pnl": _f(ledger_rec.get("net_pnl")),
        "score": ledger_rec.get("score"),
    }
    if with_system:
        out["system"] = ledger_rec.get("system")
    return out


# ============================================================
# ingestion from the V1 / V2 ledgers (read-only on their side)
# ============================================================

def _find_bar_idx(candles, bar_t):
    if bar_t is None:
        return None
    # candle lists are chronological; linear scan is fine at
    # this size and robust to unsorted edge cases.
    for i, cd in enumerate(candles):
        if cd.get("t") == bar_t:
            return i
    return None


def _ingest_v2(ev, state, candles):
    rev2 = state.get("rev2")
    if not isinstance(rev2, dict):
        return
    ledger = rev2.get("ledger")
    if not isinstance(ledger, list):
        return
    by_candidate = {
        r["link_id"]: r for r in ev["records"]
        if r["source"] == "V2_CANDIDATE"
    }
    for rec in ledger:
        if not isinstance(rec, dict):
            continue
        kind = rec.get("kind")
        if kind == "candidate":
            cid = rec.get("candidate_id")
            if not cid or cid in ev["seen"]["v2_candidates"]:
                continue
            _seen_add(ev, "v2_candidates", cid)
            detect_t = _parse_iso_ms(rec.get("start_time"))
            category, fired = _classify_v2_candidate(rec)
            systems = rec.get("systems") or {}
            new = _base_record(
                "V2:" + cid, "V2_CANDIDATE", category,
                rec.get("phase") or "forward",
                rec.get("side") or "LONG", detect_t,
                _f(rec.get("ref_price")),
            )
            new["link_id"] = cid
            new["scores"] = {
                s: (systems.get(s) or {}).get("best_score")
                for s in ("L", "A", "F")
            }
            new["fired"] = fired
            idx = _find_bar_idx(candles, detect_t)
            new["regime"] = _regime_at(candles, idx)
            _apply_tracking(new, candles, idx)
            # trades that closed before the candidate record
            # itself was written are parked in pending_trades
            parked = ev["pending_trades"].pop(cid, None)
            if parked:
                new["trades"].extend(parked)
            ev["records"].append(new)
            by_candidate[cid] = new
        elif kind == "trade":
            cid = rec.get("candidate_id")
            key = "|".join([
                str(cid), str(rec.get("system")),
                str(rec.get("mode")), str(rec.get("entry_time")),
            ])
            if key in ev["seen"]["v2_trades"]:
                continue
            _seen_add(ev, "v2_trades", key)
            summary = _trade_summary(rec, with_system=True)
            target = by_candidate.get(cid)
            if target is not None:
                target["trades"].append(summary)
            else:
                parked = ev["pending_trades"].setdefault(cid, [])
                parked.append(summary)
                if len(ev["pending_trades"]) > PENDING_TRADE_LIMIT:
                    oldest = next(iter(ev["pending_trades"]))
                    del ev["pending_trades"][oldest]


def _v1_era(detect_t):
    if detect_t is not None and detect_t < V1_FORWARD_START_T:
        return "retro"
    return "forward"


def _ingest_v1(ev, state, candles):
    rev = state.get("rev")
    if not isinstance(rev, dict):
        return
    ledger = rev.get("ledger")
    if not isinstance(ledger, list):
        return
    for rec in ledger:
        if not isinstance(rec, dict):
            continue
        kind = rec.get("kind")
        if kind == "trade":
            event_id = rec.get("event_id")
            key = "|".join([str(event_id), str(rec.get("mode")),
                            str(rec.get("entry_time"))])
            if not event_id or key in ev["seen"]["v1_trades"]:
                continue
            _seen_add(ev, "v1_trades", key)
            detect_t = _parse_iso_ms(rec.get("entry_time"))
            new = _base_record(
                "V1T:" + key, "V1_TRADE", "V1", _v1_era(detect_t),
                rec.get("side") or "LONG", detect_t,
                _f(rec.get("entry")),
            )
            new["link_id"] = event_id
            new["scores"] = {"V1": rec.get("score")}
            new["fired"] = {"V1": True}
            idx = _find_bar_idx(candles, detect_t)
            new["regime"] = _regime_at(candles, idx)
            _apply_tracking(new, candles, idx)
            new["trades"].append(_trade_summary(rec, False))
            ev["records"].append(new)
        elif kind == "unfilled":
            event_id = rec.get("event_id")
            if not event_id or event_id in ev["seen"]["v1_unfilled"]:
                continue
            _seen_add(ev, "v1_unfilled", event_id)
            detect_t = _parse_iso_ms(rec.get("start_time"))
            new = _base_record(
                "V1U:" + event_id, "V1_UNFILLED", "NONE",
                _v1_era(detect_t), rec.get("side") or "LONG",
                detect_t, _f(rec.get("ref_price")),
            )
            new["link_id"] = event_id
            new["scores"] = {"V1": rec.get("best_score")}
            new["fired"] = {"V1": False}
            new["not_fired_reasons"] = rec.get("reasons")
            idx = _find_bar_idx(candles, detect_t)
            new["regime"] = _regime_at(candles, idx)
            _apply_tracking(new, candles, idx)
            ev["records"].append(new)


def _refresh_pending(ev, candles):
    """Fill in forward tracking for records whose 16-bar window
    was not fully observable at ingestion.  Deterministic: the
    same bars always produce the same fields, and detection
    facts are never touched.  Records whose detection bar has
    scrolled out of the candle window are frozen as lost."""
    if not candles:
        return
    first_t = candles[0].get("t")
    for rec in ev["records"]:
        if rec["finalized"]:
            continue
        idx = _find_bar_idx(candles, rec["detect_t"])
        if idx is None:
            if (first_t is not None and rec["detect_t"] is not None
                    and rec["detect_t"] < first_t):
                rec["tracking_lost"] = True
                rec["finalized"] = True
            continue
        _apply_tracking(rec, candles, idx)


def _trim_records(ev):
    if len(ev["records"]) <= RECORD_LIMIT:
        return
    # drop oldest finalized retro records first, then oldest
    # finalized forward records; never drop unfinalized ones
    # unless there is no other choice.
    def droppable(rec):
        return rec["finalized"]

    excess = len(ev["records"]) - RECORD_LIMIT
    kept = []
    for rec in ev["records"]:
        if excess > 0 and droppable(rec) and rec["era"] == "retro":
            excess -= 1
            continue
        kept.append(rec)
    if excess > 0:
        tmp = []
        for rec in kept:
            if excess > 0 and droppable(rec):
                excess -= 1
                continue
            tmp.append(rec)
        kept = tmp
    ev["records"] = kept[-RECORD_LIMIT:]


# ============================================================
# update entry point
# ============================================================

def _update(ev, state, c15):
    if not c15 or len(c15) < 20:
        return {"ok": True, "note": "insufficient_candles",
                "bars": 0}
    _ingest_v2(ev, state, c15)
    _ingest_v1(ev, state, c15)
    _refresh_pending(ev, c15)
    _trim_records(ev)
    ev["last_bar_t"] = c15[-1]["t"]
    ev["last_error"] = None
    ev["updated_at"] = _iso(c15[-1]["t"] / 1000)
    return {
        "ok": True,
        "records": len(ev["records"]),
        "finalized": sum(1 for r in ev["records"] if r["finalized"]),
        "pending": sum(1 for r in ev["records"] if not r["finalized"]),
    }


def update_eval(state, c15):
    """Advance the REV evaluation layer by one house cycle.

    Isolation contract: works on a deep copy of
    state["rev_eval"] and commits it back only on success; on
    any error the house state (and state["rev"] / state["rev2"])
    is left untouched.  Never raises.
    """
    try:
        _ensure_eval(state)
        work = copy.deepcopy(state["rev_eval"])
        summary = _update(work, state, c15)
        state["rev_eval"] = work
        return summary
    except Exception as exc:  # noqa: BLE001 - isolation by design
        try:
            cur = state.get("rev_eval")
            if isinstance(cur, dict):
                cur["last_error"] = f"{type(exc).__name__}: {exc}"
        except Exception:
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ============================================================
# aggregates (finalized records only for win/loss statistics)
# ============================================================

def _mean(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _wilson95(successes, n, z=1.96):
    if not n:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt(p * (1.0 - p) / n
                            + z * z / (4.0 * n * n))) / denom
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _trade_stats(records):
    trades = []
    for rec in records:
        for tr in rec.get("trades", []):
            trades.append((rec.get("detect_t") or 0, tr))
    trades.sort(key=lambda x: x[0])
    rs = [_f(t["net_r"]) for _, t in trades]
    n = len(rs)
    if n == 0:
        return {"n_trades": 0, "wins": 0, "losses": 0,
                "win_rate": None, "avg_win_r": None,
                "avg_loss_r": None, "expectancy_r": None,
                "profit_factor_r": None,
                "max_consecutive_losses": 0, "expectancy_ci95": None,
                "total_r": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    mean = sum(rs) / n
    ci = None
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        half = 1.96 * math.sqrt(var) / math.sqrt(n)
        ci = [mean - half, mean + half]
    max_streak = 0
    streak = 0
    for r in rs:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    sum_pos = sum(wins)
    sum_neg = abs(sum(losses))
    return {
        "n_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "avg_win_r": (sum_pos / len(wins)) if wins else None,
        "avg_loss_r": (sum(losses) / len(losses)) if losses else None,
        "expectancy_r": mean,
        "profit_factor_r": (sum_pos / sum_neg) if sum_neg > 0 else None,
        "max_consecutive_losses": max_streak,
        "expectancy_ci95": ci,
        "total_r": sum(rs),
    }


def _detection_stats(records):
    fin = [r for r in records if r["finalized"]
           and not r["tracking_lost"]]
    n = len(fin)
    reversals = sum(1 for r in fin if r["outcome"] == "reversal")
    horizons = {}
    for key in HORIZON_BARS:
        succ = [r for r in fin
                if r["horizons"][key]["success"] is not None]
        horizons[key] = {
            "n": len(succ),
            "success_rate": (
                sum(1 for r in succ if r["horizons"][key]["success"])
                / len(succ) if succ else None
            ),
            "avg_ret_pct": _mean(
                [r["horizons"][key]["ret_pct"] for r in fin]
            ),
        }
    return {
        "n": n,
        "reversals": reversals,
        "failed": sum(1 for r in fin if r["outcome"] == "failed"),
        "no_outcome": sum(1 for r in fin if r["outcome"] == "none"),
        "success_rate": (reversals / n) if n else None,
        "success_rate_ci95": _wilson95(reversals, n),
        "avg_mfe_pct": _mean([r["mfe_pct"] for r in fin]),
        "avg_mae_pct": _mean([r["mae_pct"] for r in fin]),
        "avg_t_mfe_min": _mean([r["t_mfe_min"] for r in fin]),
        "avg_t_mae_min": _mean([r["t_mae_min"] for r in fin]),
        "horizons": horizons,
    }


def _group_view(records):
    return {
        "detection": _detection_stats(records),
        "trades": _trade_stats(records),
        "records_total": len(records),
        "records_finalized": sum(
            1 for r in records if r["finalized"]
        ),
        "records_pending": sum(
            1 for r in records if not r["finalized"]
        ),
    }


def _era_view(records):
    by_category = {}
    for cat in CATEGORIES:
        by_category[cat] = _group_view(
            [r for r in records if r["category"] == cat]
        )
    by_trend = {}
    for trend in ("UP", "DOWN", "RANGE"):
        by_trend[trend] = _group_view(
            [r for r in records if r["regime"]["trend"] == trend]
        )
    by_vol = {}
    for vol in ("HIGH", "MID", "LOW"):
        by_vol[vol] = _group_view(
            [r for r in records if r["regime"]["vol"] == vol]
        )
    return {
        "overall": _group_view(records),
        "by_category": by_category,
        "by_regime_trend": by_trend,
        "by_regime_vol": by_vol,
    }


def dashboard_payload(state):
    """Read-only evaluation summary for docs/data.json
    (fully separated from the house, REV V1 and REV V2
    sections).  Aggregates are computed per era and never
    pooled; unfinalized records never enter win/loss stats."""
    base = {
        "enabled": True,
        "version": EVAL_VERSION,
        "definition_version": EVAL_DEFINITION_VERSION,
        "measurement_only": True,
        "note": "REV evaluation layer: detection quality and "
                "virtual-trade profitability, aggregated "
                "separately. Observes REV V1/V2 only; no effect "
                "on detection, thresholds or house trading.",
        "definitions": {
            "reversal": "within 16 bars (4h) after detection, "
                        "+2.0% favourable reached before -1.0% "
                        "adverse (same-bar conflict resolves to "
                        "adverse); inherited from REV V2 fixed "
                        "definition, never revised",
            "horizons_min": {"15m": 15, "30m": 30, "60m": 60,
                             "120m": 120},
            "regime_trend": "chg_1h>=+0.30% and chg_4h>=+0.50% "
                            "-> UP; both <= negative -> DOWN; "
                            "else RANGE (15m bars, detection bar)",
            "regime_vol": "ATR(14)/close on 15m bars: "
                          ">=0.45% HIGH, <=0.25% LOW, else MID",
            "expectancy_ci95": "mean net R +/- 1.96 * sample "
                               "stdev / sqrt(n), n>=2",
            "success_rate_ci95": "Wilson score interval, z=1.96",
            "profit_factor": "R-based: sum(+net R)/|sum(-net R)|",
            "era_v1": "V1 detections before 2026-11-21T14:30Z "
                      "are retro (initial backfill), rest "
                      "forward; V2 era copied from its phase",
        },
    }
    ev = state.get("rev_eval") if isinstance(state, dict) else None
    if not isinstance(ev, dict) or "records" not in ev:
        base["initialized"] = False
        return base
    records = ev.get("records", [])
    base.update({
        "initialized": True,
        "eras": {
            era: _era_view([r for r in records if r["era"] == era])
            for era in ("forward", "retro")
        },
        "recent_records": [
            {
                "rec_id": r["rec_id"],
                "source": r["source"],
                "category": r["category"],
                "era": r["era"],
                "side": r["side"],
                "detect_time": r["detect_time"],
                "ref_price": r["ref_price"],
                "scores": r["scores"],
                "fired": r["fired"],
                "regime": {"trend": r["regime"]["trend"],
                           "vol": r["regime"]["vol"]},
                "outcome": r["outcome"],
                "mfe_pct": r["mfe_pct"],
                "mae_pct": r["mae_pct"],
                "finalized": r["finalized"],
                "n_trades": len(r["trades"]),
                "net_r": (
                    sum(_f(t["net_r"]) for t in r["trades"])
                    if r["trades"] else None
                ),
            }
            for r in list(reversed(records[-8:]))
        ],
        "last_bar_time": (
            _iso(ev["last_bar_t"] / 1000) if ev.get("last_bar_t")
            else None
        ),
        "updated_at": ev.get("updated_at"),
        "last_error": ev.get("last_error"),
    })
    return base
