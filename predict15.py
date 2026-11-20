#!/usr/bin/env python3
"""BTC 15-minute UP/DOWN predictor (measurement / display only).

Once per run (dispatched three minutes before each quarter hour -
at :12/:27/:42/:57 - by cron-job.org -> workflow_dispatch; three
minutes rather than one because GitHub Actions startup plus the
Pages rebuild can take 1-2 minutes) this script:

  1. SETTLES every past round whose 15-minute window has ended and that
     has no result yet. The official start/end prices are the open and
     close of that round's own Hyperliquid 15m candle (the open is the
     price exactly at the round's start time), so a round is UP when
     close > open, DOWN when close < open, FLAT when equal. The
     prediction-time mid price stored on the prediction record is a
     reference only and is never used for settlement.
  2. PREDICTS the next round (the next :00/:15/:30/:45 boundary) from
     three independent signals, each UP / DOWN / NEUTRAL ("-"):
       traders   - BTC position direction of the Hyperliquid
                   leaderboard's best 10 traders (see cohort note
                   below). UP/DOWN only when >= 8 of the 10 point the
                   same way; flat (no BTC position) members are not
                   counted, so fewer than 8 aligned means NEUTRAL.
       trend     - 1h trend composite: sign of (EMA20 - EMA50),
                   sign of (close - EMA20) and sign of the EMA20 slope
                   over the last 3 bars; majority decides, the last
                   candle's own direction breaks a 0 tie. Since
                   strategy v2.0-regime the 1h ADX(14) also classifies
                   the market regime: ADX < 20 is RANGE and the trend
                   signal is NEUTRAL there (no forced direction in a
                   ranging market); ADX >= 20 keeps the direction.
       technical - 15m composite of three votes: RSI(14) zone
                   (>=55 UP, <=45 DOWN, otherwise no vote), MACD
                   (12,26,9) histogram sign, EMA9 vs EMA21. Majority
                   of the cast votes decides; a tie is NEUTRAL.
     The final verdict comes from the two analysis signals only:
     UP when trend and technical are both UP, DOWN when both are
     DOWN, and SKIP (the dashboard shows "見送り") otherwise. The
     traders signal is still computed, recorded, displayed and
     graded on its own, but it is reference-only and never affects
     the verdict (owner's decision, 2026-11-17: the best-10 cohort
     is mostly flat, so requiring its agreement made bet rounds
     vanishingly rare). Since strategy v2.0-regime the verdict is
     FROZEN at prediction time: each new prediction record carries
     strategy_version and its verdict, and settlement grading and
     summary rebuilds use frozen_verdict(), which returns the
     stored verdict unchanged for versioned records, so a later
     rule change never rewrites the published track record of a
     newer strategy. Records written before versioning (v1, no
     strategy_version) are interpreted by frozen_verdict() under
     their frozen v1 rule - the analysis-only derive_verdict()
     rule - see that function's docstring. The JSONL originals
     themselves are never rewritten.
  3. Rewrites docs/predict15.json, the compact summary the dashboard
     reads (next round, accuracy stats, recent rounds merged from the
     JSONL log).

With `--settle-only` (dispatched at :02/:17/:32/:47 with workflow
input mode=settle) the run performs only steps 1 and 3 - it settles
finished rounds and rewrites the summary, but never creates a new
prediction - so results appear on the dashboard a couple of minutes
after a round ends instead of waiting for the next prediction run.
Without the flag the behaviour is exactly as described above.

Records live in docs/predict15.jsonl as two line types:
  {"type": "prediction", round_start_ts, round_end_ts, predicted_at_ts,
   price_at_prediction, signals{traders,trend,technical},
   signal_details{...}, verdict: "UP"|"DOWN"|"SKIP"}
  {"type": "result", round_start_ts, settled_at_ts, start_price,
   end_price, actual: "UP"|"DOWN"|"FLAT", hit: true|false|null}
`hit` is set only for bet rounds (verdict UP/DOWN) with a non-FLAT
actual; SKIP rounds and FLAT actuals get hit=null.

Accuracy definitions (also shown on the dashboard):
  bet rate - settled bet rounds only (verdict UP/DOWN, actual UP/DOWN)
  all rate - every settled round is in the denominator; only bet hits
             count in the numerator (a SKIP round can never be a hit)
  signal rates - computed here over ALL settled rounds and stored in
             the summary as stats.signal_rates, so the dashboard's
             win-rate bar renders exactly these numbers instead of
             recomputing them from the (truncated) rounds list:
             traders = rounds where the traders signal was UP/DOWN,
             analysis = rounds where trend and technical agreed on a
             direction, verdict = the bet rounds above. Rounds whose
             actual direction is FLAT are excluded from all three.

Bet simulation (paper only, no real orders): every settled bet round
(verdict UP/DOWN) is treated as a flat BET_USDT stake under an
even-money binary model - a hit returns stake * PAYOUT_MULTIPLE
(stake plus the same amount of profit), a miss returns 0, and a FLAT
actual refunds the stake. SKIP rounds stake nothing. The aggregates
(total staked, total returned, return rate, profit) are recomputed
from the full log on every run, so they cover all past settled rounds
retroactively. PAYOUT_MULTIPLE is a single constant so the model can
later be swapped for real Polymarket prices.

Cohort definition for the traders signal ("best 10"): the public
leaderboard (stats-data.hyperliquid.xyz, no auth) ranked by all-time
PnL, descending, skipping the HLP protocol vault. PnL is used rather
than account value because the user's intent is "traders who have
mastered the 15-minute game" - proven profit, not account size.

This script is fully independent of bot.py: it never reads or writes
the paper-trading state, places no orders, and uses only read-only
public APIs. It exists so the user can see a data-driven UP/DOWN call
before each 15-minute round starts, and to measure whether the
trend/technical agreement rule actually predicts anything.

Idempotency: a prediction for a round that already has one is never
duplicated, and a settled round is never settled twice, so overlapping
dispatches are harmless.

Persistence note (same as whale_shadow.py): when running inside
GitHub Actions the script commits docs/predict15.jsonl and
docs/predict15.json itself with a rebase-and-retry loop; the
workflow's own commit step then finds nothing new and no-ops.
Outside Actions the git step is skipped.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "docs", "predict15.jsonl")
SUMMARY_PATH = os.path.join(BASE_DIR, "docs", "predict15.json")
ROUND_SEC = 900
TOP_N = 10
ALIGN_MIN = 8  # of the top 10, same-direction members needed for a signal
EXCLUDE_ADDRESSES = {
    "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303",  # HLP vault
}
BET_USDT = 4.0  # simulated stake per bet round
PAYOUT_MULTIPLE = 2.0  # even-money binary: a hit returns stake * this
STRATEGY_VERSION = "v2.0-regime"
SAMPLE_INSUFFICIENT_THRESHOLD = 100  # bet rounds needed before stats are firm
MACD_REL_THRESHOLD = 0.00005  # |macd_hist| / close below this casts no vote
EMA_REL_THRESHOLD = 0.0002  # |ema9 - ema21| / close below this casts no vote
ADX_RANGE_MAX = 20.0  # ADX below this is a ranging market
ADX_TREND_MIN = 25.0  # ADX at/above this is a trending market
HIGH_VOL_ATR_PCT = 0.008  # ATR/close at/above this flags HIGH_VOL
PAUSE_SEC = 0.15  # between info API calls, same as whale_shadow.py
USER_AGENT = "Mozilla/5.0 (compatible; predict15-logger/1.0)"
RECENT_ROUNDS_IN_SUMMARY = 96


class RateLimited(Exception):
    """Raised on HTTP 429 so the caller can stop instead of hammering."""


def _open(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(f"HTTP 429 from {req.full_url}") from exc
        raise


def post_info(payload):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
    )
    return json.loads(_open(req, timeout=30))


def fetch_leaderboard():
    req = urllib.request.Request(
        LEADERBOARD_URL, headers={"User-Agent": USER_AGENT})
    return json.loads(_open(req, timeout=60))


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------- log io

def read_log():
    predictions, results = {}, {}
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "prediction":
                    predictions[rec["round_start_ts"]] = rec
                elif rec.get("type") == "result":
                    results[rec["round_start_ts"]] = rec
    return predictions, results


def append_line(record):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- candles

def fetch_candles(interval, start_ts, end_ts):
    """Hyperliquid candleSnapshot for BTC; returns list of dicts with
    numeric t (ms), o, c, h, l sorted by t."""
    out = post_info({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": int(start_ts * 1000),
            "endTime": int(end_ts * 1000),
        },
    })
    candles = [{
        "t": int(c["t"]),
        "o": float(c["o"]),
        "c": float(c["c"]),
        "h": float(c["h"]),
        "l": float(c["l"]),
    } for c in out]
    candles.sort(key=lambda c: c["t"])
    return candles


def ema_series(values, period):
    k = 2.0 / (period + 1)
    out = []
    ema = None
    for v in values:
        ema = v if ema is None else v * k + ema * (1 - k)
        out.append(ema)
    return out


def rsi_last(closes, period=14):
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0 and avg_gain == 0:
        return 50.0  # no movement at all: neutral, not 100
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd_hist_last(closes):
    if len(closes) < 35:
        return None
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd = [a - b for a, b in zip(ema12, ema26)]
    signal = ema_series(macd, 9)
    return macd[-1] - signal[-1]


def adx_atr_last(candles, period=14):
    """Wilder ADX(period) and ATR(period) from OHLC candles.

    Returns (adx, atr); either may be None when there are not enough
    candles. A completely flat series yields ADX 0.0 / ATR 0.0."""
    if len(candles) < 2 * period + 1:
        return None, None
    trs, pdms, mdms = [], [], []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        ph, pl = candles[i - 1]["h"], candles[i - 1]["l"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdms.append(up if (up > dn and up > 0) else 0.0)
        mdms.append(dn if (dn > up and dn > 0) else 0.0)

    def _wilder(vals):
        out = [None] * len(vals)
        s = sum(vals[:period])
        out[period - 1] = s
        for i in range(period, len(vals)):
            s = s - s / period + vals[i]
            out[i] = s
        return out

    str_ = _wilder(trs)
    spdm = _wilder(pdms)
    smdm = _wilder(mdms)
    dxs, dx_idx = [], []
    for i in range(period - 1, len(trs)):
        if not str_[i]:
            dxs.append(0.0)
        else:
            pdi = 100.0 * spdm[i] / str_[i]
            mdi = 100.0 * smdm[i] / str_[i]
            denom = pdi + mdi
            dxs.append(0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom)
        dx_idx.append(i)
    if len(dxs) < period:
        return None, (str_[-1] / period if str_[-1] is not None else None)
    adx = sum(dxs[:period]) / period
    for v in dxs[period:]:
        adx = (adx * (period - 1) + v) / period
    atr = str_[-1] / period if str_[-1] is not None else None
    return adx, atr


def check_candle_fresh(candles, now_ts, interval_sec, label):
    """Guard: refuse to predict from stale data. The newest candle
    must not be older than two bars of its interval."""
    if not candles:
        raise RuntimeError(f"no {label} candles")
    last_ts = candles[-1]["t"] // 1000
    if now_ts - last_ts > 2 * interval_sec:
        raise RuntimeError(
            f"stale {label} candles: last={iso(last_ts)} now={iso(now_ts)}")


def sign_dir(value):
    if value > 0:
        return "UP"
    if value < 0:
        return "DOWN"
    return None


# ---------------------------------------------------------------- signals

def traders_signal():
    """Best-10 cohort by all-time PnL; count BTC position directions."""
    rows = fetch_leaderboard()["leaderboardRows"]

    def alltime_pnl(row):
        for name, perf in row.get("windowPerformances", []):
            if name == "allTime":
                return float(perf["pnl"])
        return 0.0

    ranked = sorted(rows, key=alltime_pnl, reverse=True)
    cohort = []
    for row in ranked:
        addr = row["ethAddress"].lower()
        if addr in EXCLUDE_ADDRESSES:
            continue
        cohort.append({
            "address": row["ethAddress"],
            "account_value": float(row["accountValue"]),
            "pnl_alltime": alltime_pnl(row),
        })
        if len(cohort) >= TOP_N:
            break

    members = []
    long_n = short_n = flat_n = 0
    for member in cohort:
        state = post_info({"type": "clearinghouseState",
                           "user": member["address"]})
        szi = 0.0
        usd = 0.0
        for entry in state.get("assetPositions", []):
            pos = entry["position"]
            if pos["coin"] == "BTC":
                szi = float(pos["szi"])
                usd = float(pos["positionValue"]) * (1 if szi >= 0 else -1)
                break
        if szi > 0:
            direction, long_n = "LONG", long_n + 1
        elif szi < 0:
            direction, short_n = "SHORT", short_n + 1
        else:
            direction, flat_n = "FLAT", flat_n + 1
        members.append({**member, "direction": direction,
                        "szi": szi, "usd": usd})
        time.sleep(PAUSE_SEC)

    if long_n >= ALIGN_MIN:
        signal = "UP"
    elif short_n >= ALIGN_MIN:
        signal = "DOWN"
    else:
        signal = "NEUTRAL"
    fetched_ts = int(time.time())
    details = {
        "cohort": "leaderboard_alltime_pnl_top10",
        "long": long_n, "short": short_n, "flat": flat_n,
        "align_min": ALIGN_MIN,
        "fetched_at_ts": fetched_ts,
        "fetched_at_iso": iso(fetched_ts),
        "members": members,
    }
    return signal, details


def trend_signal(now_ts):
    """1h trend composite plus ADX/ATR market-regime classification.

    Direction comes from the 3-vote EMA composite, except in a RANGE
    regime (ADX(14) < 20), where the signal is NEUTRAL so the round
    is skipped instead of forcing a direction."""
    candles = fetch_candles("1h", now_ts - 80 * 3600, now_ts)
    closes = [c["c"] for c in candles]
    if len(closes) < 55:
        raise RuntimeError(f"not enough 1h candles: {len(closes)}")
    check_candle_fresh(candles, now_ts, 3600, "1h")
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    score = 0
    score += 1 if ema20[-1] > ema50[-1] else -1
    score += 1 if closes[-1] > ema20[-1] else -1
    score += 1 if ema20[-1] > ema20[-4] else -1
    if score > 0:
        signal = "UP"
    elif score < 0:
        signal = "DOWN"
    else:  # unreachable with 3 odd votes, kept as a safety net
        signal = "UP" if closes[-1] >= candles[-1]["o"] else "DOWN"
    adx, atr = adx_atr_last(candles)
    atr_pct = (atr / closes[-1]) if (atr is not None and closes[-1]) else None
    if adx is None:
        regime = "UNKNOWN"
    elif adx < ADX_RANGE_MAX:
        regime = "RANGE"
    elif adx < ADX_TREND_MIN:
        regime = "WEAK_TREND"
    else:
        regime = "TREND"
    high_vol = atr_pct is not None and atr_pct >= HIGH_VOL_ATR_PCT
    if regime == "RANGE":
        signal = "NEUTRAL"
    details = {
        "ema20": ema20[-1], "ema50": ema50[-1],
        "close": closes[-1], "score": score,
        "adx": adx, "atr": atr, "atr_pct": atr_pct,
        "regime": regime,
        "regime_detail": {
            "high_vol": high_vol,
            "flags": ["HIGH_VOL"] if high_vol else [],
        },
        "last_candle_t": candles[-1]["t"],
        "last_candle_iso": iso(candles[-1]["t"] // 1000),
        "last_candle_closed": False,
        "rule": "sign(EMA20-EMA50)+sign(close-EMA20)+sign(EMA20 slope 3 bars); ADX<20 RANGE -> NEUTRAL",
    }
    return signal, details


def technical_signal(now_ts):
    """15m composite: RSI zone vote + MACD histogram sign + EMA9/21."""
    candles = fetch_candles("15m", now_ts - 120 * ROUND_SEC, now_ts)
    closes = [c["c"] for c in candles]
    if len(closes) < 40:
        raise RuntimeError(f"not enough 15m candles: {len(closes)}")
    check_candle_fresh(candles, now_ts, ROUND_SEC, "15m")
    rsi = rsi_last(closes)
    hist = macd_hist_last(closes)
    ema9 = ema_series(closes, 9)[-1]
    ema21 = ema_series(closes, 21)[-1]
    close = closes[-1]
    macd_rel = (abs(hist) / close) if (hist is not None and close) else None
    ema_rel = (abs(ema9 - ema21) / close) if close else None

    votes = {}
    votes["rsi"] = ("UP" if rsi >= 55 else "DOWN" if rsi <= 45 else None) \
        if rsi is not None else None
    # Micro-move suppression: a MACD/EMA gap that is tiny relative to
    # price is noise, so that vote is withheld (None) instead of
    # casting a direction on a negligible difference.
    votes["macd"] = sign_dir(hist) if (
        hist is not None and macd_rel is not None
        and macd_rel >= MACD_REL_THRESHOLD) else None
    votes["ema"] = sign_dir(ema9 - ema21) if (
        ema_rel is not None and ema_rel >= EMA_REL_THRESHOLD) else None
    up = sum(1 for v in votes.values() if v == "UP")
    down = sum(1 for v in votes.values() if v == "DOWN")
    if up > down:
        signal = "UP"
    elif down > up:
        signal = "DOWN"
    else:
        signal = "NEUTRAL"
    details = {
        "rsi": rsi, "macd_hist": hist, "ema9": ema9, "ema21": ema21,
        "macd_hist_rel": macd_rel, "ema_diff_rel": ema_rel,
        "macd_rel_threshold": MACD_REL_THRESHOLD,
        "ema_rel_threshold": EMA_REL_THRESHOLD,
        "votes": votes,
        "last_candle_t": candles[-1]["t"],
        "last_candle_iso": iso(candles[-1]["t"] // 1000),
        "last_candle_closed": False,
        "rule": "majority of RSI(>=55 UP / <=45 DOWN), MACD hist sign, EMA9 vs EMA21; MACD/EMA votes withheld below relative thresholds",
    }
    return signal, details


# ---------------------------------------------------------------- verdict

def derive_verdict(signals):
    """Derive a verdict from signals under the analysis-only rule.

    Only the two analysis signals decide: trend (1h) and technical
    (15m). Both UP -> "UP", both DOWN -> "DOWN", anything else (they
    disagree, or either is NEUTRAL) -> "SKIP". The traders signal
    is reference-only and never affects the verdict.

    Role (since v2.0-regime): used ONLY (a) when creating a new
    prediction, whose verdict is then frozen onto the record, and
    (b) as the frozen interpretation of pre-versioning v1 records
    inside frozen_verdict(). Settlement and summary code must call
    frozen_verdict(), never this function directly, so a future
    rule change cannot re-grade versioned records.
    """
    trend = signals.get("trend")
    technical = signals.get("technical")
    if trend == "UP" and technical == "UP":
        return "UP"
    if trend == "DOWN" and technical == "DOWN":
        return "DOWN"
    return "SKIP"


def frozen_verdict(pred):
    """The immutable verdict for a stored prediction record.

    Records carrying a strategy_version return their stored verdict
    exactly as recorded at prediction time; it is never recomputed.

    Records without a strategy_version are v1 records. The frozen
    rule for v1 is the analysis-only rule (derive_verdict over the
    stored signals): that is the rule under which the v1 track
    record was published, so interpreting v1 records this way keeps
    their published results from being retroactively changed by the
    v2 regime logic (which lives in trend_signal, not here).
    """
    if pred.get("strategy_version"):
        stored = pred.get("verdict")
        if stored in ("UP", "DOWN", "SKIP"):
            return stored
    return derive_verdict(pred.get("signals") or {})


def compute_hit(verdict, actual):
    """Whether a bet-round verdict matched the actual direction.
    None when there is nothing to grade (SKIP verdict, FLAT or
    missing actual)."""
    if verdict in ("UP", "DOWN") and actual in ("UP", "DOWN"):
        return verdict == actual
    return None


# ---------------------------------------------------------------- settle

def settle_pending(predictions, results, now_ts):
    """Append result lines for rounds that have ended. Returns the
    number of rounds settled in this run."""
    settled = 0
    for start_ts in sorted(predictions):
        if start_ts in results:
            continue
        end_ts = start_ts + ROUND_SEC
        if end_ts > now_ts:
            continue
        candles = fetch_candles("15m", start_ts, end_ts)
        candle = next((c for c in candles if c["t"] == start_ts * 1000),
                      None)
        if candle is None:
            print(f"predict15: no candle for round {iso(start_ts)}, "
                  "retry next run", file=sys.stderr)
            continue
        start_price, end_price = candle["o"], candle["c"]
        if end_price > start_price:
            actual = "UP"
        elif end_price < start_price:
            actual = "DOWN"
        else:
            actual = "FLAT"
        verdict = frozen_verdict(predictions[start_ts])
        hit = compute_hit(verdict, actual)
        result = {
            "type": "result",
            "round_start_ts": start_ts,
            "settled_at_ts": now_ts,
            "settled_at_iso": iso(now_ts),
            "start_price": start_price,
            "end_price": end_price,
            "actual": actual,
            "hit": hit,
        }
        append_line(result)
        results[start_ts] = result
        settled += 1
    return settled


# ---------------------------------------------------------------- summary

def sim_for_round(verdict, actual, hit):
    """Simulated bet outcome for one settled round. Returns None when
    no bet was placed (SKIP verdict); otherwise a dict with the stake,
    the amount returned and the profit under the even-money model."""
    if verdict not in ("UP", "DOWN") or actual is None:
        return None
    if actual == "FLAT":
        payout = BET_USDT  # stake refunded
    elif hit:
        payout = BET_USDT * PAYOUT_MULTIPLE
    else:
        payout = 0.0
    return {
        "stake": BET_USDT,
        "payout": payout,
        "pnl": payout - BET_USDT,
    }


def build_summary(predictions, results, now_ts):
    rounds = []
    for start_ts in sorted(predictions, reverse=True):
        pred = predictions[start_ts]
        res = results.get(start_ts)
        # The verdict is the frozen one (frozen_verdict): versioned
        # records keep the verdict recorded at prediction time; v1
        # records are interpreted under their frozen v1 rule.
        verdict = frozen_verdict(pred)
        actual = res["actual"] if res else None
        hit = compute_hit(verdict, actual) if res else None
        trend_details = (pred.get("signal_details") or {}).get("trend") or {}
        rounds.append({
            "round_start_ts": start_ts,
            "round_start_iso": pred["round_start_iso"],
            "predicted_at_iso": pred["predicted_at_iso"],
            "price_at_prediction": pred["price_at_prediction"],
            "strategy_version": pred.get("strategy_version"),
            "regime": trend_details.get("regime"),
            "signals": pred["signals"],
            "traders_counts": {
                k: pred["signal_details"]["traders"][k]
                for k in ("long", "short", "flat")
            },
            "verdict": verdict,
            "start_price": res["start_price"] if res else None,
            "end_price": res["end_price"] if res else None,
            "actual": actual,
            "hit": hit,
            "settled": res is not None,
            "sim": sim_for_round(verdict, actual, hit),
        })

    settled_rounds = [r for r in rounds if r["settled"]]
    bet = [r for r in settled_rounds
           if r["verdict"] in ("UP", "DOWN") and r["actual"] in ("UP", "DOWN")]
    bet_hits = sum(1 for r in bet if r["hit"])
    all_hits = sum(1 for r in settled_rounds if r["hit"])

    def _sig_rate(pairs):
        total = len(pairs)
        hits = sum(1 for dir_, act in pairs if dir_ == act)
        return {"hits": hits, "total": total,
                "rate": (hits / total) if total else None}

    directional = [r for r in settled_rounds
                   if r["actual"] in ("UP", "DOWN")]
    signal_rates = {
        "traders": _sig_rate([
            (r["signals"]["traders"], r["actual"]) for r in directional
            if r["signals"]["traders"] in ("UP", "DOWN")]),
        "analysis": _sig_rate([
            (r["signals"]["trend"], r["actual"]) for r in directional
            if r["signals"]["trend"] in ("UP", "DOWN")
            and r["signals"]["trend"] == r["signals"]["technical"]]),
        "verdict": _sig_rate([
            (r["verdict"], r["actual"]) for r in bet]),
    }

    sim_rounds = [r for r in settled_rounds if r["sim"]]
    sim_invested = sum(r["sim"]["stake"] for r in sim_rounds)
    sim_returned = sum(r["sim"]["payout"] for r in sim_rounds)
    sim = {
        "rounds": len(sim_rounds),
        "stake_per_round": BET_USDT,
        "payout_multiple": PAYOUT_MULTIPLE,
        "model_note": "virtual_fixed_multiple",
        "invested": sim_invested,
        "returned": sim_returned,
        "pnl": sim_returned - sim_invested,
        "return_rate": (sim_returned / sim_invested) if sim_invested else None,
    }

    # --- separated statistics (v2.0-regime; existing keys unchanged) ---
    # Coverage: how often we actually bet, out of all settled rounds
    # whose actual direction was not FLAT.
    coverage = (len(bet) / len(directional)) if directional else None
    by_direction = {}
    for _dir in ("UP", "DOWN"):
        _pairs = [r for r in bet if r["verdict"] == _dir]
        _hits = sum(1 for r in _pairs if r["hit"])
        by_direction[_dir] = {
            "hits": _hits, "total": len(_pairs),
            "rate": (_hits / len(_pairs)) if _pairs else None,
        }
    # Miss streaks over bet rounds in chronological (round_start
    # ascending) order; FLAT/SKIP rounds neither extend nor break a
    # streak because they were never graded.
    _max_miss = _cur_miss = 0
    for r in sorted(bet, key=lambda x: x["round_start_ts"]):
        if r["hit"]:
            _cur_miss = 0
        else:
            _cur_miss += 1
            _max_miss = max(_max_miss, _cur_miss)
    streaks = {"max_miss": _max_miss, "current_miss": _cur_miss}
    sample_status = ("insufficient"
                     if len(bet) < SAMPLE_INSUFFICIENT_THRESHOLD
                     else "sufficient")

    # --- by-regime statistics (v2.0-regime only) ---
    # Aggregation only: the prediction / verdict logic above is
    # untouched. v1 rounds are excluded because their regime
    # labels carry a different meaning. 95% intervals are Wilson
    # score intervals, which stay sane at small n.
    def _wilson95(hits, n, z=1.96):
        if not n:
            return [None, None]
        p = hits / n
        z2 = z * z
        denom = 1.0 + z2 / n
        centre = (p + z2 / (2.0 * n)) / denom
        margin = (z * ((p * (1.0 - p) / n
                        + z2 / (4.0 * n * n)) ** 0.5)) / denom
        return [max(0.0, centre - margin), min(1.0, centre + margin)]

    def _regime_block(regime_rounds):
        r_bet = [r for r in regime_rounds
                 if r["verdict"] in ("UP", "DOWN")
                 and r["actual"] in ("UP", "DOWN")]
        r_hits = sum(1 for r in r_bet if r["hit"])
        r_dir = [r for r in regime_rounds
                 if r["actual"] in ("UP", "DOWN")]
        r_sim = sum(r["sim"]["pnl"] for r in regime_rounds
                    if r["sim"])
        _mx = _cur = 0
        for r in sorted(r_bet, key=lambda x: x["round_start_ts"]):
            if r["hit"]:
                _cur = 0
            else:
                _cur += 1
                _mx = max(_mx, _cur)
        return {
            "rounds": len(regime_rounds),
            "directional_rounds": len(r_dir),
            "bets": len(r_bet),
            "hits": r_hits,
            "hit_rate": (r_hits / len(r_bet)) if r_bet else None,
            "wilson95": _wilson95(r_hits, len(r_bet)),
            "coverage": (len(r_bet) / len(r_dir)) if r_dir else None,
            "sim_pnl": r_sim,
            "ev_per_bet": (r_sim / len(r_bet)) if r_bet else None,
            "max_miss_streak": _mx,
            "current_miss_streak": _cur,
        }

    v2_rounds = [r for r in settled_rounds
                 if r["strategy_version"] == STRATEGY_VERSION]
    by_regime = {"scope": STRATEGY_VERSION}
    for _regime in ("TREND", "WEAK_TREND", "RANGE"):
        by_regime[_regime] = _regime_block(
            [r for r in v2_rounds if r["regime"] == _regime])

    stats = {
        "settled": len(settled_rounds),
        "bet_rounds": len(bet),
        "bet_hits": bet_hits,
        "bet_rate": (bet_hits / len(bet)) if bet else None,
        "all_rounds": len(settled_rounds),
        "all_hits": all_hits,
        "all_rate": (all_hits / len(settled_rounds)) if settled_rounds else None,
        "signal_rates": signal_rates,
        "sim": sim,
        "coverage": coverage,
        "by_direction": by_direction,
        "by_regime": by_regime,
        "bet_rate_wilson95": _wilson95(bet_hits, len(bet)),
        "streaks": streaks,
        "sample_status": sample_status,
        "sample_threshold": SAMPLE_INSUFFICIENT_THRESHOLD,
        "strategy_version": STRATEGY_VERSION,
    }

    next_round = None
    for r in rounds:  # newest first; the next round is the newest unsettled
        if not r["settled"]:
            next_round = r
            break

    return {
        "updated_at": iso(now_ts),
        "updated_ts": now_ts,
        "next_round": next_round,
        "stats": stats,
        "rounds": rounds[:RECENT_ROUNDS_IN_SUMMARY],
    }


def write_summary(summary):
    os.makedirs(os.path.dirname(SUMMARY_PATH), exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- git

def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          timeout=120)


def self_commit():
    """Commit the predict15 outputs from inside GitHub Actions.
    No-op outside Actions; failures are reported and swallowed."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        os.chdir(BASE_DIR)
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email",
             "41898282+github-actions[bot]@users.noreply.github.com")
        _git("add", "docs/predict15.jsonl", "docs/predict15.json")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return
        commit = _git("commit", "-m", "Predict15: append round prediction/results")
        if commit.returncode != 0:
            print(f"predict15: self-commit failed: {commit.stderr}",
                  file=sys.stderr)
            return
        for attempt in range(3):
            push = _git("push", "origin", "HEAD:main")
            if push.returncode == 0:
                return
            print(f"predict15: push attempt {attempt + 1} failed, rebasing",
                  file=sys.stderr)
            _git("fetch", "origin", "main")
            rebase = _git("rebase", "origin/main")
            if rebase.returncode != 0:
                _git("rebase", "--abort")
                print(f"predict15: rebase failed: {rebase.stderr}",
                      file=sys.stderr)
                return
            time.sleep(5)
        _git("push", "origin", "HEAD:main")
    except Exception as exc:  # noqa: BLE001 - must never break the caller
        print(f"predict15: self-commit skipped ({exc})", file=sys.stderr)


