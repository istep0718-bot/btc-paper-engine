#!/usr/bin/env python3
"""
BTC Paper Engine V20.0 PRO
Professional-grade discretionary-style systematic paper trading.

- Public market data only (Hyperliquid public info API).
- Paper trading only. No wallet, private keys, or live orders.
- Standard library only.

Design pillars (fixing V19.x fatal flaws):
  * Regime classification (ADX / ATR percentile / BB width) gates setups.
  * Market structure: swings, S/R zones, minimum 1.5R room to opposing zone.
  * Setup-separated strategies (A pullback / B breakout / C range fade),
    each trade tagged and measured separately.
  * Confluence scoring incl. cost filter (expected move must dwarf costs).
  * Exit redesign: structural stop, +1R breakeven, thin/late staged
    partials at +1.5R / +3R (20% each, 60% runner), wide Chandelier
    trail (ATR x 3.5) with a +1R profit floor, trend-flip runner exit,
    24h max hold extendable to 72h while the trend cooperates, time stop.
    (V20.2: exits re-tuned to let winners run, per user direction.)
  * Unified risk: effective risk 5% of equity, DD throttle, daily stop
    count / daily loss lock, consecutive-loss cooldown.
  * Complete trade records: build, setup, regime, score breakdown,
    entry/stop/targets, planned risk, MFE/MAE, fees, funding, R multiple.
  * Realistic fills: stop fills use the worse of stop vs candle close
    plus extra slippage; marked equity is net of estimated exit costs.
  * Setup D (V20.3): copy trading. A vetted Hyperliquid trader list
    (COPY_TRADERS, see copy_research.json) is polled every cycle; a
    fresh BTC open / direction flip by a tracked trader generates a
    same-direction entry under every house rule (5% effective risk,
    structural stop, event-risk filter, DD throttle, daily locks,
    V20.2 exit engine). If the source closes or flips away, the copy
    position is closed (COPY_SOURCE_EXIT). Traders are re-evaluated
    daily and auto-disabled on inactivity or performance decay.
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V20_3_PRO_2026-10-30"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

# ============================================================
# CONFIGURATION
# ============================================================

INITIAL_EQUITY = 100.0

# Fixed leverage (position size is risk-based; leverage only caps margin).
LEVERAGE = 40.0

# Unified effective risk per trade: 5% of equity at full stop-out
# (including estimated round-trip costs). Planned == effective by
# construction; the margin cap can only reduce it, never increase it.
RISK_PER_TRADE = 0.05

# Maximum simulated margin allocation.
MAX_MARGIN_FRACTION = 0.25

FEE_RATE = 0.0005            # taker fee per side (paper model)
SLIPPAGE_RATE = 0.0002       # base slippage per side
STOP_EXTRA_SLIPPAGE = 0.0005  # extra adverse slippage assumed on stop fills

# Round-trip cost rate per unit of notional (entry+exit fee & slippage,
# plus the extra stop slippage). Used for BE offset and the cost filter.
ROUND_TRIP_COST_RATE = 2 * FEE_RATE + 2 * SLIPPAGE_RATE + STOP_EXTRA_SLIPPAGE

# --- Exit framework (R = initial risk distance) ---
STOP_ATR_MIN = 1.8           # structural stop clamped into [1.8, 2.6] x ATR
STOP_ATR_MAX = 2.6
STOP_STRUCT_BUFFER_ATR = 0.15  # buffer beyond the swing/zone for the stop
BREAKEVEN_AT_R = 1.0         # move stop to entry + costs at +1R
TP1_AT_R = 1.5               # first partial at +1.5R ...
TP1_FRACTION = 0.20          # ... closing 20% of the initial quantity
TP2_AT_R = 3.0               # second partial at +3.0R ...
TP2_FRACTION = 0.20          # ... closing 20% of the initial quantity
                             # (runner keeps 60% of the initial size)
CHANDELIER_ATR = 3.5         # runner trail: best price -/+ ATR x 3.5
RUNNER_FLOOR_ARM_R = 2.0     # once MFE >= +2R, the runner stop may not
RUNNER_FLOOR_R = 1.0         # fall below entry +/- 1R (never give back
                             # the whole move)
TIME_STOP_SECONDS = 4 * 3600  # exit if MFE < 0.5R after 4 hours
TIME_STOP_MIN_MFE_R = 0.5
MAX_HOLD_SECONDS = 86400     # 24h base block; extendable (see below)
MAX_HOLD_EXTENSIONS = 2      # up to 2 extra 24h blocks (72h hard cap)
EXTENSION_MIN_R = 1.0        # extension requires unrealized >= +1R and
                             # the HTF bias still aligned with the side

# --- Entry gates ---
MIN_SCORE = 8                # confluence threshold for setups A / B
MIN_SCORE_C = 6              # confluence threshold for setup C (range fade;
                             # half size, and its score mix differs by design)
MIN_ZONE_ROOM_R = 1.5        # required room to opposing zone, in R
COST_FILTER_MULT = 2.0       # risk distance must be >= 2x round-trip cost
# NOTE: with the stop clamped to <= 2.6 x ATR and typical 15m ATR at
# 0.16-0.25% of price, a 3x multiple could mathematically never pass
# (max R ~= 2.2x round-trip cost). 2x caps cost drag at 50% of initial
# risk as a hard ceiling; the +1R breakeven and staged exits reduce the
# realized drag further.
SETUP_C_RISK_MULT = 0.5      # range fades take half risk

# --- Regime thresholds ---
ADX_TREND = 25.0
ADX_RANGE = 20.0
ATR_PCT_HIGH_VOL = 90.0      # ATR percentile >= 90 -> HIGH_VOLATILITY

# --- Risk management ---
DD_THROTTLE_1 = 10.0         # DD >= 10% -> risk x 0.5
DD_THROTTLE_2 = 20.0         # DD >= 20% -> risk x 0.25, setup A only
DD_HARD_PAUSE = 30.0         # DD >= 30% -> 6h circuit breaker
DRAWDOWN_PAUSE_SECONDS = 21600
DAILY_LOSS_LIMIT = 0.10      # daily loss lock at -10%
DAILY_STOP_LIMIT = 2         # or after 2 losing stop-outs in a day
MAX_CONSECUTIVE_LOSSES = 3   # 3 losses -> 2h cooldown + next risk halved
CONSECUTIVE_PAUSE_SECONDS = 7200
COOLDOWN_SECONDS = 300

HTTP_TIMEOUT = 30
HISTORY_LIMIT = 1000
TRADE_LIMIT = 1000

CANDLES_15M = 500
CANDLES_1H = 300
CANDLES_4H = 200

# --- Economic calendar / event risk (V20.1) -------------------
# Fail-open by design: an unreachable calendar never halts trading;
# the hard-coded FOMC schedule below still guards FOMC windows.
ECON_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ECON_FETCH_INTERVAL_SECONDS = 86400      # refresh at most once per day
ECON_STALE_SECONDS = 3 * 86400           # older than this -> warn only
ECON_EVENT_PRE_MINUTES = 120             # standard high-impact window
ECON_EVENT_POST_MINUTES = 30
FOMC_EVENT_PRE_MINUTES = 180             # FOMC gets a wider window
FOMC_EVENT_POST_MINUTES = 60
ECON_BLOCK_LOG_INTERVAL_SECONDS = 1800   # throttle EVENT_RISK skip logs

# FOMC statement days (the second day of each meeting), taken from the
# Federal Reserve's published meeting calendar
# (federalreserve.gov/monetarypolicy/fomccalendars.htm, checked
# 2026-10-29). Statements are released at 14:00 Eastern Time.
FOMC_STATEMENT_DAYS = [
    (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
    (2026, 7, 29), (2026, 9, 16), (2026, 10, 28), (2026, 12, 9),
    (2027, 1, 27), (2027, 3, 17), (2027, 4, 28), (2027, 6, 9),
    (2027, 7, 28), (2027, 9, 15), (2027, 10, 27), (2027, 12, 8),
    (2028, 1, 26),
]

# --- Copy trading (Setup D, V20.3) ----------------------------
# Source traders vetted 2026-10-30 from the Hyperliquid leaderboard
# + per-address fills (copy_research.json / copy_selection.json):
# >=100 closed trades in ~90d, positive realized PnL, swing-style
# holding times (median >= 1h), no martingale blow-up profile.
# To replace or drop a trader, edit ONLY this list: state tracking
# for removed addresses is discarded; re-added addresses start fresh.
COPY_TRADERS = [
    {"address": "0x24e370916722584204322fad3c179a7fbde6684b", "label": "HL-684B", "enabled": True},
    {"address": "0xca4336bba924332e7f1a08db0cd91d456d5cd918", "label": "HL-D918", "enabled": True},
    {"address": "0xe6ccc91418221ea04c4dac609b0eaee7f34ef79d", "label": "HL-F79D", "enabled": True},
]
COPY_SIGNAL_MAX_AGE_SECONDS = 1800   # ignore source opens older than 30m
COPY_RECHECK_INTERVAL_SECONDS = 86400  # daily performance re-evaluation
COPY_INACTIVE_DAYS = 30              # no fills for 30d -> auto-disable
COPY_MIN_CLOSES_30D = 30             # re-check needs this many closes
COPY_MIN_WIN_RATE_30D = 0.45         # below this (or PnL<0) -> disable
COPY_FOLLOW_SOURCE_EXIT = True       # close our copy when source exits

# ============================================================
# UTILITIES
# ============================================================

def now_ts():
    return int(time.time())


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def iso_of(ts_seconds):
    return datetime.fromtimestamp(
        int(ts_seconds), tz=timezone.utc
    ).isoformat()


def utc_date():
    return datetime.now(timezone.utc).date().isoformat()


def date_of(ts_seconds):
    return datetime.fromtimestamp(
        int(ts_seconds), tz=timezone.utc
    ).date().isoformat()


def f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def i(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)


def read_json(path, default):
    try:
        with path.open(encoding="utf-8") as src:
            return json.load(src)
    except (OSError, json.JSONDecodeError):
        return default


def post(payload):
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V20/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
        return json.loads(res.read().decode("utf-8"))


def event(s, kind, **extra):
    s.setdefault("events", []).append({
        "timestamp": iso_now(),
        "type": kind,
        **extra,
    })
    s["events"] = s["events"][-200:]


# ============================================================
# STATE
# ============================================================

def default_state():
    return {
        "build": BUILD,
        "initial_equity": INITIAL_EQUITY,
        "equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
        "max_drawdown_pct": 0.0,
        "realized_pnl": 0.0,
        "trades": [],
        "equity_history": [],
        "position": None,
        "last_close_ts": 0,
        "last_entry_candle": 0,
        "consecutive_losses": 0,
        "consecutive_lock_until": 0,
        "daily_date": utc_date(),
        "daily_start_equity": INITIAL_EQUITY,
        "daily_loss_lock": False,
        "daily_stop_count": 0,
        "next_risk_multiplier": 1.0,
        "drawdown_pause_until": 0,
        "stats_by_setup": {},
        "stats_by_regime": {},
        "last_error": None,
        "events": [],
        "econ_calendar": {
            "fetched_ts": 0,
            "events": [],
            "last_error": None,
            "last_stale_warn_date": "",
            "last_block_key": "",
            "last_block_log_ts": 0,
        },
        "copy": {
            "last_recheck_ts": 0,
            "traders": {},
        },
    }


def _empty_stats_bucket():
    return {
        "round_trips": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
        "gross_win": 0.0,
        "gross_loss": 0.0,
        "total_r": 0.0,
        "fees": 0.0,
        "funding_pnl": 0.0,
    }


def _update_stats(bucket_map, key, pos, total_net, total_r):
    bucket = bucket_map.setdefault(key or "UNKNOWN", _empty_stats_bucket())
    bucket["round_trips"] += 1
    if total_net > 0:
        bucket["wins"] += 1
        bucket["gross_win"] += total_net
    elif total_net < 0:
        bucket["losses"] += 1
        bucket["gross_loss"] += total_net
    bucket["net_pnl"] += total_net
    bucket["total_r"] += total_r
    bucket["fees"] += f(pos.get("acc_fees"))
    bucket["funding_pnl"] += f(pos.get("acc_funding"))


def normalize_position(p):
    """Fill V20 fields on a position dict (incl. legacy V19 positions).

    Never moves an existing stop. Legacy positions keep their original
    stop and risk distance; they are managed under the V20 exit
    framework (breakeven / staged partials / chandelier) which can only
    tighten the stop, never loosen it.
    """
    entry = f(p.get("entry_price"))
    distance = max(f(p.get("risk_distance")), 0.01)
    side = p.get("side")

    p.setdefault("entry_fee", 0.0)
    p.setdefault("funding_pnl", 0.0)
    p.setdefault("best_price", entry)
    p.setdefault("worst_price", entry)
    p.setdefault("last_funding_ts", now_ts())
    p.setdefault("last_checked_candle_ts", 0)
    p.setdefault("opened_timestamp", now_ts())
    p.setdefault("leverage", LEVERAGE)
    p.setdefault("origin_build", p.get("build", "LEGACY"))
    p.setdefault("setup", "LEGACY")
    p.setdefault("regime", "UNKNOWN")
    p.setdefault("score_breakdown", {})
    p.setdefault("score", p.get("entry_signal_score"))
    p.setdefault("qty_initial", f(p.get("quantity")))
    p.setdefault("risk_usd_initial", f(p.get("planned_risk")))
    if f(p.get("risk_usd_initial")) <= 0:
        p["risk_usd_initial"] = f(p.get("quantity")) * (
            distance + entry * ROUND_TRIP_COST_RATE
        )
    if side in ("LONG", "SHORT"):
        sign = 1.0 if side == "LONG" else -1.0
        targets = p.get("targets") or {}
        targets.setdefault(
            "tp1_price", entry + sign * distance * TP1_AT_R
        )
        targets.setdefault(
            "tp2_price", entry + sign * distance * TP2_AT_R
        )
        targets.setdefault(
            "be_price",
            entry + sign * entry * ROUND_TRIP_COST_RATE,
        )
        p["targets"] = targets
    p.setdefault("tp1_taken", bool(p.get("partial_taken")))
    p.setdefault("tp2_taken", False)
    p.setdefault("hold_extensions", 0)
    p.setdefault("trend_flip_exit", False)
    p.setdefault("runner_floor_active", False)
    # V20.2: targets for partials that have not fired yet are moved to
    # the current (later / thinner) profile. This only delays profit
    # taking; stops are never touched here.
    if side in ("LONG", "SHORT"):
        sign = 1.0 if side == "LONG" else -1.0
        targets = p.get("targets") or {}
        if not p.get("tp1_taken"):
            targets["tp1_price"] = entry + sign * distance * TP1_AT_R
        if not p.get("tp2_taken"):
            targets["tp2_price"] = entry + sign * distance * TP2_AT_R
        p["targets"] = targets
    p.setdefault("partial_taken", bool(p.get("tp1_taken")))
    p.setdefault("be_moved", False)
    p.setdefault("trail_active", False)
    # Seed MFE from the legacy best-price excursion when available.
    if "mfe_r" not in p and side in ("LONG", "SHORT"):
        best = f(p.get("best_price"), entry)
        favorable = (best - entry) if side == "LONG" else (entry - best)
        p["mfe_r"] = max(0.0, favorable / distance)
    p.setdefault("mfe_r", 0.0)
    p.setdefault("mae_r", 0.0)
    p.setdefault("entry_event_within_24h", None)
    p.setdefault("entry_event_upcoming_24h", None)
    p.setdefault("entry_event_name", None)
    p.setdefault("event_spanned", False)
    p.setdefault("event_spanned_names", [])
    p.setdefault("source_trader", None)
    p.setdefault("source_trader_label", None)
    p.setdefault("source_entry_price", None)
    p.setdefault("copy_source_event_ts", None)
    p.setdefault("copy_detection_lag_seconds", None)
    p.setdefault("acc_net_pnl", 0.0)
    p.setdefault("acc_fees", f(p.get("entry_fee")))
    p.setdefault("acc_funding", 0.0)
    p.setdefault("saw_stop_loss", False)
    return p


def load_state():
    old = read_json(STATE_FILE, {})

    if not isinstance(old, dict) or not old:
        return default_state()

    s = default_state()

    for key, value in old.items():
        s[key] = value

    s["initial_equity"] = f(
        s.get("initial_equity"), INITIAL_EQUITY
    )
    s["equity"] = f(s.get("equity"), s["initial_equity"])
    s["peak_equity"] = max(
        f(s.get("peak_equity"), s["equity"]),
        s["equity"],
    )
    s["max_drawdown_pct"] = max(
        0.0, f(s.get("max_drawdown_pct"))
    )
    s["realized_pnl"] = f(s.get("realized_pnl"))

    if not isinstance(s.get("trades"), list):
        s["trades"] = old.get("paper_trades", [])
    if not isinstance(s.get("trades"), list):
        s["trades"] = []

    if not isinstance(s.get("equity_history"), list):
        s["equity_history"] = []

    if not isinstance(s.get("events"), list):
        s["events"] = []

    if not isinstance(s.get("stats_by_setup"), dict):
        s["stats_by_setup"] = {}
    if not isinstance(s.get("stats_by_regime"), dict):
        s["stats_by_regime"] = {}

    if not isinstance(s.get("econ_calendar"), dict):
        s["econ_calendar"] = {
            "fetched_ts": 0,
            "events": [],
            "last_error": None,
            "last_stale_warn_date": "",
            "last_block_key": "",
            "last_block_log_ts": 0,
        }
    else:
        _cal = s["econ_calendar"]
        if not isinstance(_cal.get("events"), list):
            _cal["events"] = []
        _cal["fetched_ts"] = max(0, i(_cal.get("fetched_ts")))

    if not isinstance(s.get("copy"), dict):
        s["copy"] = {"last_recheck_ts": 0, "traders": {}}
    else:
        if not isinstance(s["copy"].get("traders"), dict):
            s["copy"]["traders"] = {}
        s["copy"]["last_recheck_ts"] = max(
            0, i(s["copy"].get("last_recheck_ts"))
        )

    pos = s.get("position") or s.get("open_position")
    s["position"] = pos if isinstance(pos, dict) else None

    for key in (
        "last_close_ts",
        "last_entry_candle",
        "consecutive_lock_until",
        "drawdown_pause_until",
    ):
        s[key] = max(0, i(s.get(key)))

    s["consecutive_losses"] = max(
        0, i(s.get("consecutive_losses"))
    )
    s["daily_start_equity"] = f(
        s.get("daily_start_equity"), s["equity"]
    )
    s["daily_loss_lock"] = bool(s.get("daily_loss_lock"))
    s["daily_stop_count"] = max(0, i(s.get("daily_stop_count")))
    s["next_risk_multiplier"] = clamp(
        f(s.get("next_risk_multiplier"), 1.0), 0.1, 1.0
    )

    # Convert old permanent drawdown lock to a timed pause.
    if old.get("drawdown_lock") and not s["drawdown_pause_until"]:
        s["drawdown_pause_until"] = (
            now_ts() + DRAWDOWN_PAUSE_SECONDS
        )
        event(
            s,
            "LEGACY_DRAWDOWN_LOCK_MIGRATED",
            pause_seconds=DRAWDOWN_PAUSE_SECONDS,
        )

    if s["position"]:
        s["position"] = normalize_position(s["position"])

    s["build"] = BUILD
    return s


# ============================================================
# ECONOMIC CALENDAR (event-risk filter, V20.1)
# ============================================================

def _eastern_dst_bounds_utc(year):
    """US Eastern DST window in UTC (2nd Sun Mar 07:00Z .. 1st Sun Nov 06:00Z)."""
    def nth_sunday(month, n):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        delta = (6 - d.weekday()) % 7  # days until the first Sunday
        return d + timedelta(days=delta + 7 * (n - 1))
    return (nth_sunday(3, 2).replace(hour=7),
            nth_sunday(11, 1).replace(hour=6))


def eastern_to_utc_ts(year, month, day, hour=14, minute=0):
    """Epoch seconds for a US Eastern wall-clock time (DST aware)."""
    naive = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    start, end = _eastern_dst_bounds_utc(year)
    as_edt = naive + timedelta(hours=4)
    offset = 4 if start <= as_edt < end else 5
    return int((naive + timedelta(hours=offset)).timestamp())


def fomc_fallback_events():
    """FOMC statement events from the hard-coded official schedule."""
    return [{
        "title": "FOMC: Federal Funds Rate / Statement (official schedule)",
        "category": "FOMC",
        "ts": eastern_to_utc_ts(y, m, d, 14, 0),
        "impact": "High",
        "source": "fomc_fallback",
    } for (y, m, d) in FOMC_STATEMENT_DAYS]


def classify_econ_event(title, country, impact):
    """Map a calendar item to an event category, or None if not targeted.

    Only USD events flagged High impact are targeted. Named categories
    cover the releases that routinely move BTC; any other USD High item
    falls into OTHER_HIGH and is gated with the standard window.
    """
    if (country or "").upper() != "USD":
        return None
    if (impact or "").strip().lower() != "high":
        return None
    t = (title or "").lower()
    if "fomc" in t or "federal funds" in t or "fed chair" in t:
        return "FOMC"
    if "claims" in t:
        return "CLAIMS"
    if ("nonfarm" in t or "non-farm" in t or "unemployment rate" in t
            or "average hourly earnings" in t):
        return "EMPLOYMENT"
    if "cpi" in t or "consumer price" in t:
        return "CPI"
    if "pce" in t:
        return "PCE"
    if "ppi" in t or "producer price" in t:
        return "PPI"
    if "retail sales" in t:
        return "RETAIL"
    if "gdp" in t or "gross domestic" in t:
        return "GDP"
    if "ism" in t:
        return "ISM"
    return "OTHER_HIGH"


def fetch_econ_calendar():
    """Fetch this week's calendar feed and return targeted USD events."""
    req = urllib.request.Request(
        ECON_CALENDAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; btc-paper-engine)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    events = []
    if not isinstance(payload, list):
        return events
    for item in payload:
        if not isinstance(item, dict):
            continue
        category = classify_econ_event(
            item.get("title"), item.get("country"), item.get("impact")
        )
        if not category:
            continue
        try:
            ts = int(datetime.fromisoformat(item.get("date")).timestamp())
        except Exception:
            continue
        events.append({
            "title": item.get("title"),
            "category": category,
            "ts": ts,
            "impact": "High",
            "source": "feed",
        })
    return events


