#!/usr/bin/env python3
"""
BTC Paper Engine V11
Paper trading only. No wallet, no private keys, no live orders.
Python standard library only.

Files:
  state.json
  docs/data.json

V11 improvements:
- Separate closed-trade results from account equity / open PnL.
- Track current drawdown and historical maximum drawdown separately.
- Process every available closed 15m candle since the position's last check.
- Evaluate stop/target before updating the trailing stop.
- Include estimated fees and slippage in position sizing.
- Preserve existing state and trade history.
- Keep legacy dashboard field names where practical.
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================

BUILD = "BTC_PAPER_V11_2026-10-23"

API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0

# Risk limits
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_SECONDS = 3600
MAX_HOLD_SECONDS = 24 * 60 * 60

# Trading costs: assumptions for simulation, not guaranteed market costs.
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

# Funding is estimated from the latest observed rate.
# It is NOT a reconstruction of historical funding rates.
FUNDING_INTERVAL_SECONDS = 3600

MAX_CANDLES = 240
MAX_TRADES_STORED = 2000
MAX_HISTORY_STORED = 1000
HTTP_TIMEOUT = 20

INTERVALS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_ts():
    return time.time()


def iso_time(ts=None):
    if ts is None:
        ts = now_ts()
    return datetime.fromtimestamp(
        float(ts), tz=timezone.utc
    ).isoformat()


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def sign_for_direction(direction):
    return 1 if str(direction).upper() == "LONG" else -1


def json_write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


# ============================================================
# HYPERLIQUID PUBLIC MARKET DATA
# ============================================================

def api_post(payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V11",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def fetch_candles(interval, limit=MAX_CANDLES):
    now_ms = int(time.time() * 1000)
    interval_ms = INTERVALS[interval]

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": now_ms - interval_ms * (limit + 5),
            "endTime": now_ms,
        },
    }

    rows = api_post(payload)
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected candle response for {interval}")

    candles = []
    for row in rows:
        try:
            candle = {
                "t": safe_int(row.get("t")),
                "T": safe_int(row.get("T")),
                "o": safe_float(row.get("o")),
                "h": safe_float(row.get("h")),
                "l": safe_float(row.get("l")),
                "c": safe_float(row.get("c")),
                "v": safe_float(row.get("v")),
                "n": safe_int(row.get("n")),
            }
        except AttributeError:
            continue

        if (
            candle["t"] > 0
            and candle["h"] > 0
            and candle["l"] > 0
            and candle["c"] > 0
        ):
            candles.append(candle)

    # Sort and deduplicate by candle start timestamp.
    unique = {}
    for candle in candles:
        unique[candle["t"]] = candle
    candles = sorted(unique.values(), key=lambda item: item["t"])

    # Exclude any candle that has not fully closed yet.
    now_ms = int(time.time() * 1000)
    candles = [
        candle for candle in candles
        if candle["T"] > 0 and candle["T"] < now_ms
    ]

    if len(candles) < 110:
        raise RuntimeError(
            f"Not enough closed candles for {interval}: {len(candles)}"
        )

    return candles[-limit:]


def fetch_funding_rate():
    result = api_post({
        "type": "metaAndAssetCtxs",
    })

    try:
        universe = result[0]["universe"]
        contexts = result[1]
        for index, item in enumerate(universe):
            if item.get("name") == COIN:
                # Hyperliquid's funding value is a decimal rate per hour.
                return safe_float(contexts[index].get("funding"))
    except (IndexError, KeyError, TypeError, AttributeError):
        pass

    raise RuntimeError("Could not read BTC funding rate")


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    multiplier = 2.0 / (period + 1.0)
    previous = seed

    for index in range(period, len(values)):
        previous = (
            values[index] * multiplier
            + previous * (1.0 - multiplier)
        )
        result[index] = previous

    return result


def rsi(values, period=14):
    result = [None] * len(values)
    if len(values) <= period:
        return result

    gains = []
    losses = []

    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    def calculate(avg_gain, avg_loss):
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    result[period] = calculate(average_gain, average_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        average_gain = (
            average_gain * (period - 1) + gain
        ) / period
        average_loss = (
            average_loss * (period - 1) + loss
        ) / period

        result[index] = calculate(average_gain, average_loss)

    return result


def atr(candles, period=14):
    highs = [c["h"] for c in candles]
    lows = [c["l"] for c in candles]
    closes = [c["c"] for c in candles]

    true_ranges = []
    for index in range(len(candles)):
        if index == 0:
            true_ranges.append(highs[index] - lows[index])
        else:
            true_ranges.append(max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            ))

    result = [None] * len(candles)
    if len(true_ranges) < period:
        return result

    previous = sum(true_ranges[:period]) / period
    result[period - 1] = previous

    for index in range(period, len(true_ranges)):
        previous = (
            previous * (period - 1) + true_ranges[index]
        ) / period
        result[index] = previous

    return result


def adx(candles, period=14):
    count = len(candles)
    result = [None] * count

    if count < period * 2 + 2:
        return result

    tr_values = [0.0] * count
    plus_dm = [0.0] * count
    minus_dm = [0.0] * count

    for index in range(1, count):
        up_move = candles[index]["h"] - candles[index - 1]["h"]
        down_move = candles[index - 1]["l"] - candles[index]["l"]

        plus_dm[index] = (
            up_move if up_move > down_move and up_move > 0 else 0.0
        )
        minus_dm[index] = (
            down_move if down_move > up_move and down_move > 0 else 0.0
        )

        tr_values[index] = max(
            candles[index]["h"] - candles[index]["l"],
            abs(candles[index]["h"] - candles[index - 1]["c"]),
            abs(candles[index]["l"] - candles[index - 1]["c"]),
        )

    smoothed_tr = sum(tr_values[1:period + 1])
    smoothed_plus = sum(plus_dm[1:period + 1])
    smoothed_minus = sum(minus_dm[1:period + 1])

    dx_values = [None] * count

    for index in range(period, count):
        if index > period:
            smoothed_tr = smoothed_tr - smoothed_tr / period + tr_values[index]
            smoothed_plus = (
                smoothed_plus - smoothed_plus / period + plus_dm[index]
            )
            smoothed_minus = (
                smoothed_minus - smoothed_minus / period + minus_dm[index]
            )

        if smoothed_tr <= 0:
            continue

        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denominator = plus_di + minus_di

        if denominator > 0:
            dx_values[index] = (
                100.0 * abs(plus_di - minus_di) / denominator
            )

    first_dx_index = period
    valid_dx = [
        value for value in dx_values[first_dx_index:first_dx_index + period]
        if value is not None
    ]

    if len(valid_dx) < period:
        return result

    first_adx_index = first_dx_index + period - 1
    previous_adx = sum(valid_dx) / period
    result[first_adx_index] = previous_adx

    for index in range(first_adx_index + 1, count):
        value = dx_values[index]
        if value is None:
            continue
        previous_adx = (
            previous_adx * (period - 1) + value
        ) / period
        result[index] = previous_adx

    return result


def macd(values, fast_period=12, slow_period=26, signal_period=9):
    fast_values = ema(values, fast_period)
    slow_values = ema(values, slow_period)

    macd_line = []
    for fast_value, slow_value in zip(fast_values, slow_values):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
        else:
            macd_line.append(fast_value - slow_value)

    valid_start = next(
        (i for i, value in enumerate(macd_line) if value is not None),
        len(values),
    )

    valid_macd = [
        value for value in macd_line[valid_start:]
        if value is not None
    ]

    signal_values_valid = ema(valid_macd, signal_period)
    signal_line = [None] * len(values)

    for offset, value in enumerate(signal_values_valid):
        index = valid_start + offset
        if index < len(signal_line):
            signal_line[index] = value

    histogram = []
    for line_value, signal_value in zip(macd_line, signal_line):
        if line_value is None or signal_value is None:
            histogram.append(None)
        else:
            histogram.append(line_value - signal_value)

    return macd_line, signal_line, histogram


def analyze(candles):
    closes = [c["c"] for c in candles]
    volumes = [c["v"] for c in candles]

    ema20_values = ema(closes, 20)
    ema50_values = ema(closes, 50)
    ema100_values = ema(closes, 100)
    rsi_values = rsi(closes, 14)
    atr_values = atr(candles, 14)
    adx_values = adx(candles, 14)
    macd_line, macd_signal, macd_hist = macd(closes)

    index = len(candles) - 1
    close = closes[index]

    def latest(series, fallback=0.0):
        value = series[index] if index < len(series) else None
        return fallback if value is None else float(value)

    ema20 = latest(ema20_values, close)
    ema50 = latest(ema50_values, close)
    ema100 = latest(ema100_values, close)
    rsi_value = latest(rsi_values, 50.0)
    atr_value = latest(atr_values, close * 0.002)
    adx_value = latest(adx_values, 0.0)
    macd_value = latest(macd_line, 0.0)
    macd_signal_value = latest(macd_signal, 0.0)
    macd_hist_value = latest(macd_hist, 0.0)

    previous_close = closes[max(0, index - 4)]
    momentum = (
        (close / previous_close - 1.0) * 100.0
        if previous_close else 0.0
    )

    average_volume = (
        sum(volumes[max(0, index - 19):index]) /
        max(1, len(volumes[max(0, index - 19):index]))
    )
    volume_ratio = (
        volumes[index] / average_volume if average_volume > 0 else 0.0
    )

    if ema20 > ema50 > ema100:
        trend = "UP"
    elif ema20 < ema50 < ema100:
        trend = "DOWN"
    else:
        trend = "SIDEWAYS"

    return {
        "price": close,
        "candle_timestamp": candles[index]["t"],
        "candle_close_timestamp": candles[index]["T"],
        "ema20": ema20,
        "ema50": ema50,
        "ema100": ema100,
        "rsi": rsi_value,
        "atr": atr_value,
        "adx": adx_value,
        "macd": macd_value,
        "macd_signal": macd_signal_value,
        "macd_hist": macd_hist_value,
        "momentum_pct": momentum,
        "volume_ratio": volume_ratio,
        "trend": trend,
    }


# ============================================================
# STATE MIGRATION / PERSISTENCE
# ============================================================

def normalize_position(position):
    if not isinstance(position, dict):
        return None

    normalized = dict(position)

    direction = str(
        normalized.get("direction", normalized.get("side", ""))
    ).upper()

    if direction in ("BUY", "LONG"):
        direction = "LONG"
    elif direction in ("SELL", "SHORT"):
        direction = "SHORT"
    else:
        old_sign = safe_float(normalized.get("sign"), 0.0)
        if old_sign == 0:
            return None
        direction = "LONG" if old_sign > 0 else "SHORT"

    normalized["direction"] = direction
    normalized["sign"] = sign_for_direction(direction)

    normalized["entry_price"] = safe_float(
        normalized.get("entry_price", normalized.get("entry", 0))
    )
    normalized["quantity"] = safe_float(
        normalized.get("quantity", normalized.get("qty", 0))
    )
    normalized["leverage"] = safe_float(
        normalized.get("leverage"), LEVERAGE
    )
    normalized["entry_fee"] = safe_float(normalized.get("entry_fee"))
    normalized["funding_paid"] = safe_float(
        normalized.get("funding_paid")
    )
    normalized["margin_used"] = safe_float(
        normalized.get("margin_used")
    )
    normalized["risk_distance"] = safe_float(
        normalized.get("risk_distance")
    )
    normalized["stop_price"] = safe_float(
        normalized.get("stop_price")
    )
    normalized["target_price"] = safe_float(
        normalized.get("target_price")
    )
    normalized["opened_ts"] = safe_float(
        normalized.get("opened_ts"),
        safe_float(normalized.get("opened_timestamp"), now_ts()),
    )
    normalized["opened_candle_ts"] = safe_int(
        normalized.get("opened_candle_ts")
    )
    normalized["last_funding_ts"] = safe_float(
        normalized.get("last_funding_ts"),
        normalized["opened_ts"],
    )
    normalized["last_checked_candle_ts"] = safe_int(
        normalized.get("last_checked_candle_ts"),
        normalized["opened_candle_ts"],
    )

    if (
        normalized["entry_price"] <= 0
        or normalized["quantity"] <= 0
    ):
        return None

    return normalized


def load_state():
    if not STATE_FILE.exists():
        return {
            "build": BUILD,
            "initial_equity": INITIAL_EQUITY,
            "equity": INITIAL_EQUITY,
            "peak_equity": INITIAL_EQUITY,
            "max_drawdown_pct": 0.0,
            "position": None,
            "trades": [],
            "equity_history": [],
            "funding_total": 0.0,
            "consecutive_losses": 0,
            "last_trade_ts": 0.0,
            "day_key": "",
            "day_start_equity": INITIAL_EQUITY,
            "last_processed_15m_ts": 0,
        }

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("State file root is not an object")
    except Exception as exc:
        backup = STATE_FILE.with_name("state.corrupt.json")
        try:
            backup.write_text(
                STATE_FILE.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        except Exception:
            pass
        raise RuntimeError(
            f"Cannot safely load state.json; backup attempted: {exc}"
        )

    state.setdefault("initial_equity", INITIAL_EQUITY)
    state.setdefault("equity", state["initial_equity"])
    state.setdefault("peak_equity", state["equity"])
    state.setdefault("max_drawdown_pct", 0.0)
    state.setdefault("funding_total", 0.0)
    state.setdefault("consecutive_losses", 0)
    state.setdefault("last_trade_ts", 0.0)
    state.setdefault("day_key", "")
    state.setdefault("day_start_equity", state["equity"])
    state.setdefault("last_processed_15m_ts", 0)
    state.setdefault("equity_history", [])

    if "trades" not in state:
        state["trades"] = state.get("paper_trades", [])
    if not isinstance(state["trades"], list):
        state["trades"] = []

    old_position = state.get("position")
    if old_position is None:
        old_position = state.get("open_position")
    state["position"] = normalize_position(old_position)

    state["equity"] = safe_float(
        state.get("equity"), INITIAL_EQUITY
    )
    state["initial_equity"] = safe_float(
        state.get("initial_equity"), INITIAL_EQUITY
    )
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity"), state["equity"]),
        state["equity"],
    )

    state["trades"] = state["trades"][-MAX_TRADES_STORED:]
    state["equity_history"] = (
        state["equity_history"][-MAX_HISTORY_STORED:]
    )

    state["build"] = BUILD
    return state


# ============================================================
# PNL / FUNDING / POSITION MANAGEMENT
# ============================================================

def estimated_close_fee(position, reference_price):
    notional = abs(
        safe_float(reference_price) * safe_float(position.get("quantity"))
    )
    return notional * FEE_RATE


def mark_to_market(position, price):
    if not position:
        return 0.0

    sign = safe_float(position.get("sign"), 1.0)
    entry = safe_float(position.get("entry_price"))
    quantity = safe_float(position.get("quantity"))

    unrealized_gross = (price - entry) * quantity * sign
    close_fee = estimated_close_fee(position, price)

    # Entry fee and funding have already affected state["equity"].
    return unrealized_gross - close_fee


def marked_equity(state, price):
    position = state.get("position")
    return safe_float(state.get("equity")) + mark_to_market(position, price)


def charge_funding_until(state, rate, end_ts):
    """
    Estimate funding using the latest observed rate for elapsed whole hours.
    This does not fetch historical funding rates and must be treated as an
    approximation.
    """
    position = state.get("position")
    if not position:
        return 0.0

    last_ts = safe_float(position.get("last_funding_ts"), end_ts)
    end_ts = safe_float(end_ts, last_ts)

    elapsed = end_ts - last_ts
    if elapsed < FUNDING_INTERVAL_SECONDS:
        return 0.0

    intervals = int(elapsed // FUNDING_INTERVAL_SECONDS)
    if intervals <= 0:
        return 0.0

    sign = safe_float(position.get("sign"), 1.0)
    notional = (
        safe_float(position.get("entry_price"))
        * safe_float(position.get("quantity"))
    )

    # Positive funding rate: longs pay, shorts receive.
    payment = -sign * safe_float(rate) * notional * intervals

    state["equity"] = safe_float(state.get("equity")) + payment
    state["funding_total"] = (
        safe_float(state.get("funding_total")) + payment
    )
    position["funding_paid"] = (
        safe_float(position.get("funding_paid")) + payment
    )
    position["last_funding_ts"] = last_ts + (
        intervals * FUNDING_INTERVAL_SECONDS
    )

    return payment


def close_position(state, reference_price, reason, close_ts=None):
    position = state.get("position")
    if not position:
        return None

    sign = safe_float(position.get("sign"), 1.0)
    quantity = safe_float(position.get("quantity"))
    entry_price = safe_float(position.get("entry_price"))
    reference_price = safe_float(reference_price)

    if reference_price <= 0 or quantity <= 0:
        raise RuntimeError("Invalid close price or position quantity")

    # Adverse slippage on exit.
    exit_fill = reference_price * (1.0 - SLIPPAGE_RATE * sign)

    gross_pnl = (exit_fill - entry_price) * quantity * sign
    exit_notional = abs(exit_fill * quantity)
    exit_fee = exit_notional * FEE_RATE
    entry_fee = safe_float(position.get("entry_fee"))
    funding_paid = safe_float(position.get("funding_paid"))

    # Entry fee and funding were already reflected in account equity.
    # Therefore, closing only adds gross PnL minus exit fee to equity.
    state["equity"] = (
        safe_float(state.get("equity")) + gross_pnl - exit_fee
    )

    net_trade_pnl = (
        gross_pnl - entry_fee - exit_fee + funding_paid
    )

    closed_at = safe_float(close_ts, now_ts())
    trade = {
        "id": f"{int(closed_at)}-{len(state.get('trades', [])) + 1}",
        "direction": position.get("direction", "UNKNOWN"),
        "entry_price": entry_price,
        "entry_fill": entry_price,
        "exit_price": exit_fill,
        "exit_reference_price": reference_price,
        "quantity": quantity,
        "leverage": safe_float(position.get("leverage"), LEVERAGE),
        "entry_notional": entry_price * quantity,
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": entry_fee + exit_fee,
        "funding_paid": funding_paid,
        "net_pnl": net_trade_pnl,
        "reason": str(reason),
        "opened_at": iso_time(position.get("opened_ts")),
        "closed_at": iso_time(closed_at),
        "opened_ts": safe_float(position.get("opened_ts")),
        "closed_ts": closed_at,
        "risk_distance": safe_float(position.get("risk_distance")),
    }

    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-MAX_TRADES_STORED:]
    state["last_trade_ts"] = closed_at

    if net_trade_pnl < 0:
        state["consecutive_losses"] = (
            safe_int(state.get("consecutive_losses")) + 1
        )
    elif net_trade_pnl > 0:
        state["consecutive_losses"] = 0

    state["position"] = None
    return trade


def process_position_candles(state, candles, funding_rate):
    """
    Process each unprocessed, fully closed 15m candle in order.
    When a candle touches both stop and target, stop is assumed to occur first.
    """
    position = state.get("position")
    if not position:
        return []

    processed_trades = []
    opened_candle_ts = safe_int(position.get("opened_candle_ts"))
    last_checked = safe_int(
        position.get("last_checked_candle_ts"), opened_candle_ts
    )

    pending = [
        candle for candle in candles
        if candle["t"] > max(opened_candle_ts, last_checked)
    ]

    for candle in pending:
        position = state.get("position")
        if not position:
            break

        candle_ts = candle["T"] / 1000.0
        sign = safe_float(position.get("sign"), 1.0)
        stop_price = safe_float(position.get("stop_price"))
        target_price = safe_float(position.get("target_price"))
        high = candle["h"]
        low = candle["l"]

        stop_hit = False
        target_hit = False

        if sign > 0:
            stop_hit = stop_price > 0 and low <= stop_price
            target_hit = target_price > 0 and high >= target_price
        else:
            stop_hit = stop_price > 0 and high >= stop_price
            target_hit = target_price > 0 and low <= target_price

        # Conservative assumption if both were reached inside one candle.
        if stop_hit:
            charge_funding_until(state, funding_rate, candle_ts)
            reason = (
                "TRAIL_STOP"
                if position.get("trailing_active")
                else "STOP_LOSS"
            )
            trade = close_position(
                state, stop_price, reason, close_ts=candle_ts
            )
            if trade:
                processed_trades.append(trade)
            break

        if target_hit:
            charge_funding_until(state, funding_rate, candle_ts)
            trade = close_position(
                state, target_price, "TAKE_PROFIT", close_ts=candle_ts
            )
            if trade:
                processed_trades.append(trade)
            break

        # Only update the trailing stop AFTER checking this candle's range.
        entry = safe_float(position.get("entry_price"))
        risk_distance = safe_float(position.get("risk_distance"))

        if risk_distance > 0:
            favorable_move = (candle["c"] - entry) * sign
            if favorable_move >= 1.5 * risk_distance:
                candidate_stop = (
                    candle["c"] - 1.35 * risk_distance
                    if sign > 0
                    else candle["c"] + 1.35 * risk_distance
                )

                old_stop = safe_float(position.get("stop_price"))
                if sign > 0:
                    new_stop = max(old_stop, candidate_stop)
                else:
                    new_stop = (
                        min(old_stop, candidate_stop)
                        if old_stop > 0 else candidate_stop
                    )

                position["stop_price"] = new_stop
                position["trailing_active"] = True

        position["last_checked_candle_ts"] = candle["t"]

        # Time stop based on actual elapsed wall-clock time.
        if now_ts() - safe_float(position.get("opened_ts")) >= MAX_HOLD_SECONDS:
            charge_funding_until(state, funding_rate, candle_ts)
            trade = close_position(
                state, candle["c"], "MAX_HOLD", close_ts=candle_ts
            )
            if trade:
                processed_trades.append(trade)
            break

    return processed_trades


# ============================================================
# SIGNAL / RISK MANAGEMENT
# ============================================================

def calc_scores(market, funding_rate):
    long_score = 0
    short_score = 0
    reasons = []

    for timeframe in ("15m", "1h", "4h"):
        trend = market[timeframe]["trend"]
        if trend == "UP":
            long_score += 2
        elif trend == "DOWN":
            short_score += 2

    fifteen = market["15m"]
    rsi_value = fifteen["rsi"]
    hist = fifteen["macd_hist"]
    momentum = fifteen["momentum_pct"]
    volume_ratio = fifteen["volume_ratio"]

    if hist > 0:
        long_score += 1
    elif hist < 0:
        short_score += 1

    if momentum > 0:
        long_score += 1
    elif momentum < 0:
        short_score += 1

    if 50 <= rsi_value <= 68:
        long_score += 1
    if 32 <= rsi_value <= 50:
        short_score += 1

    if volume_ratio >= 1.2:
        if momentum > 0:
            long_score += 1
        elif momentum < 0:
            short_score += 1

    # Funding is a small contrarian input, not a standalone trade signal.
    if funding_rate >= 0.0003:
        short_score += 1
    elif funding_rate <= -0.0003:
        long_score += 1

    direction = "WAIT"
    if long_score >= 5 and long_score >= short_score + 2:
        direction = "LONG"
    elif short_score >= 5 and short_score >= long_score + 2:
        direction = "SHORT"

    adx_value = fifteen["adx"]
    runner_mode = (
        market["1h"]["trend"] == market["4h"]["trend"]
        and market["1h"]["trend"] in ("UP", "DOWN")
        and adx_value >= 22
    )

    reasons.append(
        f"15m {fifteen['trend']} RSI {rsi_value:.1f}; "
        f"ADX {adx_value:.1f}; "
        f"1h {market['1h']['trend']}, 4h {market['4h']['trend']}"
    )

    return {
        "direction": direction,
        "score": max(long_score, short_score),
        "long_score": long_score,
        "short_score": short_score,
        "reason": "; ".join(reasons),
        "runner_mode": runner_mode,
    }


def open_position(state, direction, market, latest_candle):
    price = safe_float(market["15m"]["price"])
    atr_value = safe_float(market["15m"]["atr"])

    if price <= 0 or atr_value <= 0:
        return False

    sign = sign_for_direction(direction)
    marked = marked_equity(state, price)
    if marked <= 0:
        return False

    # Stop distance is bounded to avoid absurd ATR-driven stop sizes.
    risk_distance = min(
        max(atr_value * 1.6, price * 0.0015),
        price * 0.012,
    )

    risk_cash = marked * RISK_PER_TRADE

    # Approximate per-unit loss at stop, including fees and adverse slippage.
    estimated_round_trip_cost_per_unit = (
        price * (2.0 * FEE_RATE + 2.0 * SLIPPAGE_RATE)
    )
    risk_per_unit = risk_distance + estimated_round_trip_cost_per_unit

    if risk_per_unit <= 0:
        return False

    qty_by_risk = risk_cash / risk_per_unit
    margin_cap = marked * MAX_MARGIN_FRACTION
    qty_by_margin = margin_cap * LEVERAGE / price
    quantity = min(qty_by_risk, qty_by_margin)

    if quantity <= 0 or not math.isfinite(quantity):
        return False

    # Adverse entry slippage.
    entry_fill = price * (1.0 + SLIPPAGE_RATE * sign)
    notional = entry_fill * quantity
    margin_used = notional / LEVERAGE

    if margin_used > margin_cap * 1.000001:
        return False

    entry_fee = notional * FEE_RATE
    if safe_float(state.get("equity")) <= entry_fee:
        return False

    # Entry fee is deducted immediately from account equity.
    state["equity"] = safe_float(state["equity"]) - entry_fee

    runner_mode = bool(market["signal"].get("runner_mode"))
    stop_price = (
        entry_fill - risk_distance
        if sign > 0 else entry_fill + risk_distance
    )
    target_price = 0.0 if runner_mode else (
        entry_fill + 2.4 * risk_distance
        if sign > 0 else entry_fill - 2.4 * risk_distance
    )

    current_ts = now_ts()

    state["position"] = {
        "direction": direction,
        "sign": sign,
        "entry_price": entry_fill,
        "reference_entry_price": price,
        "quantity": quantity,
        "leverage": LEVERAGE,
        "margin_used": margin_used,
        "notional": notional,
        "entry_fee": entry_fee,
        "funding_paid": 0.0,
        "opened_ts": current_ts,
        "opened_at": iso_time(current_ts),
        "opened_candle_ts": safe_int(latest_candle["t"]),
        "last_checked_candle_ts": safe_int(latest_candle["t"]),
        "last_funding_ts": current_ts,
        "risk_distance": risk_distance,
        "stop_price": stop_price,
        "target_price": target_price,
        "trailing_active": False,
        "runner_mode": runner_mode,
        "entry_signal_score": safe_int(
            market["signal"].get("score")
        ),
    }

    return True


def update_drawdown(state, current_equity):
    peak = max(
        safe_float(state.get("peak_equity"), current_equity),
        current_equity,
    )
    state["peak_equity"] = peak

    if peak > 0:
        current_dd = max(0.0, (peak - current_equity) / peak * 100.0)
    else:
        current_dd = 0.0

    historical_dd = max(
        safe_float(state.get("max_drawdown_pct")),
        current_dd,
    )
    state["max_drawdown_pct"] = historical_dd

    return current_dd, historical_dd


def update_daily_baseline(state, current_equity):
    day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if state.get("day_key") != day_key:
        state["day_key"] = day_key
        state["day_start_equity"] = current_equity

    day_start = safe_float(
        state.get("day_start_equity"), current_equity
    )
    if day_start > 0:
        daily_loss_pct = max(
            0.0,
            (day_start - current_equity) / day_start,
        )
    else:
        daily_loss_pct = 0.0

    return daily_loss_pct


def calculate_metrics(state, current_marked_equity):
    trades = state.get("trades", [])

    net_values = [
        safe_float(trade.get("net_pnl"))
        for trade in trades
        if isinstance(trade, dict) and "net_pnl" in trade
    ]

    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]

    # True gross trade PnL before fees/funding.
    gross_values = [
        safe_float(trade.get("gross_pnl"))
        for trade in trades
        if isinstance(trade, dict) and "gross_pnl" in trade
    ]
    gross_wins = [value for value in gross_values if value > 0]
    gross_losses = [value for value in gross_values if value < 0]

    total_closed_net = sum(net_values)
    total_gross_profit = sum(gross_wins)
    total_gross_loss = abs(sum(gross_losses))

    profit_factor = (
        total_gross_profit / total_gross_loss
        if total_gross_loss > 0 else None
    )

    initial = safe_float(state.get("initial_equity"), INITIAL_EQUITY)
    account_net_pnl = current_marked_equity - initial

    return {
        "trades": len(trades),
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (
            len(wins) / len(net_values) * 100.0
            if net_values else 0.0
        ),
        "gross_profit": total_gross_profit,
        "gross_loss": total_gross_loss,
        "profit_factor": profit_factor,
        "closed_trades_net_pnl": total_closed_net,
        "net_pnl": account_net_pnl,
        "account_net_pnl": account_net_pnl,
        "return_pct": (
            account_net_pnl / initial * 100.0 if initial else 0.0
        ),
        "current_drawdown_pct": safe_float(
            state.get("current_drawdown_pct")
        ),
        "max_drawdown_pct": safe_float(
            state.get("max_drawdown_pct")
        ),
        "consecutive_losses": safe_int(
            state.get("consecutive_losses")
        ),
        "funding_total": safe_float(state.get("funding_total")),
    }


# ============================================================
# DASHBOARD OUTPUT
# ============================================================

def build_payload(state, market, funding_rate, current_marked_equity,
                  current_drawdown_pct, daily_loss_pct, events):
    position = state.get("position")
    initial = safe_float(state.get("initial_equity"), INITIAL_EQUITY)
    account_equity = safe_float(state.get("equity"))

    if position:
        unrealized_pnl = mark_to_market(
            position, market["15m"]["price"]
        )
        position_view = dict(position)
        position_view["unrealized_pnl"] = unrealized_pnl
    else:
        unrealized_pnl = 0.0
        position_view = None

    metrics = calculate_metrics(state, current_marked_equity)

    # Compatibility fields for existing dashboard code.
    payload = {
        "build": BUILD,
        "generated_at": iso_time(),
        "paper_only": True,
        "live_trading": False,
        "wallet_connected": False,
        "coin": COIN,
        "price": market["15m"]["price"],
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate * 100.0,
        "initial_equity": initial,
        "equity": account_equity,
        "marked_equity": current_marked_equity,
        "realized_pnl": account_equity - initial,
        "unrealized_pnl": unrealized_pnl,
        "net_pnl": current_marked_equity - initial,
        "return_pct": (
            (current_marked_equity - initial) / initial * 100.0
            if initial else 0.0
        ),
        "leverage": LEVERAGE,
        "position": position_view,
        "signal": market["signal"],
        "market": market,
        "metrics": metrics,
        "trades": state.get("trades", [])[-MAX_TRADES_STORED:],
        "paper_trades": state.get("trades", [])[-MAX_TRADES_STORED:],
        "equity_history": state.get("equity_history", [])[-MAX_HISTORY_STORED:],
        "funding_total": safe_float(state.get("funding_total")),
        "current_drawdown_pct": current_drawdown_pct,
        "max_drawdown_pct": safe_float(state.get("max_drawdown_pct")),
        "daily_loss_pct": daily_loss_pct * 100.0,
        "consecutive_losses": safe_int(state.get("consecutive_losses")),
        "events": events[-100:],
        "risk": {
            "risk_per_trade_pct": RISK_PER_TRADE * 100.0,
            "max_margin_fraction_pct": MAX_MARGIN_FRACTION * 100.0,
            "max_daily_loss_pct": MAX_DAILY_LOSS * 100.0,
            "max_drawdown_limit_pct": MAX_DRAWDOWN * 100.0,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "max_hold_hours": MAX_HOLD_SECONDS / 3600.0,
        },
        "notes": [
            "Paper trading only; no live orders or wallet connection.",
            "Funding uses the latest observed rate as an approximation.",
            "When one candle touches both stop and target, stop is assumed first.",
            "Historical maximum drawdown is tracked from V11 onward; older history may be incomplete.",
        ],
    }

    return payload


def append_history(state, price, marked, event):
    history = state.setdefault("equity_history", [])
    history.append({
        "time": iso_time(),
        "timestamp": int(now_ts()),
        "price": safe_float(price),
        "equity": safe_float(state.get("equity")),
        "marked_equity": safe_float(marked),
        "event": str(event),
    })
    state["equity_history"] = history[-MAX_HISTORY_STORED:]


# ============================================================
# MAIN
# ============================================================

def main():
    started = now_ts()
    state = load_state()
    events = []

    candles_15m = fetch_candles("15m")
    candles_1h = fetch_candles("1h")
    candles_4h = fetch_candles("4h")
    funding_rate = fetch_funding_rate()

    market = {
        "15m": analyze(candles_15m),
        "1h": analyze(candles_1h),
        "4h": analyze(candles_4h),
    }

    market["signal"] = calc_scores(market, funding_rate)
    price = market["15m"]["price"]
    latest_candle = candles_15m[-1]
    now = now_ts()

    # Process all unprocessed closed candles for an existing position.
    old_position = state.get("position")
    if old_position:
        # Migration safety: if no candle cursor exists in a legacy position,
        # begin at the latest candle rather than replaying unknown old candles.
        if not safe_int(old_position.get("last_checked_candle_ts")):
            old_position["last_checked_candle_ts"] = safe_int(
                latest_candle["t"]
            )
            old_position["opened_candle_ts"] = safe_int(
                old_position.get("opened_candle_ts"),
                safe_int(latest_candle["t"]),
            )
            events.append("LEGACY_POSITION_CURSOR_INITIALIZED")

        closed = process_position_candles(
            state, candles_15m, funding_rate
        )
        for trade in closed:
            events.append(f"CLOSED_{trade['reason']}")

    # If position remains open, estimate funding through current run time.
    if state.get("position"):
        funding_payment = charge_funding_until(
            state, funding_rate, now
        )
        if funding_payment:
            events.append("FUNDING_ESTIMATE_APPLIED")

    current_marked = marked_equity(state, price)
    current_drawdown_pct, historical_drawdown_pct = update_drawdown(
        state, current_marked
    )
    daily_loss_pct = update_daily_baseline(state, current_marked)

    # Close on opposite signal only after the position's candle processing.
    position = state.get("position")
    signal_direction = market["signal"]["direction"]

    if position:
        if (
            signal_direction in ("LONG", "SHORT")
            and signal_direction != position.get("direction")
        ):
            trade = close_position(
                state, price, "OPPOSITE_SIGNAL", close_ts=now
            )
            if trade:
                events.append("CLOSED_OPPOSITE_SIGNAL")

    current_marked = marked_equity(state, price)
    current_drawdown_pct, historical_drawdown_pct = update_drawdown(
        state, current_marked
    )
    daily_loss_pct = update_daily_baseline(state, current_marked)

    # Entry guards. Existing positions are still managed even if a guard trips.
    if not state.get("position"):
        entry_block_reason = None

        if daily_loss_pct >= MAX_DAILY_LOSS:
            entry_block_reason = "DAILY_LOSS_LIMIT"
        elif current_drawdown_pct / 100.0 >= MAX_DRAWDOWN:
            entry_block_reason = "DRAWDOWN_LIMIT"
        elif safe_int(state.get("consecutive_losses")) >= MAX_CONSECUTIVE_LOSSES:
            entry_block_reason = "CONSECUTIVE_LOSS_LIMIT"
        elif now - safe_float(state.get("last_trade_ts")) < COOLDOWN_SECONDS:
            entry_block_reason = "COOLDOWN"

        if entry_block_reason:
            events.append(f"ENTRY_BLOCKED_{entry_block_reason}")
        elif signal_direction in ("LONG", "SHORT"):
            if open_position(
                state, signal_direction, market, latest_candle
            ):
                events.append(f"OPENED_{signal_direction}")

    # Record the latest closed candle cursor for the next scheduled run.
    state["last_processed_15m_ts"] = safe_int(latest_candle["t"])

    current_marked = marked_equity(state, price)
    current_drawdown_pct, historical_drawdown_pct = update_drawdown(
        state, current_marked
    )
    daily_loss_pct = update_daily_baseline(state, current_marked)

    append_history(
        state,
        price,
        current_marked,
        events[-1] if events else "MARKET_UPDATE",
    )

    state["last_run_at"] = iso_time()
    state["last_run_duration_seconds"] = max(0.0, now_ts() - started)
    state["last_price"] = price
    state["last_signal"] = market["signal"]["direction"]
    state["last_signal_score"] = market["signal"]["score"]
    state["current_drawdown_pct"] = current_drawdown_pct
    state["max_drawdown_pct"] = historical_drawdown_pct
    state["build"] = BUILD

    payload = build_payload(
        state,
        market,
        funding_rate,
        current_marked,
        current_drawdown_pct,
        daily_loss_pct,
        events,
    )

    # Persist state first, then publish the dashboard data.
    json_write_atomic(STATE_FILE, state)
    json_write_atomic(DATA_FILE, payload)

    print(json.dumps({
        "build": BUILD,
        "price": round(price, 4),
        "leverage": LEVERAGE,
        "signal": market["signal"]["direction"],
        "signal_score": market["signal"]["score"],
        "equity": round(safe_float(state["equity"]), 8),
        "marked_equity": round(current_marked, 8),
        "net_pnl": round(current_marked - INITIAL_EQUITY, 8),
        "closed_trades": len(state.get("trades", [])),
        "position": (
            state["position"]["direction"]
            if state.get("position") else None
        ),
        "current_drawdown_pct": round(current_drawdown_pct, 6),
        "max_drawdown_pct": round(historical_drawdown_pct, 6),
        "events": events,
        "paper_only": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()