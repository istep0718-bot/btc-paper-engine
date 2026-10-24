#!/usr/bin/env python3
"""BTC Paper Engine V17.2
Public market data only. No wallet, no private keys, no live orders.
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V17_2_2026-10-25"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

# Account and risk settings
INITIAL_EQUITY = 100.0
LEVERAGE = 40.0
RISK_PER_TRADE = 0.0075
MAX_MARGIN_FRACTION = 0.35

DAILY_LOSS_LIMIT = 0.10
MAX_DRAWDOWN_LIMIT = 0.30
MAX_CONSECUTIVE_LOSSES = 10
CONSECUTIVE_PAUSE_SECONDS = 3600
COOLDOWN_SECONDS = 60
MAX_HOLD_SECONDS = 86400

MIN_SIGNAL_SCORE = 6
MIN_SCORE_EDGE = 2

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

ATR_STOP_MULTIPLIER = 1.8
ATR_TARGET_MULTIPLIER = 3.0
MIN_REWARD_RISK = 1.5
TRAIL_ACTIVATION_R = 1.5
ATR_TRAIL_MULTIPLIER = 1.5

CANDLE_LIMIT = 200
HTTP_TIMEOUT = 20
MAX_TRADES_STORED = 1000
MAX_HISTORY_STORED = 1000
BB_PERIOD = 20
BB_STDDEV = 2.0
FIB_LOOKBACK = 80


def now_ts():
    return int(time.time())


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def utc_date():
    return str(datetime.now(timezone.utc).date())


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    temp_path.replace(path)


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def post(payload):
    request = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V17.2/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_timestamp(value):
    timestamp = safe_int(value)
    if timestamp > 100000000000:
        timestamp //= 1000
    if timestamp < 0 or timestamp > now_ts() + 60:
        return 0
    return timestamp


def default_state():
    return {
        "build": BUILD,
        "initial_equity": INITIAL_EQUITY,
        "equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
        "realized_pnl": 0.0,
        "funding_total": 0.0,
        "trades": [],
        "equity_history": [],
        "position": None,
        "last_close_ts": 0,
        "last_candle_ts": 0,
        "consecutive_losses": 0,
        "consecutive_loss_lock": False,
        "consecutive_lock_until": 0,
        "daily_date": utc_date(),
        "daily_start_equity": INITIAL_EQUITY,
        "daily_loss_lock": False,
        "drawdown_lock": False,
        "last_error": None,
        "events": [],
    }


def add_event(state, event_type, **details):
    events = state.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        state["events"] = events

    events.append({
        "timestamp": iso_now(),
        "type": event_type,
        **details,
    })
    state["events"] = events[-200:]


def load_state():
    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict) or not state:
        return default_state()

    defaults = default_state()
    for key, value in defaults.items():
        state.setdefault(key, value)

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
    state["realized_pnl"] = safe_float(state.get("realized_pnl"))
    state["funding_total"] = safe_float(state.get("funding_total"))

    trades = state.get("trades", state.get("paper_trades", []))
    state["trades"] = trades if isinstance(trades, list) else []

    history = state.get("equity_history", [])
    state["equity_history"] = history if isinstance(history, list) else []

    position = state.get("position") or state.get("open_position")
    state["position"] = position if isinstance(position, dict) else None

    for key in ("last_close_ts", "last_candle_ts", "consecutive_lock_until"):
        state[key] = normalize_timestamp(state.get(key))

    state["consecutive_losses"] = max(
        0, safe_int(state.get("consecutive_losses"))
    )

    for key in (
        "consecutive_loss_lock",
        "daily_loss_lock",
        "drawdown_lock",
    ):
        state[key] = bool(state.get(key))

    state["daily_start_equity"] = safe_float(
        state.get("daily_start_equity"), state["equity"]
    )

    if not isinstance(state.get("events"), list):
        state["events"] = []

    # Migrate older position schemas without inventing historical funding.
    position = state.get("position")
    if position:
        position.setdefault("funding_paid", 0.0)

        opened = normalize_timestamp(
            position.get("opened_timestamp", position.get("opened_at", 0))
        )
        position["opened_timestamp"] = opened

        funding_cursor = normalize_timestamp(
            position.get("last_funding_timestamp", 0)
        )

        # Old schemas sometimes have missing or unusable funding timestamps.
        # Start from now rather than inventing a large historical charge.
        if (
            funding_cursor <= 0
            or (opened > 0 and funding_cursor < opened)
            or now_ts() - funding_cursor > MAX_HOLD_SECONDS
        ):
            position["last_funding_timestamp"] = now_ts()
            add_event(
                state,
                "FUNDING_CURSOR_MIGRATED",
                message="Funding cursor reset; no historical catch-up charged",
            )
        else:
            position["last_funding_timestamp"] = funding_cursor

        position.setdefault("last_checked_candle_ts", 0)
        position.setdefault("entry_fee", 0.0)
        position.setdefault("risk_distance", 0.0)
        position.setdefault("runner_mode", False)

    state["build"] = BUILD
    return state


def fetch_price_and_funding():
    mids = post({"type": "allMids"})
    price = safe_float(mids.get(COIN)) if isinstance(mids, dict) else 0.0
    if price <= 0:
        raise RuntimeError("Could not retrieve BTC price")

    funding = 0.0
    try:
        response = post({"type": "metaAndAssetCtxs"})
        if isinstance(response, list) and len(response) >= 2:
            universe = response[0].get("universe", [])
            contexts = response[1]
            for index, asset in enumerate(universe):
                if asset.get("name") == COIN and index < len(contexts):
                    funding = safe_float(contexts[index].get("funding"))
                    break
    except Exception as error:
        # Zero is only a fallback for unavailable funding data.
        print(f"Funding unavailable; using zero for this run: {error}")

    return price, funding


def fetch_candles(interval, limit=CANDLE_LIMIT):
    interval_minutes = {"15m": 15, "1h": 60, "4h": 240}
    if interval not in interval_minutes:
        raise ValueError(f"Unsupported candle interval: {interval}")

    candle_ms = interval_minutes[interval] * 60 * 1000
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - candle_ms * (limit + 5)

    response = post({
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    })

    if not isinstance(response, list):
        raise RuntimeError(f"Invalid {interval} candle response")

    candles = []
    for item in response:
        candle = {
            "t": safe_int(item.get("t")),
            "T": safe_int(item.get("T")),
            "o": safe_float(item.get("o")),
            "h": safe_float(item.get("h")),
            "l": safe_float(item.get("l")),
            "c": safe_float(item.get("c")),
            "v": safe_float(item.get("v")),
            "n": safe_int(item.get("n")),
        }

        if (
            candle["t"] > 0
            and candle["c"] > 0
            and candle["h"] >= candle["l"] > 0
        ):
            candles.append(candle)

    candles.sort(key=lambda candle: candle["t"])
    current_ms = int(time.time() * 1000)

    # Only closed candles may drive signal and stop/target decisions.
    candles = [
        candle for candle in candles
        if candle["t"] + candle_ms <= current_ms
    ]

    if len(candles) < 35:
        raise RuntimeError(f"Not enough closed candles for {interval}")

    return candles[-limit:]


def ema(values, period):
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    changes = [
        values[index] - values[index - 1]
        for index in range(1, len(values))
    ]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    for index in range(period, len(changes)):
        average_gain = (
            average_gain * (period - 1) + gains[index]
        ) / period
        average_loss = (
            average_loss * (period - 1) + losses[index]
        ) / period

    if average_loss <= 0:
        return 100.0 if average_gain > 0 else 50.0

    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return max(candles[-1]["c"] * 0.002, 1.0)

    true_ranges = []
    for index in range(1, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1]["c"]
        true_ranges.append(max(
            candle["h"] - candle["l"],
            abs(candle["h"] - previous_close),
            abs(candle["l"] - previous_close),
        ))

    value = sum(true_ranges[:period]) / period
    for current in true_ranges[period:]:
        value = (value * (period - 1) + current) / period

    return max(value, 0.01)


def bollinger(values, period=BB_PERIOD, multiplier=BB_STDDEV):
    if len(values) < period:
        return {
            "upper": None,
            "middle": None,
            "lower": None,
            "width_pct": None,
            "percent_b": None,
            "zscore": None,
        }

    window = values[-period:]
    middle = sum(window) / period
    variance = sum((value - middle) ** 2 for value in window) / period
    deviation = math.sqrt(max(variance, 0.0))

    upper = middle + multiplier * deviation
    lower = middle - multiplier * deviation
    width = (upper - lower) / middle * 100 if middle else 0.0
    last = values[-1]
    percent_b = (
        (last - lower) / (upper - lower)
        if upper > lower else 0.5
    )

    return {
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "width_pct": width,
        "percent_b": percent_b,
        "zscore": (last - middle) / deviation if deviation else 0.0,
    }


def calc_fibonacci(candles, lookback=FIB_LOOKBACK):
    window = candles[-min(lookback, len(candles)):]
    high_candle = max(window, key=lambda candle: candle["h"])
    low_candle = min(window, key=lambda candle: candle["l"])

    high = high_candle["h"]
    low = low_candle["l"]

    if high <= low:
        return {
            "swing_high": high,
            "swing_low": low,
            "trend": "UNKNOWN",
            "levels": {},
            "nearest_level": None,
            "distance_pct": None,
        }

    if high_candle["t"] > low_candle["t"]:
        direction = "UP"
        levels = {
            "0.236": high - (high - low) * 0.236,
            "0.382": high - (high - low) * 0.382,
            "0.500": high - (high - low) * 0.500,
            "0.618": high - (high - low) * 0.618,
            "0.786": high - (high - low) * 0.786,
        }
    else:
        direction = "DOWN"
        levels = {
            "0.236": low + (high - low) * 0.236,
            "0.382": low + (high - low) * 0.382,
            "0.500": low + (high - low) * 0.500,
            "0.618": low + (high - low) * 0.618,
            "0.786": low + (high - low) * 0.786,
        }

    price = candles[-1]["c"]
    nearest_name, nearest_price = min(
        levels.items(),
        key=lambda item: abs(price - item[1]),
    )

    return {
        "swing_high": high,
        "swing_low": low,
        "trend": direction,
        "levels": levels,
        "nearest_level": nearest_name,
        "nearest_price": nearest_price,
        "distance_pct": (
            abs(price - nearest_price) / price * 100 if price else None
        ),
    }


def trend(candles):
    values = [candle["c"] for candle in candles]
    fast = ema(values, 9)[-1]
    slow = ema(values, 21)[-1]

    if fast > slow * 1.0005:
        return "UP"
    if fast < slow * 0.9995:
        return "DOWN"
    return "SIDEWAYS"


def make_signal(c15, c1h, c4h, funding):
    values = [candle["c"] for candle in c15]
    ema9_values = ema(values, 9)
    ema21_values = ema(values, 21)
    rsi = calc_rsi(values)
    atr = calc_atr(c15)
    bb = bollinger(values)
    fib = calc_fibonacci(c1h)

    trend15 = trend(c15)
    trend1h = trend(c1h)
    trend4h = trend(c4h)

    long_score = 0
    short_score = 0
    reasons = []
    flags = []

    if ema9_values[-1] > ema21_values[-1]:
        long_score += 2
        reasons.append("15m EMA bullish")
    elif ema9_values[-1] < ema21_values[-1]:
        short_score += 2
        reasons.append("15m EMA bearish")

    for timeframe, direction in (
        ("15m", trend15),
        ("1h", trend1h),
        ("4h", trend4h),
    ):
        if direction == "UP":
            long_score += 2
            reasons.append(f"{timeframe} uptrend")
        elif direction == "DOWN":
            short_score += 2
            reasons.append(f"{timeframe} downtrend")

    if 45 <= rsi <= 67:
        long_score += 1
    if 33 <= rsi <= 55:
        short_score += 1

    if rsi > 75:
        long_score -= 2
        flags.append("RSI overbought")
    if rsi < 25:
        short_score -= 2
        flags.append("RSI oversold")

    if len(values) >= 5 and values[-5] > 0:
        momentum = values[-1] / values[-5] - 1
        if momentum > 0.001:
            long_score += 1
            reasons.append("15m positive momentum")
        elif momentum < -0.001:
            short_score += 1
            reasons.append("15m negative momentum")

    if funding > 0.0003:
        short_score += 1
        reasons.append("positive funding")
    elif funding < -0.0003:
        long_score += 1
        reasons.append("negative funding")

    if bb["percent_b"] is not None:
        if (
            bb["percent_b"] < 0.08
            and values[-1] > values[-2]
            and rsi < 45
        ):
            long_score += 1
            reasons.append("BB lower-band recovery")
        elif (
            bb["percent_b"] > 0.92
            and values[-1] < values[-2]
            and rsi > 55
        ):
            short_score += 1
            reasons.append("BB upper-band rejection")

        if bb["width_pct"] is not None and bb["width_pct"] < 0.35:
            flags.append("BB squeeze: breakout unconfirmed")

    near_fib = (
        fib.get("distance_pct") is not None
        and fib["distance_pct"] <= 0.2
    )
    if near_fib:
        if fib["trend"] == "UP" and trend1h == "UP" and rsi >= 40:
            long_score += 1
            reasons.append(f"Fib support area {fib['nearest_level']}")
        elif fib["trend"] == "DOWN" and trend1h == "DOWN" and rsi <= 60:
            short_score += 1
            reasons.append(f"Fib resistance area {fib['nearest_level']}")

    direction = "WAIT"
    edge = abs(long_score - short_score)

    if (
        long_score >= MIN_SIGNAL_SCORE
        and long_score - short_score >= MIN_SCORE_EDGE
    ):
        direction = "LONG"
    elif (
        short_score >= MIN_SIGNAL_SCORE
        and short_score - long_score >= MIN_SCORE_EDGE
    ):
        direction = "SHORT"

    return {
        "direction": direction,
        "score": max(long_score, short_score),
        "long_score": long_score,
        "short_score": short_score,
        "score_edge": edge,
        "rsi": round(rsi, 4),
        "atr": round(atr, 6),
        "ema9": round(ema9_values[-1], 6),
        "ema21": round(ema21_values[-1], 6),
        "trend_15m": trend15,
        "trend_1h": trend1h,
        "trend_4h": trend4h,
        "bollinger": {
            "period": BB_PERIOD,
            "stddev": BB_STDDEV,
            **{
                key: round(value, 6)
                if isinstance(value, (int, float)) else value
                for key, value in bb.items()
            },
        },
        "fibonacci": {
            "lookback_1h_candles": FIB_LOOKBACK,
            **fib,
        },
        "timestamp": iso_now(),
        "reference_price": values[-1],
        "flags": flags,
        "reasons": reasons or ["No clear directional advantage"],
    }


def unrealized(position, price):
    if not position:
        return 0.0

    quantity = safe_float(position.get("quantity"))
    entry = safe_float(position.get("entry_price"))

    if position.get("side") == "LONG":
        return (price - entry) * quantity
    if position.get("side") == "SHORT":
        return (entry - price) * quantity
    return 0.0


def drawdown(state, equity=None):
    value = (
        safe_float(state.get("equity"))
        if equity is None else safe_float(equity)
    )
    peak = max(
        safe_float(state.get("peak_equity")),
        value,
        0.01,
    )
    return max(0.0, (peak - value) / peak)


def refresh_locks(state, marked=None):
    today = utc_date()

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_start_equity"] = safe_float(state.get("equity"))
        state["daily_loss_lock"] = False
        add_event(state, "DAILY_RISK_RESET", date=today)

    equity = safe_float(state.get("equity"))
    marked_equity = equity if marked is None else safe_float(marked, equity)
    day_start = max(
        safe_float(state.get("daily_start_equity"), equity),
        0.01,
    )

    daily_loss = max(0.0, (day_start - marked_equity) / day_start)
    current_drawdown = drawdown(state, marked_equity)

    if daily_loss >= DAILY_LOSS_LIMIT and not state.get("daily_loss_lock"):
        state["daily_loss_lock"] = True
        add_event(
            state,
            "DAILY_LOSS_LOCK",
            daily_loss_pct=round(daily_loss * 100, 4),
        )

    if (
        current_drawdown >= MAX_DRAWDOWN_LIMIT
        and not state.get("drawdown_lock")
    ):
        state["drawdown_lock"] = True
        add_event(
            state,
            "DRAWDOWN_LOCK",
            drawdown_pct=round(current_drawdown * 100, 4),
        )

    # A 10-loss streak pauses entries for one hour, then resets the
    # streak counter so the engine does not remain permanently locked.
    now = now_ts()
    losses = safe_int(state.get("consecutive_losses"))
    lock_until = normalize_timestamp(state.get("consecutive_lock_until"))

    if losses >= MAX_CONSECUTIVE_LOSSES and not state.get("consecutive_loss_lock"):
        state["consecutive_loss_lock"] = True
        state["consecutive_lock_until"] = now + CONSECUTIVE_PAUSE_SECONDS
        lock_until = state["consecutive_lock_until"]
        add_event(
            state,
            "CONSECUTIVE_LOSS_PAUSE",
            consecutive_losses=losses,
            pause_seconds=CONSECUTIVE_PAUSE_SECONDS,
        )

    if state.get("consecutive_loss_lock"):
        if lock_until <= 0:
            state["consecutive_lock_until"] = now + CONSECUTIVE_PAUSE_SECONDS
            lock_until = state["consecutive_lock_until"]

        if now >= lock_until:
            state["consecutive_loss_lock"] = False
            state["consecutive_lock_until"] = 0
            state["consecutive_losses"] = 0
            add_event(
                state,
                "CONSECUTIVE_LOSS_PAUSE_ENDED",
                message="Entry pause ended; historical trades retained",
            )

    pause_remaining = max(
        0,
        safe_int(state.get("consecutive_lock_until")) - now,
    ) if state.get("consecutive_loss_lock") else 0

    return {
        "daily_loss_pct": round(daily_loss * 100, 4),
        "daily_loss_limit_pct": DAILY_LOSS_LIMIT * 100,
        "daily_loss_lock": bool(state.get("daily_loss_lock")),
        "drawdown_pct": round(current_drawdown * 100, 4),
        "drawdown_limit_pct": MAX_DRAWDOWN_LIMIT * 100,
        "drawdown_lock": bool(state.get("drawdown_lock")),
        "consecutive_losses": safe_int(state.get("consecutive_losses")),
        "consecutive_loss_limit": MAX_CONSECUTIVE_LOSSES,
        "consecutive_loss_lock": bool(state.get("consecutive_loss_lock")),
        "consecutive_pause_remaining_seconds": pause_remaining,
        "risk_per_trade_pct": RISK_PER_TRADE * 100,
        "max_margin_pct": MAX_MARGIN_FRACTION * 100,
        "leverage": LEVERAGE,
        "paper_only": True,
        "live_trading_enabled": False,
    }


def size_position(state, entry, stop):
    equity = safe_float(state.get("equity"))
    distance = abs(entry - stop)

    if equity <= 0 or entry <= 0 or distance <= 0:
        return None, "Invalid sizing inputs"

    risk_budget = equity * RISK_PER_TRADE
    estimated_cost_per_unit = (
        distance + entry * (2 * FEE_RATE + 2 * SLIPPAGE_RATE)
    )

    if estimated_cost_per_unit <= 0:
        return None, "Invalid estimated cost"

    quantity = min(
        risk_budget / estimated_cost_per_unit,
        equity * MAX_MARGIN_FRACTION * LEVERAGE / entry,
    )

    if quantity <= 0:
        return None, "Position quantity is zero"

    notional = quantity * entry
    margin = notional / LEVERAGE
    planned_risk = quantity * estimated_cost_per_unit

    if planned_risk > risk_budget * 1.000001:
        return None, "Risk budget check failed"

    if margin > equity * MAX_MARGIN_FRACTION * 1.000001:
        return None, "Maximum margin check failed"

    return {
        "quantity": quantity,
        "notional": notional,
        "margin_used": margin,
        "risk_budget": risk_budget,
        "planned_risk": planned_risk,
    }, None


def open_position(state, side, price, atr, signal, latest_candle_ts):
    slip = price * SLIPPAGE_RATE
    entry = price + slip if side == "LONG" else price - slip

    distance = max(atr * ATR_STOP_MULTIPLIER, price * 0.001)
    target_distance = max(
        atr * ATR_TARGET_MULTIPLIER,
        distance * MIN_REWARD_RISK,
    )

    stop = entry - distance if side == "LONG" else entry + distance
    target = (
        entry + target_distance
        if side == "LONG" else entry - target_distance
    )

    sizing, error = size_position(state, entry, stop)
    if error:
        return False, error

    entry_fee = sizing["notional"] * FEE_RATE
    equity = safe_float(state.get("equity"))

    if entry_fee >= equity:
        return False, "Entry fee exceeds available equity"

    state["equity"] = equity - entry_fee
    now = now_ts()

    state["position"] = {
        "side": side,
        "entry_price": entry,
        "quantity": sizing["quantity"],
        "leverage": LEVERAGE,
        "margin_used": sizing["margin_used"],
        "notional": sizing["notional"],
        "risk_distance": distance,
        "risk_budget": sizing["risk_budget"],
        "planned_risk": sizing["planned_risk"],
        "stop_price": stop,
        "target_price": target,
        "initial_stop_price": stop,
        "initial_target_price": target,
        "opened_timestamp": now,
        "entry_time": iso_now(),
        "entry_fee": entry_fee,
        "funding_paid": 0.0,
        "last_funding_timestamp": now,
        "last_checked_candle_ts": safe_int(latest_candle_ts),
        "runner_mode": False,
        "entry_signal_score": safe_int(signal.get("score")),
        "entry_rsi": safe_float(signal.get("rsi"), 50),
        "entry_signal": signal.get("direction"),
        "entry_bollinger": signal.get("bollinger", {}),
        "entry_fibonacci": signal.get("fibonacci", {}),
        "build": BUILD,
    }

    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        safe_float(state.get("equity")),
    )

    add_event(
        state,
        "POSITION_OPENED",
        side=side,
        entry_price=entry,
        quantity=sizing["quantity"],
        stop_price=stop,
        target_price=target,
    )
    return True, f"{side} opened"


def close_position(state, price, reason):
    position = state.get("position")
    if not position:
        return False

    side = position.get("side")
    quantity = safe_float(position.get("quantity"))
    entry = safe_float(position.get("entry_price"))

    if side not in ("LONG", "SHORT") or quantity <= 0 or entry <= 0 or price <= 0:
        state["last_error"] = "Invalid position values at close"
        return False

    exit_price = (
        price * (1 - SLIPPAGE_RATE)
        if side == "LONG"
        else price * (1 + SLIPPAGE_RATE)
    )

    gross_pnl = (
        (exit_price - entry) * quantity
        if side == "LONG"
        else (entry - exit_price) * quantity
    )

    entry_fee = safe_float(position.get("entry_fee"))
    exit_fee = abs(exit_price * quantity) * FEE_RATE
    funding_cash_effect = safe_float(position.get("funding_paid"))

    net_pnl = gross_pnl - entry_fee - exit_fee + funding_cash_effect

    # Entry fee and funding were already applied when they occurred.
    # Add only gross PnL and the exit fee to the account cash balance.
    state["equity"] = (
        safe_float(state.get("equity")) + gross_pnl - exit_fee
    )
    state["realized_pnl"] = (
        safe_float(state.get("realized_pnl")) + net_pnl
    )

    margin = max(safe_float(position.get("margin_used")), 0.01)
    trade = {
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": quantity,
        "leverage": safe_float(position.get("leverage"), LEVERAGE),
        "margin_used": margin,
        "notional": safe_float(position.get("notional")),
        "opened_timestamp": safe_int(position.get("opened_timestamp")),
        "entry_time": position.get("entry_time"),
        "exit_timestamp": now_ts(),
        "exit_time": iso_now(),
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": entry_fee + exit_fee,
        "funding_paid": funding_cash_effect,
        "net_pnl": net_pnl,
        "net_return_pct": net_pnl / margin * 100,
        "reason": reason,
        "planned_risk": safe_float(position.get("planned_risk")),
        "entry_signal_score": safe_int(position.get("entry_signal_score")),
        "entry_rsi": safe_float(position.get("entry_rsi"), 50),
        "entry_bollinger": position.get("entry_bollinger", {}),
        "entry_fibonacci": position.get("entry_fibonacci", {}),
        "build": BUILD,
    }

    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-MAX_TRADES_STORED:]

    if net_pnl < 0:
        state["consecutive_losses"] = (
            safe_int(state.get("consecutive_losses")) + 1
        )
    elif net_pnl > 0:
        state["consecutive_losses"] = 0

    state["last_close_ts"] = now_ts()
    state["position"] = None
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        safe_float(state.get("equity")),
    )

    add_event(
        state,
        "POSITION_CLOSED",
        side=side,
        reason=reason,
        gross_pnl=gross_pnl,
        fees=entry_fee + exit_fee,
        funding_cash_effect=funding_cash_effect,
        net_pnl=net_pnl,
    )
    return True


def apply_funding(state, funding, price):
    position = state.get("position")
    if not position:
        return

    now = now_ts()
    opened = normalize_timestamp(position.get("opened_timestamp"))
    last = normalize_timestamp(position.get("last_funding_timestamp"))

    # Never charge an invented catch-up period after a broken/old cursor.
    if (
        last <= 0
        or (opened > 0 and last < opened)
        or now - last > MAX_HOLD_SECONDS
    ):
        position["last_funding_timestamp"] = now
        add_event(
            state,
            "FUNDING_CURSOR_RESET",
            message="Invalid or stale cursor; no catch-up funding applied",
        )
        return

    elapsed_hours = (now - last) / 3600.0
    if elapsed_hours <= 0:
        return

    # Hyperliquid's published funding rate is hourly.
    # Positive rate: LONG pays, SHORT receives.
    notional = abs(
        safe_float(position.get("quantity")) * price
    )
    if position.get("side") == "LONG":
        cash_change = -notional * funding * elapsed_hours
    else:
        cash_change = notional * funding * elapsed_hours

    position["funding_paid"] = (
        safe_float(position.get("funding_paid")) + cash_change
    )
    state["funding_total"] = (
        safe_float(state.get("funding_total")) + cash_change
    )
    state["equity"] = safe_float(state.get("equity")) + cash_change
    position["last_funding_timestamp"] = now

    if abs(cash_change) > 0:
        add_event(
            state,
            "FUNDING_ESTIMATE_APPLIED",
            hours=round(elapsed_hours, 4),
            funding_rate=funding,
            cash_change=cash_change,
        )


def manage_position(state, candles, price, funding, atr):
    position = state.get("position")
    if not position:
        return

    apply_funding(state, funding, price)
    position = state.get("position")
    if not position:
        return

    side = position.get("side")
    entry = safe_float(position.get("entry_price"))
    stop = safe_float(position.get("stop_price"))
    target = safe_float(position.get("target_price"))
    risk_distance = max(
        safe_float(position.get("risk_distance")),
        0.01,
    )

    cursor = safe_int(position.get("last_checked_candle_ts"))

    # On a legacy position with no candle cursor, begin with the latest
    # closed candle. Do not replay unknown candles from before migration.
    if cursor <= 0:
        position["last_checked_candle_ts"] = safe_int(candles[-1]["t"])
        add_event(
            state,
            "CANDLE_CURSOR_INITIALIZED",
            message="Initialized legacy position cursor; no old candles replayed",
        )
        cursor = safe_int(candles[-1]["t"])

    for candle in candles:
        candle_ts = safe_int(candle["t"])
        if candle_ts <= cursor:
            continue

        high = candle["h"]
        low = candle["l"]

        # Conservative rule: if stop and target both hit in one candle,
        # assume the stop was hit first.
        if side == "LONG":
            if low <= stop:
                close_position(state, stop, "STOP_LOSS")
                return
            if high >= target:
                close_position(state, target, "TAKE_PROFIT")
                return
            favorable_move = high - entry
        else:
            if high >= stop:
                close_position(state, stop, "STOP_LOSS")
                return
            if low <= target:
                close_position(state, target, "TAKE_PROFIT")
                return
            favorable_move = entry - low

        if favorable_move >= risk_distance * TRAIL_ACTIVATION_R:
            position["runner_mode"] = True
            trail_distance = max(
                atr * ATR_TRAIL_MULTIPLIER,
                0.01,
            )

            if side == "LONG":
                position["stop_price"] = max(
                    stop,
                    entry,
                    high - trail_distance,
                )
            else:
                position["stop_price"] = min(
                    stop,
                    entry,
                    low + trail_distance,
                )

            stop = safe_float(position.get("stop_price"))

        position["last_checked_candle_ts"] = candle_ts
        cursor = candle_ts

    opened = normalize_timestamp(position.get("opened_timestamp"))
    if opened > 0 and now_ts() - opened >= MAX_HOLD_SECONDS:
        close_position(state, price, "MAX_HOLD_TIME_MARKET")


def metrics(state):
    trades = [
        trade for trade in state.get("trades", [])
        if isinstance(trade, dict) and "net_pnl" in trade
    ]

    wins = [trade for trade in trades if safe_float(trade.get("net_pnl")) > 0]
    losses = [trade for trade in trades if safe_float(trade.get("net_pnl")) < 0]

    total_net = sum(safe_float(trade.get("net_pnl")) for trade in trades)
    gross_wins = sum(safe_float(trade.get("net_pnl")) for trade in wins)
    gross_losses = abs(
        sum(safe_float(trade.get("net_pnl")) for trade in losses)
    )

    today = utc_date()
    todays_trades = [
        trade for trade in trades
        if str(trade.get("exit_time", "")).startswith(today)
    ]

    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven_trades": len(trades) - len(wins) - len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "total_net_pnl": total_net,
        "average_trade": total_net / len(trades) if trades else 0.0,
        "average_win": gross_wins / len(wins) if wins else 0.0,
        "average_loss": -gross_losses / len(losses) if losses else 0.0,
        "profit_factor": (
            gross_wins / gross_losses
            if gross_losses > 0
            else 999.0 if gross_wins > 0 else 0.0
        ),
        "expectancy_per_trade": total_net / len(trades) if trades else 0.0,
        "best_trade": max(
            (safe_float(trade.get("net_pnl")) for trade in trades),
            default=0.0,
        ),
        "worst_trade": min(
            (safe_float(trade.get("net_pnl")) for trade in trades),
            default=0.0,
        ),
        "trades_today_utc": len(todays_trades),
        "wins_today_utc": sum(
            safe_float(trade.get("net_pnl")) > 0 for trade in todays_trades
        ),
        "losses_today_utc": sum(
            safe_float(trade.get("net_pnl")) < 0 for trade in todays_trades
        ),
    }


def make_dashboard(state, price, funding, signal, market_status="OK"):
    position = state.get("position")
    unrealized_pnl = unrealized(position, price) if position else 0.0
    marked_equity = safe_float(state.get("equity")) + unrealized_pnl
    initial_equity = max(
        safe_float(state.get("initial_equity")),
        0.01,
    )

    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        marked_equity,
    )

    risk = refresh_locks(state, marked_equity)
    stats = metrics(state)

    history = state.setdefault("equity_history", [])
    history.append({
        "timestamp": iso_now(),
        "equity": safe_float(state.get("equity")),
        "marked_equity": marked_equity,
        "price": price,
    })
    state["equity_history"] = history[-MAX_HISTORY_STORED:]

    position_view = dict(position) if position else None
    if position_view:
        position_view["unrealized_pnl"] = unrealized_pnl

    return {
        "build": BUILD,
        "generated_at": iso_now(),
        "market_status": market_status,
        "price": price,
        "funding_rate": funding,
        "funding_rate_pct": funding * 100,
        "initial_equity": initial_equity,
        "equity": safe_float(state.get("equity")),
        "marked_equity": marked_equity,
        "realized_pnl": safe_float(state.get("realized_pnl")),
        "unrealized_pnl": unrealized_pnl,
        "net_pnl": marked_equity - initial_equity,
        "net_pnl_pct": (marked_equity - initial_equity) / initial_equity * 100,
        "position": position_view,
        "open_position": position_view,
        "has_position": bool(position),
        "signal": signal,
        "risk": risk,
        "current_drawdown_pct": risk["drawdown_pct"],
        "max_drawdown_pct": risk["drawdown_pct"],
        "funding_total": safe_float(state.get("funding_total")),
        "metrics": stats,
        "closed_trades": stats["closed_trades"],
        "win_rate": stats["win_rate"],
        "trades": state.get("trades", [])[-200:],
        "paper_trades": state.get("trades", [])[-200:],
        "equity_history": state.get("equity_history", [])[-300:],
        "events": state.get("events", [])[-100:],
        "entry_pause": {
            "consecutive_loss_lock": bool(state.get("consecutive_loss_lock")),
            "remaining_seconds": risk["consecutive_pause_remaining_seconds"],
        },
        "config": {
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100,
            "leverage": LEVERAGE,
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT * 100,
            "max_drawdown_limit_pct": MAX_DRAWDOWN_LIMIT * 100,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "consecutive_pause_seconds": CONSECUTIVE_PAUSE_SECONDS,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "max_hold_hours": MAX_HOLD_SECONDS / 3600,
            "min_signal_score": MIN_SIGNAL_SCORE,
            "min_score_edge": MIN_SCORE_EDGE,
            "fee_rate_per_side": FEE_RATE,
            "slippage_rate_per_side": SLIPPAGE_RATE,
            "bollinger_period": BB_PERIOD,
            "bollinger_stddev": BB_STDDEV,
            "fibonacci_lookback_1h": FIB_LOOKBACK,
            "paper_only": True,
            "live_trading_enabled": False,
        },
        "last_error": state.get("last_error"),
    }


def main():
    state = load_state()
    state["last_error"] = None

    try:
        price, funding = fetch_price_and_funding()
        candles_15m = fetch_candles("15m")
        candles_1h = fetch_candles("1h")
        candles_4h = fetch_candles("4h")

        signal = make_signal(
            candles_15m,
            candles_1h,
            candles_4h,
            funding,
        )

        atr = max(
            safe_float(signal.get("atr"), price * 0.002),
            price * 0.0005,
        )

        # Manage an existing position before considering a new entry.
        if state.get("position"):
            manage_position(
                state,
                candles_15m,
                price,
                funding,
                atr,
            )

        latest_candle_ts = safe_int(candles_15m[-1]["t"])
        state["last_candle_ts"] = max(
            normalize_timestamp(state.get("last_candle_ts")),
            latest_candle_ts,
        )

        current_unrealized = (
            unrealized(state.get("position"), price)
            if state.get("position") else 0.0
        )
        marked_equity = safe_float(state.get("equity")) + current_unrealized
        locks = refresh_locks(state, marked_equity)

        last_close = normalize_timestamp(state.get("last_close_ts"))
        now = now_ts()
        cooldown_remaining = (
            max(0, COOLDOWN_SECONDS - (now - last_close))
            if 0 < last_close <= now else 0
        )

        if state.get("position"):
            status = "Position already open"
        elif locks["daily_loss_lock"]:
            status = "Daily loss lock active"
        elif locks["drawdown_lock"]:
            status = "Drawdown lock active"
        elif locks["consecutive_loss_lock"]:
            status = (
                "Consecutive-loss pause: "
                f"{locks['consecutive_pause_remaining_seconds']}s remaining"
            )
        elif cooldown_remaining > 0:
            status = f"Cooldown: {cooldown_remaining}s remaining"
        elif signal["direction"] in ("LONG", "SHORT"):
            opened, message = open_position(
                state,
                signal["direction"],
                price,
                atr,
                signal,
                latest_candle_ts,
            )
            status = message if opened else f"Entry skipped: {message}"
        else:
            status = "No entry: signal is WAIT"

        signal["entry_status"] = status
        signal["cooldown_remaining_seconds"] = cooldown_remaining
        signal["paper_only"] = True
        signal["live_trading_enabled"] = False

        dashboard = make_dashboard(
            state,
            price,
            funding,
            signal,
        )

        write_json(STATE_FILE, state)
        write_json(DATA_FILE, dashboard)

        print(
            f"BUILD: {BUILD}\n"
            f"PRICE: {price:.2f}\n"
            f"SIGNAL: {signal['direction']} / {signal['score']} "
            f"(long={signal['long_score']}, short={signal['short_score']})\n"
            f"ENTRY STATUS: {status}\n"
            f"EQUITY: {state['equity']:.6f}\n"
            f"MARKED EQUITY: {dashboard['marked_equity']:.6f}\n"
            f"CLOSED TRADES: {dashboard['closed_trades']}\n"
            f"WIN RATE: {dashboard['win_rate']:.2f}%\n"
            f"EXPECTANCY: {dashboard['metrics']['expectancy_per_trade']:.6f}\n"
            f"PROFIT FACTOR: {dashboard['metrics']['profit_factor']:.4f}\n"
            f"DAILY LOSS LOCK: {locks['daily_loss_lock']}\n"
            f"DRAWDOWN LOCK: {locks['drawdown_lock']}\n"
            f"CONSECUTIVE LOSS PAUSE: {locks['consecutive_loss_lock']}\n"
            "PAPER ONLY: True\n"
            "LIVE TRADING ENABLED: False"
        )

    except Exception as error:
        state["last_error"] = f"{type(error).__name__}: {error}"
        state["build"] = BUILD

        try:
            old_dashboard = read_json(DATA_FILE, {})
            if isinstance(old_dashboard, dict) and old_dashboard:
                old_dashboard.update({
                    "build": BUILD,
                    "generated_at": iso_now(),
                    "market_status": "ERROR",
                    "last_error": state["last_error"],
                })
                write_json(DATA_FILE, old_dashboard)
        except Exception:
            pass

        write_json(STATE_FILE, state)
        print(f"ERROR: {state['last_error']}")
        raise


if __name__ == "__main__":
    main()