def refresh_econ_calendar(s, now_s=None):
    """Refresh the cached calendar at most once per day (fail-open)."""
    now = int(now_s) if now_s is not None else now_ts()
    cal = s.get("econ_calendar")
    if not isinstance(cal, dict):
        cal = {
            "fetched_ts": 0, "events": [], "last_error": None,
            "last_stale_warn_date": "", "last_block_key": "",
            "last_block_log_ts": 0,
        }
        s["econ_calendar"] = cal
    if now - i(cal.get("fetched_ts")) >= ECON_FETCH_INTERVAL_SECONDS:
        try:
            fresh = fetch_econ_calendar()
            merged = {}
            for e in cal.get("events", []):
                if isinstance(e, dict):
                    merged[(e.get("title"), i(e.get("ts")))] = e
            for e in fresh:
                merged[(e["title"], e["ts"])] = e
            cal["events"] = sorted(
                merged.values(), key=lambda e: i(e.get("ts"))
            )
            cal["fetched_ts"] = now
            cal["last_error"] = None
            event(s, "ECON_CALENDAR_UPDATED", events=len(fresh))
        except Exception as exc:
            cal["last_error"] = f"{type(exc).__name__}: {exc}"
            event(s, "ECON_CALENDAR_FETCH_FAILED", error=cal["last_error"])
    # Keep a rolling window: 3 days back .. 40 days ahead.
    lo, hi = now - 3 * 86400, now + 40 * 86400
    cal["events"] = [
        e for e in cal.get("events", [])
        if isinstance(e, dict) and lo <= i(e.get("ts")) <= hi
    ][-300:]
    fetched = i(cal.get("fetched_ts"))
    if (fetched == 0 or now - fetched > ECON_STALE_SECONDS) \
            and cal.get("last_stale_warn_date") != utc_date():
        cal["last_stale_warn_date"] = utc_date()
        event(
            s, "ECON_CALENDAR_STALE", fetched_ts=fetched,
            note="calendar unknown; fail-open, FOMC fallback still active",
        )
    return cal


def current_econ_events(s):
    """Feed events plus FOMC fallback days not covered by the feed."""
    cal = s.get("econ_calendar") or {}
    feed_events = [
        e for e in cal.get("events", []) if isinstance(e, dict)
    ]
    feed_fomc_ts = [
        i(e.get("ts")) for e in feed_events if e.get("category") == "FOMC"
    ]
    out = list(feed_events)
    for fb in fomc_fallback_events():
        if any(abs(fb["ts"] - ts) <= 12 * 3600 for ts in feed_fomc_ts):
            continue  # the feed already covers this meeting
        out.append(fb)
    out.sort(key=lambda e: i(e.get("ts")))
    return out


def _event_window(e):
    ts = i(e.get("ts"))
    if e.get("category") == "FOMC":
        return (ts - FOMC_EVENT_PRE_MINUTES * 60,
                ts + FOMC_EVENT_POST_MINUTES * 60)
    return (ts - ECON_EVENT_PRE_MINUTES * 60,
            ts + ECON_EVENT_POST_MINUTES * 60)


def event_window_status(s, now_s=None):
    """Is `now` inside a high-impact event window? What is next?"""
    now = int(now_s) if now_s is not None else now_ts()
    cal = s.get("econ_calendar") or {}
    fetched = i(cal.get("fetched_ts"))
    active = None
    nxt = None
    for e in current_econ_events(s):
        lo, hi = _event_window(e)
        if lo <= now <= hi:
            active = e
        if nxt is None and hi >= now:
            nxt = e
    return {
        "blocked": active is not None,
        "active_event": active,
        "next_event": nxt,
        "calendar_stale": fetched == 0 or now - fetched > ECON_STALE_SECONDS,
    }


def entry_event_fields(s, ts_now):
    """Event-proximity tags stamped onto a position at entry time."""
    fields = {
        "entry_event_within_24h": False,
        "entry_event_upcoming_24h": False,
        "entry_event_name": None,
    }
    past = None
    upcoming = None
    for e in current_econ_events(s):
        ts = i(e.get("ts"))
        if ts <= ts_now and ts_now - ts <= 86400:
            past = e  # events are sorted; the last match is the closest
        if upcoming is None and ts_now < ts <= ts_now + 86400:
            upcoming = e
    if past is not None:
        fields["entry_event_within_24h"] = True
        fields["entry_event_name"] = past.get("title")
    if upcoming is not None:
        fields["entry_event_upcoming_24h"] = True
        if fields["entry_event_name"] is None:
            fields["entry_event_name"] = upcoming.get("title")
    return fields


def update_position_event_span(s, now_s=None):
    """Tag the open position if a high-impact release happened mid-hold."""
    pos = s.get("position")
    if not pos:
        return
    now = int(now_s) if now_s is not None else now_ts()
    opened = i(pos.get("opened_timestamp"))
    names = list(pos.get("event_spanned_names") or [])
    spanned = bool(pos.get("event_spanned"))
    for e in current_econ_events(s):
        ts = i(e.get("ts"))
        if opened < ts <= now and e.get("title") not in names:
            names.append(e.get("title"))
            spanned = True
    pos["event_spanned"] = spanned
    pos["event_spanned_names"] = names


