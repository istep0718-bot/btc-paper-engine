#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC PAPER ENGINE V12
Paper trading only.
No live orders, no wallet, no private keys.

Features:
- Hyperliquid public market data
- 15m / 1h / 4h multi-timeframe analysis
- EMA, RSI, ATR, ADX, MACD
- Risk-based position sizing
- Maximum margin and leverage limits
- Stop loss, take profit, trailing stop
- Maximum holding time
- Daily loss lock
- Maximum drawdown lock
- Fee and funding estimates
- Legacy state and trade history preservation
- Atomic JSON writes
- Dashboard output at docs/data.json
"""

import copy
import json
import math
import os
import time
import urllib.error
import urllib.request

from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

BUILD = "BTC_PAPER_V12_2026-10-23"

API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0

RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10

MAX_DAILY_LOSS_PCT = 0.03
MAX_DRAWDOWN_PCT = 0.10
MAX_CONSECUTIVE_LOSSES = 4

COOLDOWN_SECONDS = 60 * 60
MAX_HOLD_SECONDS = 24 * 60 * 60

TAKER_FEE = 0.0005
SLIPPAGE = 0.0002

CANDLE_INTERVAL = "15m"
CANDLE_MS = 15 * 60 * 1000

MIN_CANDLES = 110
MAX_STORED_TRADES = 2000
MAX_EQUITY_HISTORY = 5000
MAX_EVENTS = 300

MIN_SIGNAL_SCORE = 6
MIN_RISK_REWARD = 1.5

ATR_STOP_MULTIPLIER = 1.8
ATR_TARGET_MULTIPLIER = 3.0
TRAIL_START_R = 1.5
TRAIL_ATR_MULTIPLIER = 1.5

# A runner position has no fixed take-profit, but must still
# have a protective stop and a maximum holding time.
RUNNER_MODE = False

HTTP_TIMEOUT = 20
MAX_API_RETRIES = 3

# Existing state is never deleted simply because its version differs.
# If a state file cannot be parsed, the program stops instead of
# silently replacing it with a fresh account.


# ============================================================
# TIME / JSON HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def timestamp_ms():
    return int(time.time() * 1000)


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def clamp(value, low, high):
    return max(low, min(high, value))


def read_json(path):
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise RuntimeError(
            f"Could not read {path.name}; refusing to reset state: {exc}"
        ) from exc


def atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())

    os.replace(temp_path, path)


def api_post(payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V12",
        },
        method="POST",
    )

    last_error = None

    for attempt in range(MAX_API_RETRIES):
        try:
            with urllib.request.urlopen(
                request, timeout=HTTP_TIMEOUT
            ) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw)

        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt < MAX_API_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Hyperliquid API failed: {last_error}")


# ============================================================
# HYPERLIQUID MARKET DATA
# ============================================================

def fetch_candles(interval, count=250):
    now = timestamp_ms()

    interval_ms = {
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
    }.get(interval)

    if interval_ms is None:
        raise ValueError(f"Unsupported interval: {interval}")

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": now - interval_ms * (count + 30),
            "endTime": now,
        },
    }

    raw = api_post(payload)

    if not isinstance(raw, list):
        raise RuntimeError(f"Invalid candle response for {interval}")

    candles = []

    for item in raw:
        try:
            candle = {
                "t": int(item["t"]),
                "T": int(item["T"]),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "v": float(item["v"]),
                "n": int(item.get("n", 0)),
            }

            values = [
                candle["o"], candle["h"], candle["l"],
                candle["c"], candle["v"],
            ]

            if not all(math.isfinite(x) for x in values):
                continue

            if min(
                candle["o"], candle["h"], candle["l"], candle["c"]
            ) <= 0:
                continue

            if candle["h"] < candle["l"]:
                continue

            candles.append(candle)

        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    # Deduplicate and use only fully closed candles.
    unique = {c["t"]: c for c in candles if c["T"] < now}
    candles = sorted(unique.values(), key=lambda x: x["t"])

    if len(candles) < MIN_CANDLES:
        raise RuntimeError(
            f"Not enough closed {interval} candles: {len(candles)}"
        )

    return candles[-count:]


def fetch_market_price():
    result = api_post({"type": "allMids"})

    if not isinstance(result, dict) or COIN not in result:
        raise RuntimeError("BTC price missing from allMids response")

    price = safe_float(result[COIN], 0.0)

    if price <= 0:
        raise RuntimeError("Invalid BTC market price")

    return price


def fetch_funding_rate():
    result = api_post({"type": "metaAndAssetCtxs"})

    if not isinstance(result, list) or len(result) < 2:
        raise RuntimeError("Invalid funding response")

    meta = result[0]
    contexts = result[1]

    universe = meta.get("universe", [])

    for index, asset in enumerate(universe):
        if asset.get("name") != COIN:
            continue

        if index >= len(contexts):
            break

        rate = safe_float(contexts[index].get("funding"), float("nan"))

        if not math.isfinite(rate):
            raise RuntimeError("Invalid funding rate")

        return rate

    raise RuntimeError("BTC funding rate not found")


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = value * multiplier + result * (1.0 - multiplier)

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    recent = changes[-period:]
    gains = [max(change, 0.0) for change in recent]
    losses = [max(-change, 0.0) for change in recent]

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0

    rs = average_gain / average_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["c"]

        tr = max(
            current["h"] - current["l"],
            abs(current["h"] - previous_close),
            abs(current["l"] - previous_close),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    value = sum(true_ranges[:period]) / period

    for current_tr in true_ranges[period:]:
        value = (
            value * (period - 1) + current_tr
        ) / period

    return value


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return None

    trs = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        up_move = current["h"] - previous["h"]
        down_move = previous["l"] - current["l"]

        plus_dm.append(
            up_move if up_move > down_move and up_move > 0 else 0.0
        )
        minus_dm.append(
            down_move if down_move > up_move and down_move > 0 else 0.0
        )

        trs.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous["c"]),
            abs(current["l"] - previous["c"]),
        ))

    def smooth(values):
        initial = sum(values[:period])
        output = [initial]

        for value in values[period:]:
            output.append(
                output[-1] - output[-1] / period + value
            )

        return output

    smoothed_tr = smooth(trs)
    smoothed_plus = smooth(plus_dm)
    smoothed_minus = smooth(minus_dm)

    dx_values = []

    for i in range(len(smoothed_tr)):
        tr_value = smoothed_tr[i]

        if tr_value <= 0:
            dx_values.append(0.0)
            continue

        pdi = 100.0 * smoothed_plus[i] / tr_value
        mdi = 100.0 * smoothed_minus[i] / tr_value
        denominator = pdi + mdi

        if denominator <= 0:
            dx_values.append(0.0)
        else:
            dx_values.append(
                100.0 * abs(pdi - mdi) / denominator
            )

    if len(dx_values) < period:
        return None

    value = sum(dx_values[:period]) / period

    for current_dx in dx_values[period:]:
        value = (value * (period - 1) + current_dx) / period

    return value


def macd_values(values):
    if len(values) < 35:
        return None, None

    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return None, None

    # Build the MACD series for the signal line.
    macd_series = []

    for end in range(26, len(values) + 1):
        subset = values[:end]
        fast_value = ema(subset, 12)
        slow_value = ema(subset, 26)

        if fast_value is not None and slow_value is not None:
            macd_series.append(fast_value - slow_value)

    if len(macd_series) < 9:
        return fast - slow, None

    signal = ema(macd_series, 9)

    return fast - slow, signal


def analyze_timeframe(candles):
    closes = [c["c"] for c in candles]

    fast_ema = ema(closes, 20)
    slow_ema = ema(closes, 50)
    current_rsi = rsi(closes, 14)
    current_atr = atr(candles, 14)
    current_adx = adx(candles, 14)
    macd, macd_signal = macd_values(closes)

    if any(x is None for x in [
        fast_ema, slow_ema, current_rsi,
        current_atr, current_adx, macd,
    ]):
        raise RuntimeError("Indicator calculation incomplete")

    close = closes[-1]

    if close > fast_ema > slow_ema:
        trend = "UP"
    elif close < fast_ema < slow_ema:
        trend = "DOWN"
    else:
        trend = "SIDEWAYS"

    return {
        "close": close,
        "ema20": fast_ema,
        "ema50": slow_ema,
        "rsi": current_rsi,
        "atr": current_atr,
        "adx": current_adx,
        "macd": macd,
        "macd_signal": macd_signal,
        "trend": trend,
        "last_candle_ts": candles[-1]["t"],
    }


# ============================================================
# STATE MANAGEMENT
# ============================================================

def new_state():
    return {
        "build": BUILD,
        "initial_equity": INITIAL_EQUITY,
        "equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
        "position": None,
        "trades": [],
        "equity_history": [],
        "events": [],
        "funding_total": 0.0,
        "last_checked_candle_ts": None,
        "last_entry_ts": 0,
        "consecutive_losses": 0,
        "daily_date": utc_now().date().isoformat(),
        "daily_start_equity": INITIAL_EQUITY,
        "daily_loss_locked": False,
        "drawdown_locked": False,
        "api_error_count": 0,
    }


def add_event(state, event_type, message, **extra):
    event = {
        "timestamp": iso_now(),
        "type": event_type,
        "message": message,
    }
    event.update(extra)

    state.setdefault("events", []).append(event)
    state["events"] = state["events"][-MAX_EVENTS:]


def load_state():
    saved = read_json(STATE_FILE)

    if saved is None:
        state = new_state()
        add_event(state, "INITIALIZED", "New paper account initialized")
        return state

    if not isinstance(saved, dict):
        raise RuntimeError("State file root is not a JSON object")

    state = copy.deepcopy(saved)
    defaults = new_state()

    for key, value in defaults.items():
        if key not in state:
            state[key] = copy.deepcopy(value)

    state["build"] = BUILD
    state["initial_equity"] = safe_float(
        state.get("initial_equity"), INITIAL_EQUITY
    )
    state["equity"] = safe_float(
        state.get("equity"), state["initial_equity"]
    )
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity"), state["equity"]),
        state["equity"],
    )

    if not isinstance(state.get("trades"), list):
        state["trades"] = []

    if not isinstance(state.get("equity_history"), list):
        state["equity_history"] = []

    if not isinstance(state.get("events"), list):
        state["events"] = []

    # Keep the existing position and its historical fields.
    # Do not silently erase legacy funding or position data.
    position = state.get("position")

    if position is not None:
        if not isinstance(position, dict):
            raise RuntimeError("Position state is invalid")

        aliases = {
            "direction": ["direction", "side"],
            "entry_price": ["entry_price", "entry"],
            "quantity": ["quantity", "size", "qty"],
            "stop_price": ["stop_price", "stop_loss", "stop"],
            "target_price": ["target_price", "take_profit", "target"],
            "opened_at": ["opened_at", "entry_time", "open_time"],
            "entry_fee": ["entry_fee"],
            "funding_paid": ["funding_paid"],
        }

        for target, names in aliases.items():
            if target not in position:
                for name in names:
                    if name in position:
                        position[target] = position[name]
                        break

        direction = str(position.get("direction", "")).upper()

        if direction in ("LONG", "BUY"):
            position["direction"] = "LONG"
        elif direction in ("SHORT", "SELL"):
            position["direction"] = "SHORT"
        else:
            raise RuntimeError(
                "Existing position has unknown direction; refusing to discard it"
            )

        for key in (
            "entry_price", "quantity", "stop_price",
            "target_price", "entry_fee", "funding_paid",
        ):
            position[key] = safe_float(position.get(key), 0.0)

        if (
            position["entry_price"] <= 0
            or position["quantity"] <= 0
            or position["stop_price"] <= 0
        ):
            raise RuntimeError(
                "Existing position is missing valid entry, quantity or stop"
            )

        position.setdefault("leverage", LEVERAGE)
        position.setdefault("margin_used", 0.0)
        position.setdefault("notional", position["entry_price"] * position["quantity"])
        position.setdefault("initial_stop_price", position["stop_price"])
        position.setdefault("risk_distance", abs(
            position["entry_price"] - position["initial_stop_price"]
        ))
        position.setdefault("trailing_active", False)
        position.setdefault("funding_paid", 0.0)
        position.setdefault("last_funding_ts", 0)
        position.setdefault("last_checked_candle_ts", None)
        position.setdefault("entry_fee", 0.0)
        position.setdefault("opened_at", iso_now())

        if "last_checked_candle_ts" not in position:
            position["last_checked_candle_ts"] = None

    state["trades"] = state["trades"][-MAX_STORED_TRADES:]
    state["equity_history"] = state["equity_history"][-MAX_EQUITY_HISTORY:]
    state["events"] = state["events"][-MAX_EVENTS:]

    return state


# ============================================================
# DAILY / DRAWDOWN RISK LOCKS
# ============================================================

def update_daily_baseline(state, marked_equity):
    today = utc_now().date().isoformat()

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_start_equity"] = max(marked_equity, 0.0)
        state["daily_loss_locked"] = False
        add_event(
            state,
            "DAILY_RESET",
            "New UTC day; daily risk baseline reset",
            baseline=state["daily_start_equity"],
        )

    baseline = safe_float(
        state.get("daily_start_equity"), state["initial_equity"]
    )

    if baseline > 0:
        daily_loss = max(
            0.0, (baseline - marked_equity) / baseline
        )
    else:
        daily_loss = 0.0

    if daily_loss >= MAX_DAILY_LOSS_PCT:
        if not state.get("daily_loss_locked", False):
            add_event(
                state,
                "DAILY_LOSS_LOCK",
                "Daily loss limit reached; new entries disabled until UTC day reset",
                daily_loss_pct=daily_loss * 100.0,
            )
        state["daily_loss_locked"] = True

    initial = max(safe_float(state["initial_equity"]), 0.000001)
    peak = max(
        safe_float(state.get("peak_equity"), initial),
        marked_equity,
    )
    state["peak_equity"] = peak

    drawdown = max(0.0, (peak - marked_equity) / peak) if peak > 0 else 0.0

    if drawdown >= MAX_DRAWDOWN_PCT:
        if not state.get("drawdown_locked", False):
            add_event(
                state,
                "DRAWDOWN_LOCK",
                "Maximum drawdown reached; new entries disabled",
                drawdown_pct=drawdown * 100.0,
            )
        state["drawdown_locked"] = True

    return {
        "daily_loss_pct": daily_loss * 100.0,
        "current_drawdown_pct": drawdown * 100.0,
        "daily_loss_locked": bool(state.get("daily_loss_locked")),
        "drawdown_locked": bool(state.get("drawdown_locked")),
    }


# ============================================================
# FUNDING ESTIMATE
# ============================================================

def charge_funding_until(state, position, funding_rate):
    """
    Estimate funding for whole elapsed hours.

    The current public funding rate is applied to each elapsed hour.
    This is an approximation, not a reconstruction of historical
    funding rates. Legacy funding totals are preserved.
    """
    now = timestamp_ms()
    hour_ms = 60 * 60 * 1000

    last_ts = int(safe_float(position.get("last_funding_ts"), 0))

    if last_ts <= 0:
        # First V12 observation for an old position: establish a cursor.
        # Do not fabricate historical funding.
        position["last_funding_ts"] = now
        add_event(
            state,
            "FUNDING_CURSOR_INITIALIZED",
            "Initialized funding cursor for existing position; no backfill applied",
        )
        return

    elapsed_hours = (now - last_ts) // hour_ms

    if elapsed_hours <= 0:
        return

    # Avoid a huge unexpected adjustment after a long outage.
    # Advance only for the capped number of hours and record the estimate.
    charged_hours = min(int(elapsed_hours), 24)

    quantity = safe_float(position.get("quantity"), 0.0)
    price = safe_float(position.get("last_mark_price"), position["entry_price"])
    notional = abs(quantity * price)

    estimated_funding = notional * funding_rate * charged_hours

    # Positive funding: longs pay shorts; negative funding: shorts pay longs.
    if position["direction"] == "LONG":
        cash_change = -estimated_funding
    else:
        cash_change = estimated_funding

    state["equity"] = safe_float(state["equity"]) + cash_change
    position["funding_paid"] = (
        safe_float(position.get("funding_paid")) + cash_change
    )
    state["funding_total"] = (
        safe_float(state.get("funding_total")) + cash_change
    )

    position["last_funding_ts"] = last_ts + charged_hours * hour_ms

    add_event(
        state,
        "FUNDING_ESTIMATE_APPLIED",
        "Estimated funding applied using current rate",
        hours=charged_hours,
        funding_cash_change=cash_change,
        funding_rate=funding_rate,
    )

    if elapsed_hours > 24:
        add_event(
            state,
            "FUNDING_CATCHUP_CAPPED",
            "Funding catch-up capped at 24 hours; older missing hours were not estimated",
            skipped_hours=int(elapsed_hours - 24),
        )


# ============================================================
# POSITION PNL / CLOSING
# ============================================================

def signed_price_change(direction, entry, exit_price):
    if direction == "LONG":
        return exit_price - entry
    return entry - exit_price


def unrealized_pnl(position, mark_price):
    direction = position["direction"]
    entry = safe_float(position["entry_price"])
    quantity = safe_float(position["quantity"])

    gross = signed_price_change(direction, entry, mark_price) * quantity
    exit_fee_estimate = abs(mark_price * quantity) * TAKER_FEE

    return gross - exit_fee_estimate


def close_position(state, position, exit_price, reason, candle_ts=None):
    direction = position["direction"]
    entry = safe_float(position["entry_price"])
    quantity = safe_float(position["quantity"])

    if direction == "LONG":
        fill_price = exit_price * (1.0 - SLIPPAGE)
    else:
        fill_price = exit_price * (1.0 + SLIPPAGE)

    gross_pnl = signed_price_change(
        direction, entry, fill_price
    ) * quantity

    exit_notional = abs(fill_price * quantity)
    exit_fee = exit_notional * TAKER_FEE

    entry_fee = safe_float(position.get("entry_fee"))
    funding_paid = safe_float(position.get("funding_paid"))

    # Entry fee and funding have already affected account equity.
    # Only gross PnL and exit fee are applied at close.
    equity_change_now = gross_pnl - exit_fee
    state["equity"] = safe_float(state["equity"]) + equity_change_now

    net_pnl = gross_pnl - entry_fee - exit_fee + funding_paid

    opened_at = position.get("opened_at", iso_now())

    trade = {
        "direction": direction,
        "side": direction,
        "entry_price": entry,
        "exit_price": fill_price,
        "quantity": quantity,
        "leverage": safe_float(position.get("leverage"), LEVERAGE),
        "margin_used": safe_float(position.get("margin_used")),
        "notional": safe_float(position.get("notional")),
        "opened_at": opened_at,
        "closed_at": iso_now(),
        "exit_time": iso_now(),
        "reason": reason,
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": entry_fee + exit_fee,
        "funding_paid": funding_paid,
        "net_pnl": net_pnl,
        "equity_after_close": state["equity"],
        "build": BUILD,
    }

    if candle_ts is not None:
        trade["exit_candle_ts"] = candle_ts

    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-MAX_STORED_TRADES:]

    if net_pnl < 0:
        state["consecutive_losses"] = (
            int(state.get("consecutive_losses", 0)) + 1
        )
    else:
        state["consecutive_losses"] = 0

    state["last_entry_ts"] = int(
        safe_float(state.get("last_entry_ts"), 0)
    )
    state["last_close_ts"] = timestamp_ms()

    state["position"] = None

    add_event(
        state,
        "POSITION_CLOSED",
        f"{direction} closed: {reason}",
        net_pnl=net_pnl,
        equity=state["equity"],
    )

    return trade


# ============================================================
# CANDLE-BASED POSITION MANAGEMENT
# ============================================================

def candle_exit_price(position, candle):
    """
    Conservative OHLC rule:
    - If stop and target are both touched in one candle, stop wins.
    - Stops and targets execute at their levels, then slippage is applied
      by close_position.
    """
    direction = position["direction"]
    stop = safe_float(position.get("stop_price"))
    target = safe_float(position.get("target_price"))

    if direction == "LONG":
        stop_hit = candle["l"] <= stop
        target_hit = target > 0 and candle["h"] >= target

        if stop_hit:
            return stop, "STOP_LOSS"

        if target_hit:
            return target, "TAKE_PROFIT"

    else:
        stop_hit = candle["h"] >= stop
        target_hit = target > 0 and candle["l"] <= target

        if stop_hit:
            return stop, "STOP_LOSS"

        if target_hit:
            return target, "TAKE_PROFIT"

    return None, None


def process_position_candles(state, candles, mark_price):
    position = state.get("position")

    if not position:
        return

    position["last_mark_price"] = mark_price

    cursor = position.get("last_checked_candle_ts")

    if cursor is None:
        # Safe migration: do not replay candles whose relationship to this
        # old position is unknown. Start from the latest fully closed candle.
        position["last_checked_candle_ts"] = candles[-1]["t"]
        state["last_checked_candle_ts"] = candles[-1]["t"]

        add_event(
            state,
            "LEGACY_POSITION_CURSOR_INITIALIZED",
            "Initialized candle cursor for legacy position; prior candles were not replayed",
            candle_ts=candles[-1]["t"],
        )
        return

    cursor = int(safe_float(cursor, 0))
    pending = [c for c in candles if c["t"] > cursor]

    if not pending:
        # Max holding time must be checked even if no new candle is pending.
        try:
            opened_ts = int(
                datetime.fromisoformat(
                    str(position["opened_at"]).replace("Z", "+00:00")
                ).timestamp() * 1000
            )
        except Exception:
            opened_ts = timestamp_ms()

        if timestamp_ms() - opened_ts >= MAX_HOLD_SECONDS:
            close_position(
                state, position, mark_price, "MAX_HOLD_TIME"
            )
        return

    for candle in pending:
        # Refresh the reference because close_position clears state position.
        position = state.get("position")

        if not position:
            break

        position["last_mark_price"] = candle["c"]

        exit_price, reason = candle_exit_price(position, candle)

        if exit_price is not None:
            close_position(
                state,
                position,
                exit_price,
                reason,
                candle_ts=candle["t"],
            )
            state["last_checked_candle_ts"] = candle["t"]
            break

        direction = position["direction"]
        entry = safe_float(position["entry_price"])
        risk_distance = safe_float(position.get("risk_distance"))

        if risk_distance <= 0:
            risk_distance = abs(
                entry - safe_float(position.get("initial_stop_price"), entry)
            )

        current_atr = safe_float(position.get("entry_atr"))

        if current_atr <= 0:
            current_atr = atr(candles, 14) or 0.0

        if risk_distance > 0 and current_atr > 0:
            favorable = (
                candle["h"] - entry
                if direction == "LONG"
                else entry - candle["l"]
            )

            if favorable >= TRAIL_START_R * risk_distance:
                if not position.get("trailing_active"):
                    position["trailing_active"] = True
                    add_event(
                        state,
                        "TRAILING_ACTIVATED",
                        "Trailing stop activated",
                    )

                if direction == "LONG":
                    candidate = candle["c"] - (
                        current_atr * TRAIL_ATR_MULTIPLIER
                    )
                    position["stop_price"] = max(
                        safe_float(position["stop_price"]), candidate
                    )
                else:
                    candidate = candle["c"] + (
                        current_atr * TRAIL_ATR_MULTIPLIER
                    )
                    position["stop_price"] = min(
                        safe_float(position["stop_price"]), candidate
                    )

        try:
            opened_ts = int(
                datetime.fromisoformat(
                    str(position["opened_at"]).replace("Z", "+00:00")
                ).timestamp() * 1000
            )
        except Exception:
            opened_ts = timestamp_ms()

        if candle["T"] - opened_ts >= MAX_HOLD_SECONDS:
            close_position(
                state,
                position,
                candle["c"],
                "MAX_HOLD_TIME",
                candle_ts=candle["t"],
            )
            state["last_checked_candle_ts"] = candle["t"]
            break

        position["last_checked_candle_ts"] = candle["t"]
        state["last_checked_candle_ts"] = candle["t"]

    # Keep top-level cursor current even after a fully processed batch.
    if state.get("position") is not None and pending:
        state["last_checked_candle_ts"] = pending[-1]["t"]
        state["position"]["last_checked_candle_ts"] = pending[-1]["t"]


# ============================================================
# SIGNAL ENGINE
# ============================================================

def build_signal(c15, c1h, c4h, funding_rate):
    a15 = analyze_timeframe(c15)
    a1h = analyze_timeframe(c1h)
    a4h = analyze_timeframe(c4h)

    long_score = 0
    short_score = 0
    reasons = []

    # Higher timeframe direction.
    if a4h["trend"] == "UP":
        long_score += 2
        reasons.append("4h uptrend")
    elif a4h["trend"] == "DOWN":
        short_score += 2
        reasons.append("4h downtrend")

    if a1h["trend"] == "UP":
        long_score += 2
        reasons.append("1h uptrend")
    elif a1h["trend"] == "DOWN":
        short_score += 2
        reasons.append("1h downtrend")

    # 15m trend and momentum.
    if a15["trend"] == "UP":
        long_score += 1
    elif a15["trend"] == "DOWN":
        short_score += 1

    if a15["rsi"] >= 52:
        long_score += 1
    elif a15["rsi"] <= 48:
        short_score += 1

    if a15["macd_signal"] is not None:
        if a15["macd"] > a15["macd_signal"]:
            long_score += 1
        elif a15["macd"] < a15["macd_signal"]:
            short_score += 1

    if a15["adx"] >= 20:
        if a15["trend"] == "UP":
            long_score += 1
        elif a15["trend"] == "DOWN":
            short_score += 1

    # Avoid chasing very stretched short-term momentum.
    if a15["rsi"] >= 75:
        long_score -= 2
        reasons.append("15m RSI overbought")
    if a15["rsi"] <= 25:
        short_score -= 2
        reasons.append("15m RSI oversold")

    # Funding is only a small modifier, never a standalone entry signal.
    funding_pct = funding_rate * 100.0

    if funding_pct > 0.03:
        short_score += 1
        reasons.append("positive funding")
    elif funding_pct < -0.03:
        long_score += 1
        reasons.append("negative funding")

    if (
        long_score >= MIN_SIGNAL_SCORE
        and long_score >= short_score + 2
    ):
        direction = "LONG"
        score = long_score
    elif (
        short_score >= MIN_SIGNAL_SCORE
        and short_score >= long_score + 2
    ):
        direction = "SHORT"
        score = short_score
    else:
        direction = "WAIT"
        score = max(long_score, short_score)

    return {
        "direction": direction,
        "score": int(score),
        "long_score": int(long_score),
        "short_score": int(short_score),
        "reasons": reasons,
        "timeframes": {
            "15m": a15,
            "1h": a1h,
            "4h": a4h,
        },
        "rsi": a15["rsi"],
        "atr": a15["atr"],
        "adx": a15["adx"],
        "ema20": a15["ema20"],
        "ema50": a15["ema50"],
        "macd": a15["macd"],
        "macd_signal": a15["macd_signal"],
        "trend_15m": a15["trend"],
        "trend_1h": a1h["trend"],
        "trend_4h": a4h["trend"],
    }


# ============================================================
# OPEN POSITION
# ============================================================

def open_position(state, direction, price, atr_value, signal):
    if state.get("position") is not None:
        return False

    if direction not in ("LONG", "SHORT"):
        return False

    equity = safe_float(state.get("equity"))

    if equity <= 0 or atr_value <= 0 or price <= 0:
        return False

    if state.get("daily_loss_locked"):
        add_event(
            state,
            "ENTRY_BLOCKED",
            "Daily loss lock is active",
        )
        return False

    if state.get("drawdown_locked"):
        add_event(
            state,
            "ENTRY_BLOCKED",
            "Maximum drawdown lock is active",
        )
        return False

    if int(state.get("consecutive_losses", 0)) >= MAX_CONSECUTIVE_LOSSES:
        add_event(
            state,
            "ENTRY_BLOCKED",
            "Maximum consecutive losses reached",
        )
        return False

    last_entry = int(safe_float(state.get("last_entry_ts"), 0))
    last_close = int(safe_float(state.get("last_close_ts"), 0))
    last_activity = max(last_entry, last_close)

    if timestamp_ms() - last_activity < COOLDOWN_SECONDS:
        add_event(
            state,
            "ENTRY_BLOCKED",
            "Cooldown period is active",
        )
        return False

    risk_distance = atr_value * ATR_STOP_MULTIPLIER

    if risk_distance <= 0:
        return False

    if direction == "LONG":
        entry_price = price * (1.0 + SLIPPAGE)
        stop_price = entry_price - risk_distance
        target_price = (
            0.0 if RUNNER_MODE
            else entry_price + atr_value * ATR_TARGET_MULTIPLIER
        )
    else:
        entry_price = price * (1.0 - SLIPPAGE)
        stop_price = entry_price + risk_distance
        target_price = (
            0.0 if RUNNER_MODE
            else entry_price - atr_value * ATR_TARGET_MULTIPLIER
        )

    if stop_price <= 0:
        return False

    # Estimate the round-trip cost in risk sizing.
    risk_budget = equity * RISK_PER_TRADE
    risk_per_btc = (
        risk_distance
        + entry_price * TAKER_FEE
        + entry_price * TAKER_FEE
        + entry_price * SLIPPAGE
    )

    if risk_per_btc <= 0:
        return False

    qty_by_risk = risk_budget / risk_per_btc

    max_margin = equity * MAX_MARGIN_FRACTION
    max_notional = max_margin * LEVERAGE
    qty_by_margin = max_notional / entry_price

    quantity = min(qty_by_risk, qty_by_margin)

    if not math.isfinite(quantity) or quantity <= 0:
        return False

    notional = quantity * entry_price
    margin_used = notional / LEVERAGE
    entry_fee = notional * TAKER_FEE

    if margin_used > max_margin + 1e-9:
        return False

    if entry_fee >= equity:
        return False

    # Entry fee is deducted immediately. The open-position equity is
    # cash equity; unrealized PnL is displayed separately.
    state["equity"] = equity - entry_fee

    position = {
        "direction": direction,
        "side": direction,
        "entry_price": entry_price,
        "quantity": quantity,
        "leverage": LEVERAGE,
        "margin_used": margin_used,
        "notional": notional,
        "stop_price": stop_price,
        "initial_stop_price": stop_price,
        "target_price": target_price,
        "risk_distance": abs(entry_price - stop_price),
        "entry_atr": atr_value,
        "entry_fee": entry_fee,
        "funding_paid": 0.0,
        "last_funding_ts": timestamp_ms(),
        "opened_at": iso_now(),
        "opened_ts": timestamp_ms(),
        "last_mark_price": price,
        "trailing_active": False,
        "signal_score": signal["score"],
        "entry_signal": copy.deepcopy(signal),
        "last_checked_candle_ts": None,
    }

    state["position"] = position
    state["last_entry_ts"] = timestamp_ms()

    add_event(
        state,
        "POSITION_OPENED",
        f"{direction} paper position opened",
        entry_price=entry_price,
        quantity=quantity,
        margin_used=margin_used,
        notional=notional,
        entry_fee=entry_fee,
        signal_score=signal["score"],
    )

    return True


# ============================================================
# PERFORMANCE METRICS
# ============================================================

def trade_net_pnl(trade):
    if "net_pnl" in trade:
        return safe_float(trade.get("net_pnl"))

    gross = safe_float(trade.get("gross_pnl"))
    fees = safe_float(trade.get("fees"))
    if fees == 0:
        fees = (
            safe_float(trade.get("entry_fee"))
            + safe_float(trade.get("exit_fee"))
        )

    funding = safe_float(trade.get("funding_paid"))

    return gross - fees + funding


def calculate_metrics(state):
    trades = [
        t for t in state.get("trades", [])
        if isinstance(t, dict)
    ]

    net_values = [trade_net_pnl(t) for t in trades]
    wins = [x for x in net_values if x > 0]
    losses = [x for x in net_values if x < 0]

    closed = len(net_values)
    net_total = sum(net_values)

    win_rate = (len(wins) / closed * 100.0) if closed else 0.0
    net_profit = sum(wins)
    net_loss_abs = abs(sum(losses))

    if net_loss_abs > 0:
        profit_factor = net_profit / net_loss_abs
    elif net_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0

    gross_values = [
        safe_float(t.get("gross_pnl"))
        for t in trades
    ]
    gross_profit = sum(x for x in gross_values if x > 0)
    gross_loss_abs = abs(sum(x for x in gross_values if x < 0))

    if gross_loss_abs > 0:
        gross_profit_factor = gross_profit / gross_loss_abs
    elif gross_profit > 0:
        gross_profit_factor = None
    else:
        gross_profit_factor = 0.0

    fees_total = sum(
        safe_float(t.get("fees"))
        if "fees" in t else (
            safe_float(t.get("entry_fee"))
            + safe_float(t.get("exit_fee"))
        )
        for t in trades
    )

    funding_closed = sum(
        safe_float(t.get("funding_paid"))
        for t in trades
    )

    initial = safe_float(state.get("initial_equity"), INITIAL_EQUITY)
    equity = safe_float(state.get("equity"))

    return {
        "closed_trades": closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_profit": net_total,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss_abs,
        "profit_factor": profit_factor,
        "gross_profit_factor": gross_profit_factor,
        "average_net_pnl": net_total / closed if closed else 0.0,
        "total_fees": fees_total,
        "closed_trade_funding": funding_closed,
        "account_equity_change": equity - initial,
        "consecutive_losses": int(state.get("consecutive_losses", 0)),
    }


# ============================================================
# DASHBOARD DATA
# ============================================================

def make_dashboard(
    state,
    price,
    funding_rate,
    signal,
    risk_status,
    market_data_error=None,
):
    position = state.get("position")
    equity = safe_float(state.get("equity"))
    initial = safe_float(state.get("initial_equity"), INITIAL_EQUITY)

    unrealized = 0.0
    marked_equity = equity

    position_data = None

    if position:
        position["last_mark_price"] = price
        unrealized = unrealized_pnl(position, price)
        marked_equity = equity + unrealized

        position_data = {
            "direction": position["direction"],
            "side": position["direction"],
            "entry_price": position["entry_price"],
            "quantity": position["quantity"],
            "leverage": position.get("leverage", LEVERAGE),
            "margin_used": position.get("margin_used", 0.0),
            "notional": position.get("notional", 0.0),
            "stop_price": position.get("stop_price"),
            "target_price": position.get("target_price"),
            "entry_fee": position.get("entry_fee", 0.0),
            "funding_paid": position.get("funding_paid", 0.0),
            "opened_at": position.get("opened_at"),
            "trailing_active": bool(position.get("trailing_active")),
            "unrealized_pnl": unrealized,
        }

    peak = max(
        safe_float(state.get("peak_equity"), initial),
        marked_equity,
    )
    state["peak_equity"] = peak

    current_dd = (
        max(0.0, (peak - marked_equity) / peak * 100.0)
        if peak > 0 else 0.0
    )

    metrics = calculate_metrics(state)

    data = {
        "build": BUILD,
        "generated_at": iso_now(),
        "price": price,
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate * 100.0,
        "initial_equity": initial,
        "equity": equity,
        "marked_equity": marked_equity,
        "realized_pnl": equity - initial,
        "unrealized_pnl": unrealized,
        "net_pnl": marked_equity - initial,
        "net_pnl_pct": (
            (marked_equity / initial - 1.0) * 100.0
            if initial else 0.0
        ),
        "position": position_data,
        "has_position": position is not None,
        "signal": signal or {
            "direction": "WAIT",
            "score": 0,
            "long_score": 0,
            "short_score": 0,
        },
        "risk": {
            **risk_status,
            "risk_per_trade_pct": RISK_PER_TRADE * 100.0,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100.0,
            "max_daily_loss_pct": MAX_DAILY_LOSS_PCT * 100.0,
            "max_drawdown_limit_pct": MAX_DRAWDOWN_PCT * 100.0,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "leverage": LEVERAGE,
        },
        "current_drawdown_pct": current_dd,
        "max_drawdown_pct": safe_float(
            state.get("max_drawdown_pct"), current_dd
        ),
        "funding_total": safe_float(state.get("funding_total")),
        "metrics": metrics,
        "closed_trades": len(state.get("trades", [])),
        "trades": state.get("trades", [])[-200:],
        "equity_history": state.get("equity_history", [])[-500:],
        "events": state.get("events", [])[-100:],
        "consecutive_losses": int(state.get("consecutive_losses", 0)),
        "daily_loss_locked": bool(state.get("daily_loss_locked")),
        "drawdown_locked": bool(state.get("drawdown_locked")),
        "paper_only": True,
        "live_trading_enabled": False,
        "market_data_ok": market_data_error is None,
        "market_data_error": market_data_error,
    }

    return data


# ============================================================
# MAIN ENGINE
# ============================================================

def main():
    state = load_state()

    # Save migrated state before requesting market data, so that
    # schema normalization is not lost if the public API is down.
    atomic_write_json(STATE_FILE, state)

    try:
        price = fetch_market_price()
        funding_rate = fetch_funding_rate()

        candles15 = fetch_candles("15m", 250)
        candles1h = fetch_candles("1h", 250)
        candles4h = fetch_candles("4h", 250)

        signal = build_signal(
            candles15,
            candles1h,
            candles4h,
            funding_rate,
        )

        state["api_error_count"] = 0

    except Exception as exc:
        state["api_error_count"] = (
            int(state.get("api_error_count", 0)) + 1
        )

        add_event(
            state,
            "MARKET_DATA_ERROR",
            str(exc),
        )

        # Do not make a trading decision with stale or incomplete data.
        # Preserve the existing position and history.
        atomic_write_json(STATE_FILE, state)

        old_data = read_json(DATA_FILE) if DATA_FILE.exists() else {}

        if not isinstance(old_data, dict):
            old_data = {}

        old_data.update({
            "build": BUILD,
            "generated_at": iso_now(),
            "market_data_ok": False,
            "market_data_error": str(exc),
            "paper_only": True,
            "live_trading_enabled": False,
            "events": state.get("events", [])[-100:],
        })

        atomic_write_json(DATA_FILE, old_data)
        print(f"MARKET DATA ERROR: {exc}")
        return

    # Charge only the estimated funding since the position's cursor.
    if state.get("position") is not None:
        charge_funding_until(
            state, state["position"], funding_rate
        )

    # Process closed candles before considering a new entry.
    process_position_candles(state, candles15, price)

    position = state.get("position")

    if position is not None:
        position["last_mark_price"] = price

    marked_equity = safe_float(state.get("equity"))

    if position is not None:
        marked_equity += unrealized_pnl(position, price)

    risk_status = update_daily_baseline(state, marked_equity)

    # Update all-time drawdown tracking.
    previous_peak = safe_float(
        state.get("peak_equity"), state["initial_equity"]
    )
    state["peak_equity"] = max(previous_peak, marked_equity)

    peak = max(state["peak_equity"], 0.000001)
    current_dd = max(
        0.0, (peak - marked_equity) / peak * 100.0
    )

    state["max_drawdown_pct"] = max(
        safe_float(state.get("max_drawdown_pct")),
        current_dd,
    )

    # New entries are only considered when flat.
    # Signal is evaluated on fully closed candles.
    if state.get("position") is None:
        if signal["direction"] in ("LONG", "SHORT"):
            opened = open_position(
                state,
                signal["direction"],
                price,
                signal["atr"],
                signal,
            )

            if opened:
                # A new position starts processing from the latest
                # closed candle; do not replay candles before entry.
                latest_ts = candles15[-1]["t"]
                state["position"]["last_checked_candle_ts"] = latest_ts
                state["last_checked_candle_ts"] = latest_ts

    # Refresh account mark after potential entry or close.
    position = state.get("position")
    unrealized = 0.0

    if position is not None:
        position["last_mark_price"] = price
        unrealized = unrealized_pnl(position, price)

    marked_equity = safe_float(state.get("equity")) + unrealized

    peak = max(
        safe_float(state.get("peak_equity"), state["initial_equity"]),
        marked_equity,
    )
    state["peak_equity"] = peak

    current_dd = (
        max(0.0, (peak - marked_equity) / peak * 100.0)
        if peak > 0 else 0.0
    )
    state["max_drawdown_pct"] = max(
        safe_float(state.get("max_drawdown_pct")),
        current_dd,
    )

    # Record one account snapshot per run.
    state.setdefault("equity_history", []).append({
        "timestamp": iso_now(),
        "equity": safe_float(state.get("equity")),
        "equity_usd": safe_float(state.get("equity")),
        "marked_equity": marked_equity,
        "price": price,
        "unrealized_pnl": unrealized,
        "net_pnl": marked_equity - safe_float(state["initial_equity"]),
        "drawdown_pct": current_dd,
    })

    state["equity_history"] = (
        state["equity_history"][-MAX_EQUITY_HISTORY:]
    )

    dashboard = make_dashboard(
        state,
        price,
        funding_rate,
        signal,
        risk_status,
    )

    # State first, then dashboard. If the second write fails, the
    # account state is still saved for the next run.
    atomic_write_json(STATE_FILE, state)
    atomic_write_json(DATA_FILE, dashboard)

    print(f"BUILD: {BUILD}")
    print(f"PRICE: {price:.2f}")
    print(f"SIGNAL: {signal['direction']} / {signal['score']}")
    print(f"EQUITY: {state['equity']:.6f}")
    print(f"MARKED EQUITY: {dashboard['marked_equity']:.6f}")
    print(f"UNREALIZED PNL: {dashboard['unrealized_pnl']:.6f}")
    print(f"CLOSED TRADES: {dashboard['metrics']['closed_trades']}")
    print(f"WIN RATE: {dashboard['metrics']['win_rate']:.2f}%")
    print(f"DAILY LOSS LOCK: {state['daily_loss_locked']}")
    print(f"DRAWDOWN LOCK: {state['drawdown_locked']}")
    print("PAPER ONLY: True")


if __name__ == "__main__":
    main()