# ---------------------------------------------------------------- main

def run(settle_only=False):
    now_ts = int(time.time())
    predictions, results = read_log()

    settled = settle_pending(predictions, results, now_ts)
    if settled:
        print(f"predict15: settled {settled} round(s)")

    target = (now_ts // ROUND_SEC + 1) * ROUND_SEC
    if settle_only:
        print("predict15: settle-only run; skipping prediction")
    elif target in predictions:
        print(f"predict15: prediction for {iso(target)} already exists, "
              "skipping")
    else:
        mids = post_info({"type": "allMids"})
        price = float(mids["BTC"]) if mids.get("BTC") else None
        traders, traders_details = traders_signal()
        trend, trend_details = trend_signal(now_ts)
        technical, technical_details = technical_signal(now_ts)
        signals = {"traders": traders, "trend": trend,
                   "technical": technical}
        verdict = derive_verdict(signals)
        # The newest Hyperliquid candle for each interval is still
        # forming at prediction time; record that explicitly so later
        # verification never mistakes it for a closed candle.
        trend_details.setdefault("last_candle_closed", False)
        technical_details.setdefault("last_candle_closed", False)
        record = {
            "type": "prediction",
            "strategy_version": STRATEGY_VERSION,
            "round_start_ts": target,
            "round_end_ts": target + ROUND_SEC,
            "round_start_iso": iso(target),
            "predicted_at_ts": now_ts,
            "predicted_at_iso": iso(now_ts),
            "price_at_prediction": price,
            "signals": signals,
            "signal_details": {
                "traders": traders_details,
                "trend": trend_details,
                "technical": technical_details,
            },
            "verdict": verdict,
        }
        append_line(record)
        predictions[target] = record
        print(json.dumps(record, ensure_ascii=False))

    summary = build_summary(predictions, results, now_ts)
    write_summary(summary)
    self_commit()
    return summary


def main():
    settle_only = "--settle-only" in sys.argv[1:]
    try:
        run(settle_only=settle_only)
    except RateLimited as exc:
        print(f"predict15: rate limited, stopping: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