def _next_high_event_payload(s):
    st = event_window_status(s)
    e = st["next_event"]
    if not e:
        return None
    active = st["active_event"]
    return {
        "title": e.get("title"),
        "category": e.get("category"),
        "timestamp": i(e.get("ts")),
        "time": iso_of(i(e.get("ts"))),
        "in_window": bool(
            active and i(active.get("ts")) == i(e.get("ts"))
        ),
    }


def _event_risk_payload(s):
    st = event_window_status(s)
    cal = s.get("econ_calendar") or {}
    active = st["active_event"]
    return {
        "blocked": st["blocked"],
        "active_event": active.get("title") if active else None,
        "active_category": active.get("category") if active else None,
        "calendar_updated_ts": i(cal.get("fetched_ts")),
        "calendar_stale": st["calendar_stale"],
        "calendar_last_error": cal.get("last_error"),
    }


# ============================================================
# MARKET DATA
# ============================================================

def fetch_market():
    """Return dict with price, funding rate and premium (HL asset ctx)."""
    mids = post({"type": "allMids"})
    price = f(mids.get(COIN)) if isinstance(mids, dict) else 0.0

    if price <= 0:
        raise RuntimeError("BTC price unavailable from Hyperliquid")

    funding = 0.0
    premium = 0.0
    open_interest = 0.0

    try:
        result = post({"type": "metaAndAssetCtxs"})

        if isinstance(result, list) and len(result) > 1:
            universe = result[0].get("universe", [])
            contexts = result[1]

            for index, asset in enumerate(universe):
                if asset.get("name") == COIN and index < len(contexts):
                    funding = f(contexts[index].get("funding"))
                    premium = f(contexts[index].get("premium"))
                    open_interest = f(contexts[index].get("openInterest"))
                    break

    except Exception as exc:
        print("Funding/premium unavailable:", exc)

    return {
        "price": price,
        "funding": funding,
        "premium": premium,
        "open_interest": open_interest,
    }


def fetch_price_funding():
    m = fetch_market()
    return m["price"], m["funding"]


_INTERVAL_MINUTES = {"15m": 15, "1h": 60, "4h": 240}


def parse_candles(result, interval, limit, now_ms=None):
    """Convert a raw candleSnapshot response to closed-candle dicts."""
    minutes = _INTERVAL_MINUTES[interval]
    end = int(now_ms if now_ms is not None else time.time() * 1000)

    if not isinstance(result, list):
        raise RuntimeError("Invalid candle response: " + interval)

    candles = []

    for item in result:
        candle = {
            "t": i(item.get("t")),
            "T": i(item.get("T")),
            "o": f(item.get("o")),
            "h": f(item.get("h")),
            "l": f(item.get("l")),
            "c": f(item.get("c")),
            "v": f(item.get("v")),
            "n": i(item.get("n")),
        }

        # Exclude an unfinished candle.
        if (
            candle["t"] > 0
            and candle["c"] > 0
            and candle["h"] >= candle["l"] > 0
            and candle["t"] + minutes * 60_000 <= end
        ):
            candles.append(candle)

    candles.sort(key=lambda x: x["t"])
    return candles[-limit:] if limit else candles


def fetch_candles(interval, limit=200):
    minutes = _INTERVAL_MINUTES[interval]

    end = int(time.time() * 1000)
    start = end - minutes * 60_000 * (limit + 5)

    result = post({
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": start,
            "endTime": end,
        },
    })

    candles = parse_candles(result, interval, limit, end)

    if len(candles) < 35:
        raise RuntimeError("Not enough closed candles: " + interval)

    return candles


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if not values:
        return []

    alpha = 2.0 / (period + 1.0)
    result = [values[0]]

    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    changes = [
        values[index] - values[index - 1]
        for index in range(1, len(values))
    ]

    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for index in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[index]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[index]
        ) / period

    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _true_ranges(candles):
    trs = [0.0]
    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["c"]
        trs.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous_close),
            abs(current["l"] - previous_close),
        ))
    return trs


def atr_series(candles, period=14):
    """Wilder ATR aligned with candles (index 0..period-1 are warmup)."""
    trs = _true_ranges(candles)
    out = [0.0] * len(candles)
    if len(candles) <= period:
        return out

    value = sum(trs[1:period + 1]) / period
    out[period] = value
    for index in range(period + 1, len(candles)):
        value = (value * (period - 1) + trs[index]) / period
        out[index] = value
    return out


def atr(candles, period=14):
    series = atr_series(candles, period)
    value = series[-1] if series else 0.0
    if value <= 0:
        return candles[-1]["c"] * 0.002
    return max(value, 0.01)


def atr_percentile(candles, period=14, lookback=100):
    """Percentile rank (0-100) of the current ATR within recent ATRs."""
    series = [x for x in atr_series(candles, period) if x > 0]
    if len(series) < 20:
        return 50.0
    window = series[-lookback:]
    current = window[-1]
    below = sum(1 for x in window if x < current)
    return below / len(window) * 100.0


def bollinger(values, period=20, deviations=2.0):
    if len(values) < period:
        last = values[-1] if values else 0.0
        return {
            "middle": last,
            "upper": last,
            "lower": last,
            "width_pct": 0.0,
            "position": 0.5,
            "state": "UNKNOWN",
        }

    window = values[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    sd = math.sqrt(max(variance, 0.0))

    upper = middle + deviations * sd
    lower = middle - deviations * sd
    width_pct = (
        (upper - lower) / middle * 100.0 if middle else 0.0
    )

    current = values[-1]
    band_range = upper - lower

    position = (
        (current - lower) / band_range
        if band_range > 0 else 0.5
    )

    position = clamp(position, -1.0, 2.0)

    if position >= 1.0:
        state = "ABOVE_UPPER"
    elif position <= 0.0:
        state = "BELOW_LOWER"
    elif position >= 0.8:
        state = "NEAR_UPPER"
    elif position <= 0.2:
        state = "NEAR_LOWER"
    else:
        state = "INSIDE"

    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "width_pct": width_pct,
        "position": position,
        "state": state,
    }


def trend(candles):
    values = [x["c"] for x in candles]
    fast = ema(values, 9)[-1]
    slow = ema(values, 21)[-1]

    if fast > slow * 1.0003:
        return "UP"
    if fast < slow * 0.9997:
        return "DOWN"
    return "SIDEWAYS"


def macd(values, fast=12, slow=26, signal_period=9):
    """Return dict with macd line, signal line and histogram (last bar)."""
    if len(values) < slow + signal_period:
        return {"line": 0.0, "signal": 0.0, "hist": 0.0, "hist_prev": 0.0}

    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal = ema(line, signal_period)
    hist = line[-1] - signal[-1]
    hist_prev = line[-2] - signal[-2] if len(line) >= 2 else hist

    return {
        "line": line[-1],
        "signal": signal[-1],
        "hist": hist,
        "hist_prev": hist_prev,
    }


def adx(candles, period=14):
    """Wilder ADX. Returns last ADX value (0-100)."""
    n = len(candles)
    if n < 2 * period + 2:
        return 0.0

    trs = []
    plus_dm = []
    minus_dm = []
    for idx in range(1, n):
        cur = candles[idx]
        prev = candles[idx - 1]
        up = cur["h"] - prev["h"]
        down = prev["l"] - cur["l"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))

    def _wilder_smooth(values):
        out = []
        total = sum(values[:period])
        out.append(total)
        for value in values[period:]:
            total = total - total / period + value
            out.append(total)
        return out

    s_tr = _wilder_smooth(trs)
    s_plus = _wilder_smooth(plus_dm)
    s_minus = _wilder_smooth(minus_dm)

    dxs = []
    for s_t, s_p, s_m in zip(s_tr, s_plus, s_minus):
        if s_t <= 0:
            dxs.append(0.0)
            continue
        plus_di = 100.0 * s_p / s_t
        minus_di = 100.0 * s_m / s_t
        denom = plus_di + minus_di
        dxs.append(
            100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0
        )

    if len(dxs) < period:
        return dxs[-1] if dxs else 0.0

    value = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        value = (value * (period - 1) + dx) / period
    return value


def daily_vwap(candles):
    """Session (UTC day) VWAP from 15m candles, using the last bar's day."""
    if not candles:
        return 0.0
    last_day = date_of(candles[-1]["t"] // 1000)
    num = 0.0
    den = 0.0
    for candle in candles:
        if date_of(candle["t"] // 1000) != last_day:
            continue
        typical = (candle["h"] + candle["l"] + candle["c"]) / 3.0
        num += typical * candle["v"]
        den += candle["v"]
    if den <= 0:
        return candles[-1]["c"]
    return num / den


def relative_volume(candles, period=20):
    """Last bar volume vs the mean of the preceding `period` bars."""
    if len(candles) < period + 1:
        return 1.0
    window = [c["v"] for c in candles[-period - 1:-1]]
    mean_v = sum(window) / len(window) if window else 0.0
    if mean_v <= 0:
        return 1.0
    return candles[-1]["v"] / mean_v


def swing_points(candles, k=3, lookback=160):
    """Confirmed fractal swings: list of {index, kind, price, t}.

    A swing at index j is only included once bars up to j+k exist,
    so no future information leaks into decisions at the last bar.
    """
    start = max(k, len(candles) - lookback)
    out = []
    for j in range(start, len(candles) - k):
        high = candles[j]["h"]
        low = candles[j]["l"]
        is_high = all(
            candles[j + d]["h"] <= high for d in range(-k, k + 1) if d != 0
        ) and any(
            candles[j + d]["h"] < high for d in range(-k, k + 1) if d != 0
        )
        is_low = all(
            candles[j + d]["l"] >= low for d in range(-k, k + 1) if d != 0
        ) and any(
            candles[j + d]["l"] > low for d in range(-k, k + 1) if d != 0
        )
        if is_high:
            out.append({
                "index": j, "kind": "HIGH", "price": high,
                "t": candles[j]["t"],
            })
        if is_low:
            out.append({
                "index": j, "kind": "LOW", "price": low,
                "t": candles[j]["t"],
            })
    return out


def market_structure(candles, price):
    """Swing-based structure bias + nearest support/resistance zones."""
    swings = swing_points(candles)
    highs = [s for s in swings if s["kind"] == "HIGH"]
    lows = [s for s in swings if s["kind"] == "LOW"]

    structure = "MIXED"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1]["price"] > highs[-2]["price"]
        hl = lows[-1]["price"] > lows[-2]["price"]
        lh = highs[-1]["price"] < highs[-2]["price"]
        ll = lows[-1]["price"] < lows[-2]["price"]
        if hh and hl:
            structure = "UP"
        elif lh and ll:
            structure = "DOWN"

    # Round numbers on a 1000 USD grid act as psychological zones
    # (a 500 grid would sit inside 1.5R almost permanently at current
    # ATR levels and veto nearly every entry by construction).
    grid = 1000.0
    round_above = (math.floor(price / grid) + 1) * grid
    round_below = math.floor(price / grid) * grid
    if round_below >= price:
        round_below -= grid

    above = [s["price"] for s in highs if s["price"] > price]
    above.append(round_above)
    below = [s["price"] for s in lows if s["price"] < price]
    below.append(round_below)

    resistance = min(above) if above else None
    support = max(below) if below else None

    return {
        "structure": structure,
        "support": support,
        "resistance": resistance,
        "last_swing_high": highs[-1]["price"] if highs else None,
        "last_swing_low": lows[-1]["price"] if lows else None,
        "prev_swing_high": highs[-2]["price"] if len(highs) >= 2 else None,
        "prev_swing_low": lows[-2]["price"] if len(lows) >= 2 else None,
        "recent_swing_highs": [s["price"] for s in highs[-4:]],
        "recent_swing_lows": [s["price"] for s in lows[-4:]],
        "swings": swings[-12:],
    }


# ============================================================
# MARKET CONTEXT: REGIME + STRUCTURE + CONFLUENCE
# ============================================================

def classify_regime(adx_value, atr_pct, htf_bias, structure_bias):
    if atr_pct >= ATR_PCT_HIGH_VOL:
        return "HIGH_VOLATILITY"
    if adx_value >= ADX_TREND:
        direction = structure_bias if structure_bias in ("UP", "DOWN") \
            else htf_bias
        if direction == "UP":
            return "TREND_UP"
        if direction == "DOWN":
            return "TREND_DOWN"
        return "NEUTRAL"
    if adx_value < ADX_RANGE:
        return "RANGE"
    return "NEUTRAL"


def build_context(c15, c1, c4, funding, premium=0.0):
    values = [x["c"] for x in c15]
    ema9 = ema(values, 9)
    ema21 = ema(values, 21)

    current = values[-1]
    previous = values[-2]

    rv = rsi(values)
    av = atr(c15)
    atr_pct = atr_percentile(c15)
    adx_value = adx(c15)
    mc = macd(values)
    bb = bollinger(values)
    vwap = daily_vwap(c15)
    relvol = relative_volume(c15)

    trends = {
        "15m": trend(c15),
        "1h": trend(c1),
        "4h": trend(c4),
    }

    htf_points = 0
    htf_points += 1 if trends["1h"] == "UP" else (
        -1 if trends["1h"] == "DOWN" else 0
    )
    htf_points += 2 if trends["4h"] == "UP" else (
        -2 if trends["4h"] == "DOWN" else 0
    )
    htf_bias = "UP" if htf_points >= 2 else (
        "DOWN" if htf_points <= -2 else "NEUTRAL"
    )

    structure = market_structure(c15, current)
    regime = classify_regime(
        adx_value, atr_pct, htf_bias, structure["structure"]
    )

    # Fibonacci levels from recent 1h candles (dashboard/context only).
    window = c1[-min(80, len(c1)):]
    swing_high = max(x["h"] for x in window)
    swing_low = min(x["l"] for x in window)
    span = swing_high - swing_low
    fib_levels = {
        key: swing_high - span * ratio
        for key, ratio in {
            "0.236": 0.236,
            "0.382": 0.382,
            "0.500": 0.500,
            "0.618": 0.618,
            "0.786": 0.786,
        }.items()
    }

    return {
        "price": current,
        "previous_close": previous,
        "last_candle": c15[-1],
        "prev_candle": c15[-2],
        "candles": c15,
        "ema9": ema9[-1],
        "ema21": ema21[-1],
        "ema9_prev": ema9[-2],
        "rsi": rv,
        "atr": av,
        "atr_percentile": atr_pct,
        "adx": adx_value,
        "macd": mc,
        "bollinger": bb,
        "vwap": vwap,
        "relative_volume": relvol,
        "trend_15m": trends["15m"],
        "trend_1h": trends["1h"],
        "trend_4h": trends["4h"],
        "htf_bias": htf_bias,
        "structure": structure,
        "regime": regime,
        "funding": funding,
        "premium": premium,
        "fibonacci": {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "levels": fib_levels,
            "trend": trends["1h"],
        },
    }


def _crowding_points(ctx, side):
    """Funding/premium crowding: fade crowded positioning."""
    points = 0
    funding = ctx["funding"]
    premium = ctx["premium"]
    # Funding is hourly on Hyperliquid; +/-0.03% is clearly crowded.
    if side == "LONG":
        if funding >= 0.0003:
            points -= 2
        elif funding <= -0.0003:
            points += 1
        if premium >= 0.0005:
            points -= 1
        elif premium <= -0.0005:
            points += 1
    else:
        if funding <= -0.0003:
            points -= 2
        elif funding >= 0.0003:
            points += 1
        if premium <= -0.0005:
            points -= 1
        elif premium >= 0.0005:
            points += 1
    return points


def score_breakdown(ctx, side, setup):
    """Confluence scoring for one direction. Returns (total, breakdown)."""
    sign = 1 if side == "LONG" else -1
    bd = {}

    # Higher-timeframe bias.
    pts = 0
    t1 = ctx["trend_1h"]
    t4 = ctx["trend_4h"]
    want = "UP" if side == "LONG" else "DOWN"
    against = "DOWN" if side == "LONG" else "UP"
    if setup != "C":
        if t1 == want:
            pts += 2
        elif t1 == against:
            pts -= 2
        if t4 == want:
            pts += 2
        elif t4 == against:
            pts -= 2
    bd["htf_bias"] = pts

    # Market structure.
    pts = 0
    structure = ctx["structure"]["structure"]
    if setup != "C":
        if structure == want:
            pts += 2
        elif structure == against:
            pts -= 2
    bd["structure"] = pts

    # Momentum: MACD histogram direction + expansion.
    pts = 0
    hist = ctx["macd"]["hist"]
    hist_prev = ctx["macd"]["hist_prev"]
    if sign * hist > 0:
        pts += 1
        if sign * (hist - hist_prev) > 0:
            pts += 1
    elif sign * hist < 0 and sign * (hist - hist_prev) < 0:
        pts -= 1
    bd["macd_momentum"] = pts

    # RSI band / extremes.
    pts = 0
    rv = ctx["rsi"]
    if setup == "C":
        if side == "LONG" and rv <= 32:
            pts += 2
        elif side == "SHORT" and rv >= 68:
            pts += 2
        # A Bollinger-band extreme confirms the mean-reversion edge.
        bb_pos = ctx["bollinger"]["position"]
        if (side == "LONG" and bb_pos <= 0.05) or (
            side == "SHORT" and bb_pos >= 0.95
        ):
            pts += 1
    else:
        if side == "LONG":
            if 40 <= rv <= 70:
                pts += 1
            elif rv > 78:
                pts -= 1
        else:
            if 30 <= rv <= 60:
                pts += 1
            elif rv < 22:
                pts -= 1
    bd["rsi"] = pts

    # Relative volume.
    pts = 0
    if ctx["relative_volume"] >= 1.3:
        pts += 1
    bd["volume"] = pts

    # VWAP position (trend setups only; range fades ignore VWAP).
    pts = 0
    if setup != "C" and ctx["vwap"] > 0:
        if side == "LONG" and ctx["price"] >= ctx["vwap"]:
            pts += 1
        elif side == "SHORT" and ctx["price"] <= ctx["vwap"]:
            pts += 1
    bd["vwap"] = pts

    # Crowding (funding / premium).
    bd["crowding"] = _crowding_points(ctx, side)

    # Volatility fit.
    pts = 0
    if 20 <= ctx["atr_percentile"] < 85:
        pts += 1
    elif ctx["atr_percentile"] >= 95:
        pts -= 1
    bd["volatility_fit"] = pts

    # Range quality (setup C only): a genuinely flat ADX is the fade's
    # best friend; trend-aligned components are structurally zero for
    # counter-trend fades, so range quality carries this weight instead.
    if setup == "C":
        bd["range_quality"] = 1 if ctx["adx"] < 17 else 0

    total = sum(bd.values())
    return total, bd


# ============================================================
# SETUP EVALUATION
# ============================================================

def _finalize_candidate(ctx, side, setup, stop_anchor, breakdown_extra=None):
    """Apply structural-stop clamping, cost filter and zone-room gate."""
    price = ctx["price"]
    av = ctx["atr"]
    entry = (
        price * (1 + SLIPPAGE_RATE)
        if side == "LONG"
        else price * (1 - SLIPPAGE_RATE)
    )

    raw_distance = abs(entry - stop_anchor)
    min_distance = max(
        STOP_ATR_MIN * av,
        entry * (FEE_RATE + SLIPPAGE_RATE) * 4.0,
    )
    max_distance = STOP_ATR_MAX * av
    distance = clamp(raw_distance, min_distance, max_distance)

    if side == "LONG":
        stop = entry - distance
    else:
        stop = entry + distance

    # Cost filter: the risk distance (our unit of expected movement)
    # must be a healthy multiple of the round-trip cost per unit.
    round_trip_cost = entry * ROUND_TRIP_COST_RATE
    if distance < COST_FILTER_MULT * round_trip_cost:
        return None, (
            f"コストフィルター（想定値幅が往復コストの"
            f"{COST_FILTER_MULT:.0f}倍未満）"
        )

    # Zone room: never enter with < 1.5R of room to the opposing zone.
    # (Setup D opts out via _finalize_candidate_no_zone: the source
    # trader's own analysis replaces this gate.)
    structure = ctx["structure"]
    if side == "LONG":
        opposing = structure["resistance"]
        room = (opposing - entry) if opposing else None
    else:
        opposing = structure["support"]
        room = (entry - opposing) if opposing else None

    room_r = (room / distance) if room is not None else None
    if room_r is not None and room_r < MIN_ZONE_ROOM_R:
        return None, "対向ゾーンまでの距離が1.5R未満"

    total, bd = score_breakdown(ctx, side, setup)
    if breakdown_extra:
        for key, value in breakdown_extra.items():
            bd[key] = bd.get(key, 0) + value
        total = sum(bd.values())

    sign = 1.0 if side == "LONG" else -1.0
    candidate = {
        "side": side,
        "setup": setup,
        "regime": ctx["regime"],
        "score": total,
        "score_breakdown": bd,
        "entry_price": entry,
        "reference_price": price,
        "stop_price": stop,
        "initial_stop_price": stop,
        "risk_distance": distance,
        "targets": {
            "tp1_price": entry + sign * distance * TP1_AT_R,
            "tp2_price": entry + sign * distance * TP2_AT_R,
            "be_price": entry + sign * entry * ROUND_TRIP_COST_RATE,
        },
        "zone_room_r": room_r,
        "support": structure["support"],
        "resistance": structure["resistance"],
        "atr": av,
    }
    return candidate, None


def _candidate_a(ctx, side):
    """Setup A: trend pullback to EMA21 / VWAP / zone + reversal trigger."""
    regime = ctx["regime"]
    want_regime = "TREND_UP" if side == "LONG" else "TREND_DOWN"
    if regime not in (want_regime, "NEUTRAL", "HIGH_VOLATILITY"):
        return None, "レジーム不一致"

    want = "UP" if side == "LONG" else "DOWN"
    against = "DOWN" if side == "LONG" else "UP"
    if ctx["trend_1h"] != want or ctx["trend_4h"] == against:
        return None, "上位足バイアス不一致"

    candles = ctx["candles"]
    av = ctx["atr"]
    price = ctx["price"]
    last = candles[-1]
    prev = candles[-2]

    anchors = [ctx["ema21"], ctx["vwap"]]
    zone = (
        ctx["structure"]["support"] if side == "LONG"
        else ctx["structure"]["resistance"]
    )
    if zone:
        anchors.append(zone)
    anchors = [a for a in anchors if a and a > 0]

    if side == "LONG":
        recent_extreme = min(c["l"] for c in candles[-3:])
        pulled = any(
            abs(recent_extreme - anchor) <= 0.35 * av for anchor in anchors
        )
        held = price >= min(anchors) - 0.5 * av if anchors else False
        trigger = (
            last["c"] > prev["h"]
            or (last["c"] > ctx["ema9"] and prev["c"] <= ctx["ema9_prev"])
        )
        swing_anchor = ctx["structure"]["last_swing_low"]
        fallback = min(c["l"] for c in candles[-10:])
    else:
        recent_extreme = max(c["h"] for c in candles[-3:])
        pulled = any(
            abs(recent_extreme - anchor) <= 0.35 * av for anchor in anchors
        )
        held = price <= max(anchors) + 0.5 * av if anchors else False
        trigger = (
            last["c"] < prev["l"]
            or (last["c"] < ctx["ema9"] and prev["c"] >= ctx["ema9_prev"])
        )
        swing_anchor = ctx["structure"]["last_swing_high"]
        fallback = max(c["h"] for c in candles[-10:])

    if not pulled:
        return None, "押し目/戻りが未形成"
    if not held:
        return None, "押し目が深すぎる"
    if not trigger:
        return None, "反転トリガー未発生"

    anchor = swing_anchor if swing_anchor else fallback
    if side == "LONG":
        stop_anchor = anchor - STOP_STRUCT_BUFFER_ATR * av
    else:
        stop_anchor = anchor + STOP_STRUCT_BUFFER_ATR * av

    extra = {"zone_value": 1}
    candidate, reason = _finalize_candidate(
        ctx, side, "A", stop_anchor, extra
    )
    if candidate is None:
        return None, reason
    threshold = MIN_SCORE + (1 if regime == "HIGH_VOLATILITY" else 0)
    if candidate["score"] < threshold:
        return None, f"スコア不足 {candidate['score']}<{threshold}"
    return candidate, None


def _candidate_b(ctx, side):
    """Setup B: volume-confirmed breakout of the prior 20-bar range."""
    if ctx["regime"] == "RANGE":
        return None, "レンジレジームではブレイクアウト禁止"

    against = "DOWN" if side == "LONG" else "UP"
    if ctx["trend_4h"] == against or ctx["trend_1h"] == against:
        return None, "上位足が逆行"

    candles = ctx["candles"]
    last = candles[-1]
    prior = candles[-21:-1]
    if len(prior) < 20:
        return None, "データ不足"

    if ctx["relative_volume"] < 1.5:
        return None, "出来高不足"

    body = abs(last["c"] - last["o"])
    span = max(last["h"] - last["l"], 0.01)
    if body / span < 0.55:
        return None, "ブレイク足の実体が弱い"

    av = ctx["atr"]
    if side == "LONG":
        level = max(c["h"] for c in prior)
        if not last["c"] > level:
            return None, "ブレイク未成立"
        stop_anchor = level - 0.25 * av
    else:
        level = min(c["l"] for c in prior)
        if not last["c"] < level:
            return None, "ブレイク未成立"
        stop_anchor = level + 0.25 * av

    candidate, reason = _finalize_candidate(ctx, side, "B", stop_anchor)
    if candidate is None:
        return None, reason
    if candidate["score"] < MIN_SCORE:
        return None, f"スコア不足 {candidate['score']}<{MIN_SCORE}"
    return candidate, None


def _candidate_c(ctx, side):
    """Setup C: range fade at a zone edge with RSI extreme (half risk)."""
    if ctx["regime"] != "RANGE":
        return None, "レンジレジーム限定"

    candles = ctx["candles"]
    last = candles[-1]
    prev = candles[-2]
    av = ctx["atr"]
    structure = ctx["structure"]

    # The extreme is checked over the last 3 bars: on the confirmation
    # (trigger) bar itself, RSI/BB have usually already snapped back,
    # so requiring the extreme on the trigger bar would veto the very
    # reversal the setup is designed to catch.
    closes = [c["c"] for c in candles]
    extremes = []
    for back in range(3):
        sub = closes[:len(closes) - back] if back else closes
        if len(sub) < 30:
            break
        extremes.append((rsi(sub), bollinger(sub)["position"]))

    if side == "LONG":
        zone = structure["support"]
        if not zone:
            return None, "サポートゾーンなし"
        near = min(c["l"] for c in candles[-3:]) <= zone + 0.3 * av
        extreme = any(
            rv <= 32 or pos <= 0.10 for rv, pos in extremes
        )
        trigger = last["c"] > last["o"] and last["c"] > prev["c"]
        stop_anchor = zone - STOP_STRUCT_BUFFER_ATR * av
    else:
        zone = structure["resistance"]
        if not zone:
            return None, "レジスタンスゾーンなし"
        near = max(c["h"] for c in candles[-3:]) >= zone - 0.3 * av
        extreme = any(
            rv >= 68 or pos >= 0.90 for rv, pos in extremes
        )
        trigger = last["c"] < last["o"] and last["c"] < prev["c"]
        stop_anchor = zone + STOP_STRUCT_BUFFER_ATR * av

    if not near:
        return None, "ゾーン端に未到達"
    if not extreme:
        return None, "RSI/BB極値でない"
    if not trigger:
        return None, "反転トリガー未発生"

    extra = {"zone_value": 1}
    candidate, reason = _finalize_candidate(
        ctx, side, "C", stop_anchor, extra
    )
    if candidate is None:
        return None, reason
    if candidate["score"] < MIN_SCORE_C:
        return None, f"スコア不足 {candidate['score']}<{MIN_SCORE_C}"
    return candidate, None


_SETUP_FUNCS = {"A": _candidate_a, "B": _candidate_b, "C": _candidate_c}


def _finalize_candidate_no_zone(ctx, side, stop_anchor):
    """_finalize_candidate with the zone-room gate relaxed (setup D).

    The cost filter and stop clamping inside _finalize_candidate
    still apply; only the opposing-zone room check is skipped.
    """
    global MIN_ZONE_ROOM_R
    saved = MIN_ZONE_ROOM_R
    MIN_ZONE_ROOM_R = 0.0
    try:
        candidate, _reason = _finalize_candidate(ctx, side, "D",
                                                 stop_anchor)
    finally:
        MIN_ZONE_ROOM_R = saved
    return candidate


def evaluate_entry(ctx, allowed_setups=("A", "B", "C")):
    """Return (best_candidate, per_side_scores, rejection_notes)."""
    best = None
    side_scores = {"LONG": 0, "SHORT": 0}
    notes = []

    for side in ("LONG", "SHORT"):
        for setup in ("A", "B", "C"):
            if setup not in allowed_setups:
                continue
            candidate, reason = _SETUP_FUNCS[setup](ctx, side)
            if candidate is not None:
                side_scores[side] = max(
                    side_scores[side], candidate["score"]
                )
                if best is None or candidate["score"] > best["score"]:
                    best = candidate
            elif reason:
                notes.append(f"{setup}/{side}: {reason}")

    return best, side_scores, notes



# ============================================================
# COPY TRADING (Setup D, V20.3)
# ============================================================

def _copy_config_traders():
    """Validated, enabled entries from the COPY_TRADERS config list."""
    out = []
    for t in COPY_TRADERS:
        if not isinstance(t, dict):
            continue
        addr = str(t.get("address", "")).lower()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        if not t.get("enabled", True):
            continue
        out.append({
            "address": addr,
            "label": str(t.get("label", addr[:10])),
        })
    return out


def _copy_book(s):
    book = s.setdefault("copy", {"last_recheck_ts": 0, "traders": {}})
    if not isinstance(book.get("traders"), dict):
        book["traders"] = {}
    return book


def _copy_rec(book, meta):
    return book["traders"].setdefault(meta["address"], {
        "label": meta["label"],
        "snapshot": None,
        "auto_disabled": False,
        "disabled_reason": None,
        "disabled_ts": 0,
        "last_error_ts": 0,
        "reeval": None,
    })


def fetch_trader_btc_position(address):
    """Current BTC position of a trader; None on API error.

    Returns {"side": "LONG"/"SHORT"/None, "szi": float,
    "entry_px": float|None}. side=None means confirmed flat.
    """
    try:
        data = post({"type": "clearinghouseState", "user": address})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for ap in data.get("assetPositions", []):
        p = ap.get("position", {})
        if p.get("coin") == COIN:
            szi = f(p.get("szi"))
            if szi == 0:
                return {"side": None, "szi": 0.0, "entry_px": None}
            return {
                "side": "LONG" if szi > 0 else "SHORT",
                "szi": szi,
                "entry_px": f(p.get("entryPx")) or None,
            }
    return {"side": None, "szi": 0.0, "entry_px": None}


def fetch_trader_fills(address, start_ms, end_ms, coin=None):
    """userFillsByTime; None on API error. Optionally one coin only."""
    try:
        data = post({
            "type": "userFillsByTime",
            "user": address,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
        })
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    if coin is not None:
        data = [x for x in data if x.get("coin") == coin]
    return data


def update_copy_tracking(s, now_s):
    """Poll tracked traders; return fresh/stale entry signals.

    Snapshot diffing (per trader, stored in state):
      flat -> LONG/SHORT : "OPEN"
      LONG <-> SHORT     : "FLIP" (fresh open of the new side)
      LONG/SHORT -> flat : source exit (no entry signal; open copy
                           positions are closed by the source-exit
                           check in main)
      same side          : no signal (scaling in/out is not chased)

    The source event time is estimated from the trader's BTC fills
    since the previous snapshot. If no fill is found in that window
    (e.g. first poll finds a long-standing position), the signal is
    marked stale so we never copy an old entry blind.
    """
    signals = []
    book = _copy_book(s)
    configured = {t["address"]: t for t in _copy_config_traders()}

    # Tracking follows the config list exactly.
    for addr in list(book["traders"].keys()):
        if addr not in configured:
            del book["traders"][addr]

    for addr, meta in configured.items():
        rec = _copy_rec(book, meta)
        rec["label"] = meta["label"]
        if rec.get("auto_disabled"):
            continue

        current = fetch_trader_btc_position(addr)
        if current is None:
            rec["last_error_ts"] = now_s
            continue

        prev = rec.get("snapshot") or {}
        prev_side = prev.get("side")
        cur_side = current["side"]

        event_ts = prev.get("event_ts")
        if cur_side is not None and cur_side != prev_side:
            since = prev.get("updated_ts") or (now_s - 3600)
            fills = fetch_trader_fills(
                addr, since * 1000, now_s * 1000, coin=COIN
            )
            opens = [
                x for x in (fills or [])
                if str(x.get("dir", "")).startswith("Open")
            ]
            pool = opens or (fills or [])
            event_ts = (
                max(int(x["time"]) for x in pool) / 1000.0
                if pool else None
            )
            age = (now_s - event_ts) if event_ts is not None else None
            kind = "OPEN" if prev_side is None else "FLIP"
            signals.append({
                "kind": kind,
                "address": addr,
                "label": meta["label"],
                "side": cur_side,
                "source_entry_price": current["entry_px"],
                "source_event_ts": event_ts,
                "detected_ts": now_s,
                "age_seconds": age,
                "fresh": (
                    age is not None
                    and age <= COPY_SIGNAL_MAX_AGE_SECONDS
                ),
            })
            event(
                s, "COPY_SIGNAL_" + kind,
                trader=addr, label=meta["label"], side=cur_side,
                source_entry_price=current["entry_px"],
                source_event_ts=(
                    int(event_ts) if event_ts is not None else None
                ),
                age_seconds=(int(age) if age is not None else None),
                fresh=signals[-1]["fresh"],
            )
        elif cur_side is None and prev_side is not None:
            event(
                s, "COPY_SOURCE_FLAT",
                trader=addr, label=meta["label"],
                previous_side=prev_side,
            )

        rec["snapshot"] = {
            "side": cur_side,
            "szi": current["szi"],
            "entry_px": current["entry_px"],
            "event_ts": event_ts,
            "updated_ts": now_s,
        }

    return signals


def copy_source_side(s, pos):
    """Last confirmed side of the position's source trader.

    Returns "LONG"/"SHORT", None (confirmed flat), or "UNKNOWN"
    when we have no snapshot (never exit on missing data).
    """
    addr = pos.get("source_trader")
    if not addr:
        return "UNKNOWN"
    rec = (_copy_book(s)["traders"].get(addr) or {})
    snap = rec.get("snapshot")
    if not snap:
        return "UNKNOWN"
    return snap.get("side")


def reevaluate_copy_traders(s, now_s):
    """Daily re-evaluation: auto-disable inactive or decaying traders.

    Rules (trailing COPY_INACTIVE_DAYS window, all coins for the
    activity check, fills capped by the API at the most recent 2000):
      * no fills at all in the window -> disabled (inactive)
      * >= COPY_MIN_CLOSES_30D closes and (win rate <
        COPY_MIN_WIN_RATE_30D or realized PnL < 0) -> disabled
    API errors never disable anyone (fail-open).
    """
    book = _copy_book(s)
    if now_s - i(book.get("last_recheck_ts"))             < COPY_RECHECK_INTERVAL_SECONDS:
        return
    book["last_recheck_ts"] = now_s

    start_ms = (now_s - COPY_INACTIVE_DAYS * 86400) * 1000
    for meta in _copy_config_traders():
        addr = meta["address"]
        rec = _copy_rec(book, meta)
        if rec.get("auto_disabled"):
            continue
        fills = fetch_trader_fills(addr, start_ms, now_s * 1000)
        if fills is None:
            continue
        closes = [
            f(x.get("closedPnl"))
            for x in fills
            if f(x.get("closedPnl")) != 0.0
        ]
        wins = sum(1 for p in closes if p > 0)
        win_rate = wins / len(closes) if closes else 0.0
        realized = sum(closes)
        rec["reeval"] = {
            "ts": now_s,
            "fills": len(fills),
            "closes": len(closes),
            "win_rate": round(win_rate, 4),
            "realized_pnl": round(realized, 2),
        }
        reason = None
        if not fills:
            reason = "inactive_30d"
        elif len(closes) >= COPY_MIN_CLOSES_30D and (
            win_rate < COPY_MIN_WIN_RATE_30D or realized < 0
        ):
            reason = "performance_decay_30d"
        if reason:
            rec["auto_disabled"] = True
            rec["disabled_reason"] = reason
            rec["disabled_ts"] = now_s
            event(
                s, "COPY_TRADER_AUTO_DISABLED",
                trader=addr, label=meta["label"], reason=reason,
                closes=len(closes), win_rate=round(win_rate, 4),
                realized_pnl=round(realized, 2),
            )


def _candidate_copy(ctx, side, signal):
    """Setup D candidate: direction from the source trader.

    Everything else stays house rules: structural stop from the
    recent swing (clamped exactly like setup A) and the cost filter.
    There is deliberately no confluence score and no zone-room gate:
    the source trader's analysis replaces ours, so those gates would
    veto the very signals this setup exists to follow. Risk controls,
    the event filter and the V20.2 exit engine still apply unchanged.
    """
    candles = ctx["candles"]
    av = ctx["atr"]
    structure = ctx["structure"]
    if side == "LONG":
        anchor = structure["last_swing_low"] or min(
            c["l"] for c in candles[-10:]
        )
        stop_anchor = anchor - STOP_STRUCT_BUFFER_ATR * av
    else:
        anchor = structure["last_swing_high"] or max(
            c["h"] for c in candles[-10:]
        )
        stop_anchor = anchor + STOP_STRUCT_BUFFER_ATR * av

    candidate, reason = _finalize_candidate(ctx, side, "D", stop_anchor)
    if candidate is None:
        # _finalize_candidate also enforces the zone-room gate; for a
        # copy signal the only acceptable house vetoes are the cost
        # filter and stop construction, so rebuild without the gate.
        if reason and "対向ゾーン" not in reason:
            return None, reason
        candidate = _finalize_candidate_no_zone(ctx, side, stop_anchor)
        if candidate is None:
            return None, "コストフィルター（コピー）"
    candidate["score"] = 0
    candidate["score_breakdown"] = {"copy_signal": 0}
    candidate["source_trader"] = signal["address"]
    candidate["source_trader_label"] = signal["label"]
    candidate["source_entry_price"] = signal.get("source_entry_price")
    candidate["copy_source_event_ts"] = signal.get("source_event_ts")
    candidate["copy_detection_lag_seconds"] = (
        signal["detected_ts"] - signal["source_event_ts"]
        if signal.get("source_event_ts") is not None else None
    )
    return candidate, None


def _copy_dashboard_payload(s):
    book = s.get("copy") or {}
    traders = []
    for meta in _copy_config_traders():
        rec = (book.get("traders") or {}).get(meta["address"]) or {}
        snap = rec.get("snapshot") or {}
        traders.append({
            "address": meta["address"],
            "label": meta["label"],
            "side": snap.get("side"),
            "source_entry_price": snap.get("entry_px"),
            "auto_disabled": bool(rec.get("auto_disabled")),
            "disabled_reason": rec.get("disabled_reason"),
            "reeval": rec.get("reeval"),
        })
    return {
        "enabled": bool(traders),
        "follow_source_exit": COPY_FOLLOW_SOURCE_EXIT,
        "signal_max_age_seconds": COPY_SIGNAL_MAX_AGE_SECONDS,
        "traders": traders,
    }


# ============================================================
# POSITION ACCOUNTING
# ============================================================

def unrealized(pos, price):
    if not pos:
        return 0.0

    quantity = f(pos.get("quantity"))
    entry = f(pos.get("entry_price"))

    if pos.get("side") == "LONG":
        return (price - entry) * quantity

    return (entry - price) * quantity


def unrealized_net(pos, price):
    """Unrealized PnL net of the estimated cost of closing now."""
    if not pos:
        return 0.0
    gross = unrealized(pos, price)
    exit_cost = abs(f(pos.get("quantity")) * price) * (
        FEE_RATE + SLIPPAGE_RATE
    )
    return gross - exit_cost


def size_position(equity, entry, stop, risk_fraction):
    distance = abs(entry - stop)
    risk_budget = max(0.0, equity) * risk_fraction

    estimated_cost_per_unit = distance + entry * ROUND_TRIP_COST_RATE

    if equity <= 0 or distance <= 0 or estimated_cost_per_unit <= 0:
        return None

    risk_quantity = risk_budget / estimated_cost_per_unit

    margin_quantity = (
        equity * MAX_MARGIN_FRACTION * LEVERAGE / entry
    )

    quantity = min(risk_quantity, margin_quantity)

    if quantity <= 0:
        return None

    notional = quantity * entry

    planned_risk = quantity * estimated_cost_per_unit

    return {
        "quantity": quantity,
        "margin_used": notional / LEVERAGE,
        "notional": notional,
        "planned_risk": planned_risk,
        "effective_risk_pct": planned_risk / equity * 100,
    }


def apply_funding(s, funding, now_s=None):
    """
    Approximate paper funding cost using the current funding rate.
    Hyperliquid funding is paid hourly. This estimate assumes the
    current rate applies throughout elapsed time; historical rates
    are not reconstructed, so this is an approximation.
    """
    pos = s.get("position")
    if not pos:
        return

    current_time = int(now_s) if now_s is not None else now_ts()
    last_time = i(pos.get("last_funding_ts"), current_time)

    elapsed = max(0, current_time - last_time)
    if elapsed < 60:
        return

    hours = elapsed / 3600.0
    notional = abs(f(pos.get("quantity")) * f(pos.get("entry_price")))

    # Positive funding: longs pay, shorts receive.
    if pos.get("side") == "LONG":
        cash_effect = -notional * funding * hours
    else:
        cash_effect = notional * funding * hours

    s["equity"] += cash_effect
    pos["funding_pnl"] = f(pos.get("funding_pnl")) + cash_effect
    pos["last_funding_ts"] = current_time

    if abs(cash_effect) > 0.000001:
        event(
            s,
            "PAPER_FUNDING_ESTIMATE",
            amount=round(cash_effect, 8),
            hours=round(hours, 4),
        )


def close_position(s, price, reason, fraction=1.0, extra_slippage=0.0,
                   now_s=None):
    """Close `fraction` of the current quantity at `price` (pre-slippage).

    Records are complete: build, origin build, setup, regime, score
    breakdown, entry/stop/targets, planned risk, effective risk pct,
    MFE/MAE, fees, funding, exit reason and R multiple are all saved.
    Round-trip aggregates (win/loss, consecutive losses, daily stop
    count, per-setup / per-regime stats) update on the final close.
    """
    pos = s.get("position")
    if not pos:
        return False

    fraction = clamp(fraction, 0.0, 1.0)
    if fraction <= 0:
        return False

    old_quantity = f(pos.get("quantity"))
    if old_quantity <= 0:
        s["position"] = None
        return False

    ts_now = int(now_s) if now_s is not None else now_ts()

    quantity = old_quantity * fraction
    side = pos["side"]
    entry = f(pos.get("entry_price"))

    # Simulate worse execution on exit.
    slip = SLIPPAGE_RATE + max(0.0, extra_slippage)
    exit_price = (
        price * (1 - slip)
        if side == "LONG"
        else price * (1 + slip)
    )

    gross = (
        (exit_price - entry) * quantity
        if side == "LONG"
        else (entry - exit_price) * quantity
    )

    exit_fee = abs(exit_price * quantity) * FEE_RATE
    entry_fee_allocated = f(pos.get("entry_fee")) * fraction
    funding_allocated = f(pos.get("funding_pnl")) * fraction

    net = (
        gross
        - entry_fee_allocated
        - exit_fee
        + funding_allocated
    )

    # Entry fee was already deducted when the position opened.
    # Funding cash effects were already applied when accrued.
    s["equity"] += gross - exit_fee
    s["realized_pnl"] += net

    qty_initial = max(f(pos.get("qty_initial"), old_quantity), 1e-12)
    closed_fraction_of_initial = quantity / qty_initial
    risk_slice = f(pos.get("risk_usd_initial")) * closed_fraction_of_initial
    r_multiple = (net / risk_slice) if risk_slice > 0 else 0.0
    entry_equity = f(pos.get("entry_equity")) or s["equity"]
    effective_risk_pct = (
        risk_slice / entry_equity * 100 if entry_equity > 0 else 0.0
    )

    pos["acc_net_pnl"] = f(pos.get("acc_net_pnl")) + net
    pos["acc_fees"] = f(pos.get("acc_fees")) + exit_fee
    pos["acc_funding"] = f(pos.get("acc_funding")) + funding_allocated
    pos["acc_r"] = f(pos.get("acc_r")) + r_multiple

    trade = {
        "side": side,
        "direction": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": quantity,
        "leverage": pos.get("leverage", LEVERAGE),
        "margin_used": f(pos.get("margin_used")) * fraction,
        "entry_time": pos.get("entry_time"),
        "exit_time": iso_of(ts_now),
        "exit_timestamp": ts_now,
        "gross_pnl": gross,
        "entry_fee": entry_fee_allocated,
        "exit_fee": exit_fee,
        "fees": entry_fee_allocated + exit_fee,
        "funding_pnl": funding_allocated,
        "net_pnl": net,
        "pnl": net,
        "reason": reason,
        "exit_reason": reason,
        "partial": fraction < 0.999999,
        "build": BUILD,
        "origin_build": pos.get("origin_build", BUILD),
        "setup": pos.get("setup", "UNKNOWN"),
        "regime": pos.get("regime", "UNKNOWN"),
        "score": pos.get("score"),
        "score_breakdown": pos.get("score_breakdown", {}),
        "initial_stop_price": f(pos.get("initial_stop_price")),
        "stop_price_at_exit": f(pos.get("stop_price")),
        "targets": pos.get("targets", {}),
        "planned_risk": risk_slice,
        "effective_risk_pct": effective_risk_pct,
        "risk_distance": f(pos.get("risk_distance")),
        "mfe_r": f(pos.get("mfe_r")),
        "mae_r": f(pos.get("mae_r")),
        "r_multiple": r_multiple,
        "zone_room_r": pos.get("zone_room_r"),
        "event_spanned": bool(pos.get("event_spanned")),
        "event_spanned_names": pos.get("event_spanned_names", []),
        "entry_event_within_24h": pos.get("entry_event_within_24h"),
        "entry_event_upcoming_24h": pos.get("entry_event_upcoming_24h"),
        "entry_event_name": pos.get("entry_event_name"),
        "tp1_taken": bool(pos.get("tp1_taken")),
        "tp2_taken": bool(pos.get("tp2_taken")),
        "tp_stage": max(
            2 if pos.get("tp2_taken")
            else 1 if pos.get("tp1_taken") else 0,
            2 if reason == "PARTIAL_TAKE_PROFIT_2"
            else 1 if reason == "PARTIAL_TAKE_PROFIT_1" else 0,
        ),
        "hold_extensions": i(pos.get("hold_extensions")),
        "trend_flip": bool(pos.get("trend_flip_exit"))
                      or reason == "TREND_FLIP",
        "runner_floor_active": bool(pos.get("runner_floor_active")),
        "source_trader": pos.get("source_trader"),
        "source_trader_label": pos.get("source_trader_label"),
        "source_entry_price": pos.get("source_entry_price"),
        "copy_source_event_ts": pos.get("copy_source_event_ts"),
        "copy_detection_lag_seconds": pos.get(
            "copy_detection_lag_seconds"
        ),
    }

    s["trades"].append(trade)
    s["trades"] = s["trades"][-TRADE_LIMIT:]

    event(
        s,
        "POSITION_PARTIALLY_CLOSED" if fraction < 0.999999
        else "POSITION_CLOSED",
        side=side,
        reason=reason,
        net_pnl=net,
        fraction=fraction,
        setup=trade["setup"],
        r_multiple=round(r_multiple, 4),
    )

    if fraction >= 0.999999:
        total_net = f(pos.get("acc_net_pnl"))
        total_r = f(pos.get("acc_r"))

        _update_stats(
            s["stats_by_setup"], pos.get("setup"), pos, total_net, total_r
        )
        _update_stats(
            s["stats_by_regime"], pos.get("regime"), pos, total_net, total_r
        )

        if total_net < 0:
            s["consecutive_losses"] = i(s.get("consecutive_losses")) + 1
            if pos.get("saw_stop_loss"):
                s["daily_stop_count"] = i(s.get("daily_stop_count")) + 1
            if s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
                s["consecutive_lock_until"] = (
                    ts_now + CONSECUTIVE_PAUSE_SECONDS
                )
                s["next_risk_multiplier"] = 0.5
                event(
                    s,
                    "CONSECUTIVE_LOSS_COOLDOWN",
                    losses=s["consecutive_losses"],
                    pause_seconds=CONSECUTIVE_PAUSE_SECONDS,
                )
        else:
            s["consecutive_losses"] = 0

        s["last_close_ts"] = ts_now
        s["position"] = None
    else:
        remaining_fraction = 1.0 - fraction

        pos["quantity"] = old_quantity * remaining_fraction
        pos["entry_fee"] = (
            f(pos.get("entry_fee")) * remaining_fraction
        )
        pos["funding_pnl"] = (
            f(pos.get("funding_pnl")) * remaining_fraction
        )
        pos["margin_used"] = (
            f(pos.get("margin_used")) * remaining_fraction
        )
        pos["notional"] = (
            f(pos.get("notional")) * remaining_fraction
        )
        pos["partial_taken"] = True

    s["peak_equity"] = max(s["peak_equity"], s["equity"])
    return True


def open_position(s, candidate, candle_ts, risk_multiplier=1.0,
                  now_s=None):
    ts_now = int(now_s) if now_s is not None else now_ts()
    side = candidate["side"]
    entry = candidate["entry_price"]
    stop = candidate["stop_price"]

    sizing = size_position(
        s["equity"], entry, stop, RISK_PER_TRADE * risk_multiplier
    )
    if not sizing:
        return False, "数量計算失敗"

    quantity = sizing["quantity"]
    notional = sizing["notional"]
    fee = notional * FEE_RATE

    if fee >= s["equity"]:
        return False, "手数料が資金を超過"
    if notional < 10.0:
        return False, "想定元本が最小注文未満"

    entry_equity = s["equity"]
    s["equity"] -= fee

    distance = candidate["risk_distance"]
    risk_usd = quantity * (distance + entry * ROUND_TRIP_COST_RATE)

    s["position"] = {
        "side": side,
        "entry_price": entry,
        "quantity": quantity,
        "leverage": LEVERAGE,
        "margin_used": sizing["margin_used"],
        "notional": notional,
        "planned_risk": risk_usd,
        "effective_risk_pct": risk_usd / entry_equity * 100,
        "stop_price": stop,
        "initial_stop_price": stop,
        "risk_distance": distance,
        "entry_fee": fee,
        "funding_pnl": 0.0,
        "entry_time": iso_of(ts_now),
        "opened_timestamp": ts_now,
        "last_funding_ts": ts_now,
        "last_checked_candle_ts": candle_ts,
        "entry_signal_score": candidate["score"],
        "score": candidate["score"],
        "score_breakdown": candidate["score_breakdown"],
        "setup": candidate["setup"],
        "regime": candidate["regime"],
        "origin_build": BUILD,
        "targets": candidate["targets"],
        "zone_room_r": candidate.get("zone_room_r"),
        "source_trader": candidate.get("source_trader"),
        "source_trader_label": candidate.get("source_trader_label"),
        "source_entry_price": candidate.get("source_entry_price"),
        "copy_source_event_ts": candidate.get("copy_source_event_ts"),
        "copy_detection_lag_seconds": candidate.get(
            "copy_detection_lag_seconds"
        ),
        "qty_initial": quantity,
        "risk_usd_initial": risk_usd,
        "entry_equity": entry_equity,
        "risk_multiplier": risk_multiplier,
        "tp1_taken": False,
        "tp2_taken": False,
        "hold_extensions": 0,
        "trend_flip_exit": False,
        "runner_floor_active": False,
        "partial_taken": False,
        "be_moved": False,
        "trail_active": False,
        "best_price": candidate["reference_price"],
        "worst_price": candidate["reference_price"],
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "acc_net_pnl": 0.0,
        "acc_fees": fee,
        "acc_funding": 0.0,
        "acc_r": 0.0,
        "saw_stop_loss": False,
    }

    s["position"].update(entry_event_fields(s, ts_now))

    if candidate.get("source_trader"):
        event(
            s,
            "COPY_ENTRY",
            trader=candidate["source_trader"],
            label=candidate.get("source_trader_label"),
            side=side,
            source_entry_price=candidate.get("source_entry_price"),
            entry_price=entry,
            source_event_ts=candidate.get("copy_source_event_ts"),
            detection_lag_seconds=candidate.get(
                "copy_detection_lag_seconds"
            ),
        )

    s["last_entry_candle"] = candle_ts
    # The post-cooldown half-risk flag is consumed by this entry.
    s["next_risk_multiplier"] = 1.0

    event(
        s,
        "POSITION_OPENED",
        side=side,
        setup=candidate["setup"],
        regime=candidate["regime"],
        score=candidate["score"],
        entry_price=entry,
        stop_price=stop,
        quantity=quantity,
        leverage=LEVERAGE,
        effective_risk_pct=s["position"]["effective_risk_pct"],
    )

    return True, side + " opened (" + candidate["setup"] + ")"


# ============================================================
# RISK STATE
# ============================================================

def current_drawdown_pct(s, marked_equity):
    peak = max(f(s.get("peak_equity")), marked_equity, 0.01)
    return max(0.0, (peak - marked_equity) / peak * 100.0)


def refresh_risk_state(s, marked_equity, now_s=None):
    """Daily reset, daily loss lock, DD pause. Returns current DD pct."""
    ts_now = int(now_s) if now_s is not None else now_ts()

    if s.get("daily_date") != date_of(ts_now):
        s["daily_date"] = date_of(ts_now)
        s["daily_start_equity"] = marked_equity
        s["daily_loss_lock"] = False
        s["daily_stop_count"] = 0
        event(s, "DAILY_RESET")

    daily_start = max(f(s.get("daily_start_equity")), 0.01)
    daily_loss_pct = max(
        0.0, (daily_start - marked_equity) / daily_start * 100.0
    )

    if daily_loss_pct >= DAILY_LOSS_LIMIT * 100:
        if not s["daily_loss_lock"]:
            event(s, "DAILY_LOSS_LOCK", daily_loss_pct=daily_loss_pct)
        s["daily_loss_lock"] = True

    if i(s.get("daily_stop_count")) >= DAILY_STOP_LIMIT:
        if not s["daily_loss_lock"]:
            event(s, "DAILY_STOP_LOCK", stops=s["daily_stop_count"])
        s["daily_loss_lock"] = True

    s["peak_equity"] = max(f(s.get("peak_equity")), marked_equity)
    dd = current_drawdown_pct(s, marked_equity)
    s["max_drawdown_pct"] = max(f(s.get("max_drawdown_pct")), dd)

    if dd >= DD_HARD_PAUSE and s["drawdown_pause_until"] < ts_now:
        s["drawdown_pause_until"] = ts_now + DRAWDOWN_PAUSE_SECONDS
        event(s, "DRAWDOWN_PAUSE_STARTED", drawdown_pct=dd)

    if (
        s["drawdown_pause_until"]
        and s["drawdown_pause_until"] <= ts_now
    ):
        s["drawdown_pause_until"] = 0
        event(s, "DRAWDOWN_PAUSE_ENDED")

    return dd


def risk_controls(s, dd_pct):
    """Return (risk_multiplier, allowed_setups) for the next entry."""
    mult = 1.0
    allowed = ("A", "B", "C")
    if dd_pct >= DD_THROTTLE_2:
        mult = 0.25
        allowed = ("A",)
    elif dd_pct >= DD_THROTTLE_1:
        mult = 0.5
    mult *= clamp(f(s.get("next_risk_multiplier"), 1.0), 0.1, 1.0)
    return mult, allowed


# ============================================================
# POSITION MANAGEMENT (V20 exit framework)
# ============================================================

def _stop_reason(pos):
    if pos.get("trail_active"):
        return "TRAIL_STOP"
    if pos.get("be_moved"):
        return "BREAKEVEN_STOP"
    return "STOP_LOSS"


def manage_position(s, candles, price, ctx, funding, now_s=None):
    pos = s.get("position")
    if not pos:
        return

    ts_now = int(now_s) if now_s is not None else now_ts()

    apply_funding(s, funding, ts_now)

    pos = s.get("position")
    if not pos:
        return

    side = pos["side"]
    entry = f(pos.get("entry_price"))
    distance = max(f(pos.get("risk_distance")), 0.01)
    targets = pos.get("targets") or {}
    tp1 = f(targets.get("tp1_price"))
    tp2 = f(targets.get("tp2_price"))
    be_price = f(targets.get("be_price")) or (
        entry + (1.0 if side == "LONG" else -1.0)
        * entry * ROUND_TRIP_COST_RATE
    )
    av = max(f(ctx.get("atr")) if ctx else 0.0, 0.01)

    cursor = i(pos.get("last_checked_candle_ts"))

    for candle in candles:
        if candle["t"] <= cursor:
            continue

        high = candle["h"]
        low = candle["l"]
        close = candle["c"]
        candle_ts_s = candle["t"] // 1000

        # --- MFE / MAE tracking (in R) ---
        if side == "LONG":
            favorable = high - entry
            adverse = entry - low
            pos["best_price"] = max(f(pos.get("best_price"), entry), high)
            pos["worst_price"] = min(f(pos.get("worst_price"), entry), low)
        else:
            favorable = entry - low
            adverse = high - entry
            pos["best_price"] = min(f(pos.get("best_price"), entry), low)
            pos["worst_price"] = max(f(pos.get("worst_price"), entry), high)
        pos["mfe_r"] = max(f(pos.get("mfe_r")), favorable / distance)
        pos["mae_r"] = max(f(pos.get("mae_r")), adverse / distance)

        # --- Stop check first (conservative). Stop always re-read from
        # the position so trail/BE updates from earlier candles in this
        # same catch-up pass apply to later candles (V19 stale-stop bug).
        stop = f(pos.get("stop_price"))
        stop_hit = (low <= stop) if side == "LONG" else (high >= stop)
        if stop_hit:
            # Realistic fill: worse of the stop and the candle close
            # (gap-through), then extra adverse slippage in close_position.
            if side == "LONG":
                fill_ref = min(stop, close)
            else:
                fill_ref = max(stop, close)
            reason = _stop_reason(pos)
            if reason == "STOP_LOSS":
                pos["saw_stop_loss"] = True
            close_position(
                s, fill_ref, reason,
                extra_slippage=STOP_EXTRA_SLIPPAGE,
                now_s=candle_ts_s,
            )
            return

        # --- Breakeven at +1R ---
        if not pos.get("be_moved"):
            be_trigger = (
                entry + distance * BREAKEVEN_AT_R if side == "LONG"
                else entry - distance * BREAKEVEN_AT_R
            )
            reached = (
                high >= be_trigger if side == "LONG"
                else low <= be_trigger
            )
            if reached:
                if side == "LONG":
                    pos["stop_price"] = max(f(pos.get("stop_price")), be_price)
                else:
                    pos["stop_price"] = min(f(pos.get("stop_price")), be_price)
                pos["be_moved"] = True
                event(s, "STOP_MOVED_TO_BREAKEVEN", stop_price=pos["stop_price"])

        # --- Partial take-profit 1: +1.5R, 20% of initial quantity ---
        if not pos.get("tp1_taken") and tp1 > 0:
            hit = high >= tp1 if side == "LONG" else low <= tp1
            if hit:
                qty_now = f(pos.get("quantity"))
                qty_initial = max(f(pos.get("qty_initial"), qty_now), 1e-12)
                frac = clamp(
                    TP1_FRACTION * qty_initial / max(qty_now, 1e-12),
                    0.0, 1.0,
                )
                if frac > 0 and close_position(
                    s, tp1, "PARTIAL_TAKE_PROFIT_1", frac,
                    now_s=candle_ts_s,
                ):
                    pos = s.get("position")
                    if not pos:
                        return
                    pos["tp1_taken"] = True
                    pos["partial_taken"] = True

        # --- Partial take-profit 2: +3R, 20% of initial quantity ---
        pos = s.get("position")
        if not pos:
            return
        if pos.get("tp1_taken") and not pos.get("tp2_taken") and tp2 > 0:
            hit = high >= tp2 if side == "LONG" else low <= tp2
            if hit:
                qty_now = f(pos.get("quantity"))
                qty_initial = max(f(pos.get("qty_initial"), qty_now), 1e-12)
                frac = clamp(
                    TP2_FRACTION * qty_initial / max(qty_now, 1e-12),
                    0.0, 1.0,
                )
                if frac > 0 and close_position(
                    s, tp2, "PARTIAL_TAKE_PROFIT_2", frac,
                    now_s=candle_ts_s,
                ):
                    pos = s.get("position")
                    if not pos:
                        return
                    pos["tp2_taken"] = True

        # --- Chandelier trail on the runner (after TP1) ---
        pos = s.get("position")
        if not pos:
            return
        if pos.get("tp1_taken"):
            if side == "LONG":
                candidate_stop = f(pos.get("best_price")) - CHANDELIER_ATR * av
                new_stop = max(f(pos.get("stop_price")), candidate_stop)
                # Profit floor: once the trade has shown +2R, the stop
                # may not fall below entry + 1R (never return it all).
                if f(pos.get("mfe_r")) >= RUNNER_FLOOR_ARM_R:
                    floor_stop = entry + distance * RUNNER_FLOOR_R
                    if floor_stop > new_stop:
                        new_stop = floor_stop
                        pos["runner_floor_active"] = True
            else:
                candidate_stop = f(pos.get("best_price")) + CHANDELIER_ATR * av
                new_stop = min(f(pos.get("stop_price")), candidate_stop)
                if f(pos.get("mfe_r")) >= RUNNER_FLOOR_ARM_R:
                    floor_stop = entry - distance * RUNNER_FLOOR_R
                    if floor_stop < new_stop:
                        new_stop = floor_stop
                        pos["runner_floor_active"] = True
            if new_stop != f(pos.get("stop_price")):
                pos["stop_price"] = new_stop
                pos["trail_active"] = True

        # --- Time stop: no follow-through within the window ---
        elapsed = candle_ts_s - i(pos.get("opened_timestamp"))
        if (
            elapsed >= TIME_STOP_SECONDS
            and f(pos.get("mfe_r")) < TIME_STOP_MIN_MFE_R
        ):
            close_position(s, close, "TIME_STOP", now_s=candle_ts_s)
            return

        pos["last_checked_candle_ts"] = candle["t"]
        cursor = candle["t"]

    # --- Trend flip: HTF bias reversed against the runner ---
    # Applies only once the position is in its runner phase (TP1 taken):
    # the trade thesis was trend continuation, so a higher-timeframe
    # reversal closes the runner immediately instead of waiting for the
    # (now wider) trail to be hit.
    pos = s.get("position")
    if pos and pos.get("tp1_taken"):
        htf_bias = (ctx or {}).get("htf_bias")
        against = "DOWN" if side == "LONG" else "UP"
        if htf_bias == against:
            pos["trend_flip_exit"] = True
            event(
                s, "TREND_FLIP_EXIT", side=side, htf_bias=htf_bias,
                hold_extensions=i(pos.get("hold_extensions")),
            )
            close_position(s, price, "TREND_FLIP", now_s=ts_now)
            return

    # --- Max hold: 24h blocks, extendable while the trend cooperates ---
    pos = s.get("position")
    if not pos:
        return
    extensions = i(pos.get("hold_extensions"))
    deadline = i(pos.get("opened_timestamp")) + MAX_HOLD_SECONDS * (
        1 + extensions
    )
    if ts_now >= deadline:
        unrealized_r = (
            (price - entry) if side == "LONG" else (entry - price)
        ) / distance
        htf_bias = (ctx or {}).get("htf_bias")
        aligned = htf_bias == ("UP" if side == "LONG" else "DOWN")
        if (
            extensions < MAX_HOLD_EXTENSIONS
            and unrealized_r >= EXTENSION_MIN_R
            and aligned
        ):
            pos["hold_extensions"] = extensions + 1
            event(
                s, "MAX_HOLD_EXTENDED",
                extensions=pos["hold_extensions"],
                unrealized_r=round(unrealized_r, 3),
                htf_bias=htf_bias,
            )
        else:
            close_position(s, price, "MAX_HOLD_TIME", now_s=ts_now)


# ============================================================
# SIGNAL (dashboard-shaped) + DASHBOARD
# ============================================================

def make_signal(ctx, candidate, side_scores, status, notes):
    direction = candidate["side"] if candidate else "WAIT"
    score = candidate["score"] if candidate else max(
        side_scores.get("LONG", 0), side_scores.get("SHORT", 0)
    )
    reasons = []
    flags = []
    if candidate:
        bd = candidate["score_breakdown"]
        for key, value in bd.items():
            if value:
                reasons.append(f"{key}:{value:+d}")
        reasons.append(
            f"setup {candidate['setup']} / regime {candidate['regime']}"
        )
        flags.append("コストフィルター通過")
        if candidate.get("zone_room_r") is not None:
            flags.append(
                f"対向ゾーンまで {candidate['zone_room_r']:.2f}R"
            )
    structure = ctx["structure"]
    return {
        "direction": direction,
        "score": score,
        "long_score": side_scores.get("LONG", 0),
        "short_score": side_scores.get("SHORT", 0),
        "score_edge": abs(
            side_scores.get("LONG", 0) - side_scores.get("SHORT", 0)
        ),
        "rsi": round(ctx["rsi"], 3),
        "atr": round(ctx["atr"], 5),
        "ema9": ctx["ema9"],
        "ema21": ctx["ema21"],
        "trend_15m": ctx["trend_15m"],
        "trend_1h": ctx["trend_1h"],
        "trend_4h": ctx["trend_4h"],
        "timestamp": iso_now(),
        "reference_price": ctx["price"],
        "previous_close": ctx["previous_close"],
        "reasons": reasons or ["明確な方向優位性なし"],
        "flags": flags,
        "bollinger": ctx["bollinger"],
        "fibonacci": ctx["fibonacci"],
        # V20 additions:
        "regime": ctx["regime"],
        "adx": round(ctx["adx"], 2),
        "atr_percentile": round(ctx["atr_percentile"], 1),
        "macd": {
            key: round(value, 4)
            for key, value in ctx["macd"].items()
        },
        "vwap": ctx["vwap"],
        "relative_volume": round(ctx["relative_volume"], 3),
        "htf_bias": ctx["htf_bias"],
        "structure_bias": structure["structure"],
        "support": structure["support"],
        "resistance": structure["resistance"],
        "premium": ctx["premium"],
        "setup": candidate["setup"] if candidate else None,
        "score_breakdown": (
            candidate["score_breakdown"] if candidate else {}
        ),
        "entry_status": status,
        "evaluation_notes": notes[-12:],
    }


def make_dashboard(s, price, funding, sig, status):
    pos = s.get("position")
    upnl_gross = unrealized(pos, price) if pos else 0.0
    upnl = unrealized_net(pos, price) if pos else 0.0
    marked_equity = s["equity"] + upnl

    initial = max(f(s["initial_equity"]), 0.01)

    s["peak_equity"] = max(
        f(s.get("peak_equity"), initial),
        marked_equity,
    )

    current_dd = current_drawdown_pct(s, marked_equity)

    s["max_drawdown_pct"] = max(
        f(s.get("max_drawdown_pct")),
        current_dd,
    )

    daily_start = max(f(s.get("daily_start_equity")), 0.01)
    daily_loss = max(
        0.0,
        (daily_start - marked_equity) / daily_start * 100.0,
    )

    trades = s["trades"]

    # Includes partial exits as individual realized trade records.
    pnls = [
        f(t.get("net_pnl", t.get("pnl")))
        for t in trades
    ]

    wins = sum(x > 0 for x in pnls)
    losses = sum(x < 0 for x in pnls)
    total_wins = sum(x for x in pnls if x > 0)
    total_losses = abs(sum(x for x in pnls if x < 0))

    history = s["equity_history"]
    history.append({
        "timestamp": iso_now(),
        "equity": s["equity"],
        "marked_equity": marked_equity,
        "price": price,
    })
    s["equity_history"] = history[-HISTORY_LIMIT:]

    position = dict(pos) if pos else None
    if position:
        position["unrealized_pnl"] = upnl
        position["unrealized_pnl_gross"] = upnl_gross

    metrics = {
        "closed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0.0,
        "total_net_pnl": s["realized_pnl"],
        "average_trade": sum(pnls) / len(pnls) if pnls else 0.0,
        "average_win": total_wins / wins if wins else 0.0,
        "average_loss": total_losses / losses if losses else 0.0,
        "profit_factor": (
            total_wins / total_losses
            if total_losses > 0
            else (999.0 if total_wins > 0 else 0.0)
        ),
        "expectancy_per_trade": (
            sum(pnls) / len(pnls) if pnls else 0.0
        ),
    }

    return {
        "build": BUILD,
        "generated_at": iso_now(),
        "market_status": "OK",
        "price": price,
        "funding_rate": funding,
        "funding_rate_pct": funding * 100.0,
        "initial_equity": initial,
        "equity": s["equity"],
        "marked_equity": marked_equity,
        "realized_pnl": s["realized_pnl"],
        "unrealized_pnl": upnl,
        "unrealized_pnl_gross": upnl_gross,
        "net_pnl": marked_equity - initial,
        "net_pnl_pct": (marked_equity - initial) / initial * 100.0,
        "position": position,
        "open_position": position,
        "has_position": bool(position),
        "signal": {
            **sig,
            "entry_status": status,
            "paper_only": True,
            "live_trading_enabled": False,
        },
        "risk": {
            "daily_loss_pct": daily_loss,
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT * 100,
            "daily_loss_lock": s["daily_loss_lock"],
            "daily_stop_count": i(s.get("daily_stop_count")),
            "drawdown_pct": current_dd,
            "current_drawdown_pct": current_dd,
            "max_drawdown_pct": s["max_drawdown_pct"],
            "drawdown_limit_pct": DD_HARD_PAUSE,
            "drawdown_pause_until": s["drawdown_pause_until"],
            "drawdown_lock": s["drawdown_pause_until"] > now_ts(),
            "consecutive_losses": s["consecutive_losses"],
            "consecutive_lock_until": s["consecutive_lock_until"],
            "consecutive_loss_lock": (
                s["consecutive_lock_until"] > now_ts()
            ),
            "leverage": LEVERAGE,
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100,
            "paper_only": True,
            "live_trading_enabled": False,
        },
        "metrics": metrics,
        "closed_trades": len(trades),
        "win_rate": metrics["win_rate"],
        "trades": trades[-200:],
        "paper_trades": trades[-200:],
        "recent_trades": trades[-200:],
        "equity_history": s["equity_history"][-1000:],
        "events": s["events"][-100:],
        "stats_by_setup": s.get("stats_by_setup", {}),
        "stats_by_regime": s.get("stats_by_regime", {}),
        "timeframes": {
            "15m": {"direction": sig["trend_15m"]},
            "1h": {"direction": sig["trend_1h"]},
            "4h": {"direction": sig["trend_4h"]},
        },
        "indicators": {
            "rsi_15m": sig["rsi"],
            "atr": sig["atr"],
            "bollinger": sig["bollinger"],
            "adx": sig.get("adx"),
            "atr_percentile": sig.get("atr_percentile"),
            "macd": sig.get("macd"),
            "vwap": sig.get("vwap"),
            "relative_volume": sig.get("relative_volume"),
        },
        "fibonacci": sig["fibonacci"],
        "config": {
            "leverage": LEVERAGE,
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "paper_only": True,
            "live_trading_enabled": False,
            "min_signal_score": MIN_SCORE,
            "min_score_edge": 0,
            "partial_tp_fraction": TP1_FRACTION,
            "tp2_fraction": TP2_FRACTION,
            "trail_atr": CHANDELIER_ATR,
            "runner_floor_r": RUNNER_FLOOR_R,
            "max_hold_extensions": MAX_HOLD_EXTENSIONS,
            "stop_atr_min": STOP_ATR_MIN,
            "stop_atr_max": STOP_ATR_MAX,
            "tp1_at_r": TP1_AT_R,
            "tp2_at_r": TP2_AT_R,
            "copy_traders": len(_copy_config_traders()),
            "copy_follow_source_exit": COPY_FOLLOW_SOURCE_EXIT,
        },
        "last_error": s.get("last_error"),
        "next_high_event": _next_high_event_payload(s),
        "event_risk": _event_risk_payload(s),
        "copy_trading": _copy_dashboard_payload(s),
    }


# ============================================================
# MAIN
# ============================================================

def main():
    s = load_state()
    s["last_error"] = None

    try:
        market = fetch_market()
        price = market["price"]
        funding = market["funding"]
        premium = market["premium"]

        c15 = fetch_candles("15m", CANDLES_15M)
        c1 = fetch_candles("1h", CANDLES_1H)
        c4 = fetch_candles("4h", CANDLES_4H)

        ctx = build_context(c15, c1, c4, funding, premium)
        latest_candle = c15[-1]["t"]
        now = now_ts()

        # Economic calendar: refresh (daily, fail-open) and evaluate
        # the event-risk gate for this cycle.
        refresh_econ_calendar(s, now)
        ev_status = event_window_status(s, now)

        # Copy trading (Setup D): refresh source snapshots (entry
        # signals + source-exit checks below) and run the daily
        # re-evaluation. All fetches fail open.
        copy_signals = update_copy_tracking(s, now)
        reevaluate_copy_traders(s, now)

        # Mark-to-market (net of exit costs) for risk state.
        pos = s.get("position")
        marked = s["equity"] + (
            unrealized_net(pos, price) if pos else 0.0
        )
        dd = refresh_risk_state(s, marked, now)

        # Copy positions follow their source: if the tracked trader
        # has closed or flipped away, exit now (house exits remain as
        # the backstop). Missing data never forces an exit.
        if COPY_FOLLOW_SOURCE_EXIT:
            _pos = s.get("position")
            if _pos and _pos.get("setup") == "D" \
                    and _pos.get("source_trader"):
                _src_side = copy_source_side(s, _pos)
                if _src_side != "UNKNOWN" \
                        and _src_side != _pos.get("side"):
                    event(
                        s, "COPY_SOURCE_EXIT",
                        trader=_pos.get("source_trader"),
                        source_side=_src_side,
                        position_side=_pos.get("side"),
                    )
                    close_position(
                        s, price, "COPY_SOURCE_EXIT", now_s=now
                    )

        had_position = bool(s.get("position"))

        if had_position:
            update_position_event_span(s, now)
            manage_position(s, c15, price, ctx, funding, now)

        if had_position and not s.get("position"):
            s["last_entry_candle"] = latest_candle

        # Re-mark after management for entry decisions.
        pos = s.get("position")
        marked = s["equity"] + (
            unrealized_net(pos, price) if pos else 0.0
        )
        dd = current_drawdown_pct(s, marked)

        risk_mult, allowed_setups = risk_controls(s, dd)
        candidate, side_scores, notes = evaluate_entry(
            ctx, allowed_setups
        )
        for _cs in copy_signals:
            notes.append(
                f"COPY {_cs['kind']}: {_cs['label']} {_cs['side']}"
                f"（ラグ "
                f"{int(_cs['age_seconds']) if _cs['age_seconds'] is not None else '不明'}"
                f"秒、{'新鮮' if _cs['fresh'] else '鮮度切れ'}）"
            )

        status = "WAIT: シグナル条件未達"

        if s.get("position"):
            status = "保有中"
        elif s["daily_loss_lock"]:
            status = "日次停止中（損失/ストップ回数上限、UTC日付更新で解除）"
        elif s["drawdown_pause_until"] > now:
            remaining = (s["drawdown_pause_until"] - now) // 60
            status = f"DD休止中（残り{remaining}分）"
        elif s["consecutive_lock_until"] > now:
            remaining = (s["consecutive_lock_until"] - now) // 60
            status = f"連敗クールダウン中（残り{remaining}分）"
        elif now - s["last_close_ts"] < COOLDOWN_SECONDS:
            status = "クールダウン中"
        elif latest_candle == s["last_entry_candle"]:
            status = "同じ15分足での再エントリー防止"
        elif candidate is not None and ev_status["blocked"]:
            ev = ev_status["active_event"]
            status = (
                "イベント回避（EVENT_RISK）: "
                f"{ev.get('title')} の発表窓のため新規エントリー見送り"
            )
            notes.append(
                f"EVENT_RISK: {ev.get('title')}"
                f"（{ev.get('category')}）"
            )
            _cal = s["econ_calendar"]
            _key = f"{ev.get('title')}|{ev.get('ts')}"
            if _cal.get("last_block_key") != _key or \
                    now - i(_cal.get("last_block_log_ts")) \
                    >= ECON_BLOCK_LOG_INTERVAL_SECONDS:
                _cal["last_block_key"] = _key
                _cal["last_block_log_ts"] = now
                event(
                    s, "ENTRY_SKIPPED_EVENT_RISK",
                    event_title=ev.get("title"),
                    category=ev.get("category"),
                    event_ts=i(ev.get("ts")),
                    reason="EVENT_RISK",
                )
        elif candidate is not None:
            mult = risk_mult
            if candidate["setup"] == "C":
                mult *= SETUP_C_RISK_MULT
            ok, message = open_position(
                s, candidate, latest_candle,
                risk_multiplier=mult, now_s=now,
            )
            status = message if ok else "エントリー見送り: " + message
        elif copy_signals and ev_status["blocked"]:
            status = (
                "イベント回避（EVENT_RISK）: "
                "コピー信号も新規エントリー見送り"
            )
        elif copy_signals and dd >= DD_THROTTLE_2:
            status = "コピー見送り: DDスロットル（20%以上ではAのみ許可）"
        elif copy_signals:
            fresh = next(
                (x for x in copy_signals if x["fresh"]), None
            )
            if fresh is None:
                status = "コピー見送り: 信号の鮮度切れ（30分超）"
            else:
                cand_d, reason_d = _candidate_copy(
                    ctx, fresh["side"], fresh
                )
                if cand_d is None:
                    status = "コピー見送り: " + str(reason_d)
                else:
                    ok, message = open_position(
                        s, cand_d, latest_candle,
                        risk_multiplier=risk_mult, now_s=now,
                    )
                    status = (
                        f"コピー参入: {fresh['label']} の"
                        f"{fresh['side']}に追随（セットアップD）"
                        if ok else "エントリー見送り: " + message
                    )
        else:
            status = (
                f"WAIT（LONG {side_scores.get('LONG', 0)} / "
                f"SHORT {side_scores.get('SHORT', 0)}、"
                f"必要スコア {MIN_SCORE}、レジーム {ctx['regime']}）"
            )

        sig = make_signal(ctx, candidate, side_scores, status, notes)
        sig["entry_status"] = status

        dashboard = make_dashboard(
            s, price, funding, sig, status
        )

        write_json(STATE_FILE, s)
        write_json(DATA_FILE, dashboard)

        print(
            f"BUILD: {BUILD}\n"
            f"PRICE: {price:.2f}\n"
            f"REGIME: {ctx['regime']} "
            f"ADX={ctx['adx']:.1f} "
            f"ATR_PCT={ctx['atr_percentile']:.0f}\n"
            f"SIGNAL: {sig['direction']} "
            f"score={sig['score']} "
            f"long={sig['long_score']} "
            f"short={sig['short_score']}\n"
            f"ENTRY: {status}\n"
            f"EVENT_RISK: {('BLOCKED: ' + str(ev_status['active_event'].get('title'))) if ev_status['blocked'] else 'clear'}\n"
            f"COPY: {len(copy_signals)} signal(s), "
            f"{sum(1 for x in copy_signals if x['fresh'])} fresh\n"
            f"EQUITY: {s['equity']:.4f}\n"
            f"REALIZED PNL: {s['realized_pnl']:.4f}\n"
            f"CLOSED RECORDS: {len(s['trades'])}\n"
            f"LEVERAGE: {LEVERAGE}x\n"
            f"EFFECTIVE RISK: {RISK_PER_TRADE * 100:.1f}%\n"
            f"PAPER ONLY: True\n"
            f"LIVE TRADING ENABLED: False"
        )

    except Exception as exc:
        s["last_error"] = f"{type(exc).__name__}: {exc}"

        old = read_json(DATA_FILE, {})
        if isinstance(old, dict) and old:
            old.update({
                "build": BUILD,
                "generated_at": iso_now(),
                "market_status": "ERROR",
                "last_error": s["last_error"],
            })
            write_json(DATA_FILE, old)

        write_json(STATE_FILE, s)
        print("ERROR:", s["last_error"])
        raise


if __name__ == "__main__":
    main()
