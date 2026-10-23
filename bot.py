
#!/usr/bin/env python3
"""
BTC Paper Engine V14
Paper trading only. No wallet, private key, or live order functionality.

V14 changes:
- Planned risk per trade: 1.0% of equity
- Minimum margin threshold: 5% of equity
- Maximum margin: 50% of equity
- Keeps existing account equity and trade history where possible
- Preserves dashboard-compatible data keys
- Uses public Hyperliquid market data only
"""

import json
import math
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, date
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================

BUILD = "BTC_PAPER_V14_2026-10-23"

API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DOCS_DIR = ROOT / "docs"
DATA_FILE = DOCS_DIR / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0

# V14 risk and margin settings
RISK_PER_TRADE = 0.010
MIN_MARGIN_FRACTION = 0.05
MAX_MARGIN_FRACTION = 0.50

DAILY_LOSS_LIMIT = 0.03
MAX_DRAWDOWN_LIMIT = 0.10
MAX_CONSECUTIVE_LOSSES = 4

COOLDOWN_SECONDS = 60 * 60
MAX_HOLD_SECONDS = 24 * 60 * 60

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

MIN_SIGNAL_SCORE = 6
MIN_REWARD_RISK = 1.5

ATR_STOP_MULTIPLIER = 1.8
ATR_TARGET_MULTIPLIER = 3.0
TRAIL_ACTIVATION_R = 1.5
ATR_TRAIL_MULTIPLIER = 1.5

CANDLE_LIMIT = 200
HTTP_TIMEOUT = 20


# ============================================================
# GENERAL HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def unix_now():
    return int(time.time())


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass
    return float(default)


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def read_json(path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                result = json.load(f)
                if isinstance(result, type(default)):
                    return result
    except (OSError, json.JSONDecodeError):
        pass
    return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    temp_path.replace(path)


def clamp(value, low, high):
    return max(low, min(high, value))


def pct(value):
    return round(safe_float(value) * 100.0, 6)


def http_post(payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V14/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


# ============================================================
# HYPERLIQUID PUBLIC MARKET DATA
# ============================================================

def fetch_candles(interval, limit=CANDLE_LIMIT):
    end_ms = int(time.time() * 1000)
    interval_ms = {
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
    }[interval]

    start_ms = end_ms - interval_ms * (limit + 5)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }

    raw = http_post(payload)
    candles = []

    if not isinstance(raw, list):
        raise RuntimeError(f"Unexpected candle response for {interval}")

    for item in raw:
        try:
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
            if candle["t"] > 0 and candle["c"] > 0:
                candles.append(candle)
        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda c: c["t"])

    # Ignore the candle that is still being formed.
    now_ms = int(time.time() * 1000)
    closed = [c for c in candles if c["t"] + interval_ms <= now_ms]

    if len(closed) < 35:
        raise RuntimeError(f"Not enough closed {interval} candles")

    return closed[-limit:]


def fetch_market_data():
    mids = http_post({"type": "allMids"})
    if not isinstance(mids, dict) or COIN not in mids:
        raise RuntimeError("BTC mid price was not available")

    price = safe_float(mids[COIN])
    if price <= 0:
        raise RuntimeError("Invalid BTC price")

    funding_rate = 0.0
    try:
        meta = http_post({"type": "metaAndAssetCtxs"})
        if isinstance(meta, list) and len(meta) > 1:
            universe = meta[0].get("universe", [])
            contexts = meta[1]
            for index, asset in enumerate(universe):
                if asset.get("name") == COIN and index < len(contexts):
                    funding_rate = safe_float(
                        contexts[index].get("funding", 0.0)
                    )
                    break
    except Exception as exc:
        print(f"Funding data unavailable; using 0 for this run: {exc}")

    return price, funding_rate


# ============================================================
# INDICATORS
# ============================================================

def closes(candles):
    return [safe_float(c["c"]) for c in candles]


def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return max(candles[-1]["c"] * 0.002, 1.0)

    true_ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["c"]
        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous_close),
            abs(current["l"] - previous_close),
        ))

    if len(true_ranges) < period:
        return max(true_ranges) if true_ranges else 1.0

    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return max(value, 0.01)


def trend_label(candles):
    values = closes(candles)
    fast = ema(values, 9)
    slow = ema(values, 21)

    if fast[-1] > slow[-1] * 1.001:
        return "UP"
    if fast[-1] < slow[-1] * 0.999:
        return "DOWN"
    return "SIDEWAYS"


def build_signal(c15, c1h, c4h, funding_rate):
    values = closes(c15)
    current_price = values[-1]
    ema9 = ema(values, 9)
    ema21 = ema(values, 21)
    rsi_value = rsi(values, 14)
    atr_value = atr(c15)

    trend15 = trend_label(c15)
    trend1h = trend_label(c1h)
    trend4h = trend_label(c4h)

    long_score = 0
    short_score = 0
    reasons = []

    if ema9[-1] > ema21[-1]:
        long_score += 2
    elif ema9[-1] < ema21[-1]:
        short_score += 2

    if trend15 == "UP":
        long_score += 1
    elif trend15 == "DOWN":
        short_score += 1

    if trend1h == "UP":
        long_score += 2
    elif trend1h == "DOWN":
        short_score += 2

    if trend4h == "UP":
        long_score += 2
    elif trend4h == "DOWN":
        short_score += 2

    if 45 <= rsi_value <= 68:
        long_score += 1
    if 32 <= rsi_value <= 55:
        short_score += 1

    if rsi_value > 75:
        long_score -= 1
    if rsi_value < 25:
        short_score -= 1

    # Extreme funding is a small counter-bias, not a standalone signal.
    if funding_rate > 0.0003:
        short_score += 1
    elif funding_rate < -0.0003:
        long_score += 1

    direction = "WAIT"
    score = max(long_score, short_score)

    if long_score >= MIN_SIGNAL_SCORE and long_score > short_score:
        direction = "LONG"
        reasons.append("Long score leads")
    elif short_score >= MIN_SIGNAL_SCORE and short_score > long_score:
        direction = "SHORT"
        reasons.append("Short score leads")
    else:
        reasons.append("Signal threshold or directional agreement not met")

    return {
        "direction": direction,
        "score": int(score),
        "long_score": int(long_score),
        "short_score": int(short_score),
        "rsi": round(rsi_value, 4),
        "atr": round(atr_value, 6),
        "ema9": round(ema9[-1], 6),
        "ema21": round(ema21[-1], 6),
        "trend_15m": trend15,
        "trend_1h": trend1h,
        "trend_4h": trend4h,
        "reasons": reasons,
        "timestamp": iso_now(),
        "reference_price": round(current_price, 6),
    }


# ============================================================
# STATE AND ACCOUNTING
# ============================================================

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
        "daily_date": str(utc_now().date()),
        "daily_start_equity": INITIAL_EQUITY,
        "daily_loss_lock": False,
        "drawdown_lock": False,
        "last_error": None,
    }


def load_state():
    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict) or not state:
        return default_state()

    # Preserve old account and trade history; add missing V14 fields.
    defaults = default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = value

    state["initial_equity"] = safe_float(
        state.get("initial_equity"), INITIAL_EQUITY
    )
    state["equity"] = safe_float(state.get("equity"), INITIAL_EQUITY)
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity"), state["equity"]),
        state["equity"],
    )
    state["realized_pnl"] = safe_float(state.get("realized_pnl"))
    state["funding_total"] = safe_float(state.get("funding_total"))
    state["trades"] = state.get("trades") or state.get("paper_trades") or []
    state["equity_history"] = state.get("equity_history") or []
    state["position"] = state.get("position") or state.get("open_position")
    state["last_close_ts"] = safe_int(state.get("last_close_ts"))
    state["last_candle_ts"] = safe_int(state.get("last_candle_ts"))
    state["consecutive_losses"] = safe_int(
        state.get("consecutive_losses")
    )

    # Do not reset equity or history merely because the build changed.
    state["build"] = BUILD
    return state


def current_drawdown(state, marked_equity=None):
    equity = (
        safe_float(marked_equity)
        if marked_equity is not None
        else safe_float(state.get("equity"))
    )
    peak = max(safe_float(state.get("peak_equity")), equity, 0.01)
    return max(0.0, (peak - equity) / peak)


def refresh_risk_locks(state):
    today = str(utc_now().date())
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_start_equity"] = safe_float(state.get("equity"))
        state["daily_loss_lock"] = False

    equity = safe_float(state.get("equity"))
    day_start = max(safe_float(state.get("daily_start_equity")), 0.01)
    daily_loss = max(0.0, (day_start - equity) / day_start)
    dd = current_drawdown(state)

    if daily_loss >= DAILY_LOSS_LIMIT:
        state["daily_loss_lock"] = True

    if dd >= MAX_DRAWDOWN_LIMIT:
        state["drawdown_lock"] = True

    if safe_int(state.get("consecutive_losses")) >= MAX_CONSECUTIVE_LOSSES:
        state["consecutive_loss_lock"] = True
    elif "consecutive_loss_lock" not in state:
        state["consecutive_loss_lock"] = False

    return {
        "daily_loss_pct": round(pct(daily_loss), 4),
        "daily_loss_limit_pct": pct(DAILY_LOSS_LIMIT),
        "daily_loss_lock": bool(state.get("daily_loss_lock")),
        "drawdown_pct": round(pct(dd), 4),
        "drawdown_limit_pct": pct(MAX_DRAWDOWN_LIMIT),
        "drawdown_lock": bool(state.get("drawdown_lock")),
        "consecutive_losses": safe_int(state.get("consecutive_losses")),
        "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
        "consecutive_loss_lock": bool(state.get("consecutive_loss_lock")),
        "risk_per_trade_pct": pct(RISK_PER_TRADE),
        "min_margin_pct": pct(MIN_MARGIN_FRACTION),
        "max_margin_pct": pct(MAX_MARGIN_FRACTION),
        "leverage": LEVERAGE,
        "paper_only": True,
        "live_trading_enabled": False,
    }


def position_unrealized(position, price):
    if not position:
        return 0.0
    qty = safe_float(position.get("quantity"))
    entry = safe_float(position.get("entry_price"))
    if position.get("side") == "LONG":
        return (price - entry) * qty
    return (entry - price) * qty


def position_marked_pnl(position, price):
    if not position:
        return 0.0
    gross = position_unrealized(position, price)
    exit_fee_estimate = (
        abs(safe_float(position.get("quantity")) * price) * FEE_RATE
    )
    entry_fee = safe_float(position.get("entry_fee"))
    funding_paid = safe_float(position.get("funding_paid"))
    return gross - entry_fee - exit_fee_estimate + funding_paid


# ============================================================
# POSITION SIZING
# ============================================================

def calculate_position_size(state, entry_price, stop_price):
    equity = safe_float(state.get("equity"))
    if equity <= 0 or entry_price <= 0:
        return None, "Invalid equity or entry price"

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return None, "Invalid stop distance"

    risk_budget = equity * RISK_PER_TRADE

    # Estimate entry + exit fees and slippage so sizing isn't based on
    # stop distance alone. This is a planning estimate, not a guarantee.
    cost_per_btc = (
        stop_distance
        + entry_price * (2.0 * FEE_RATE + 2.0 * SLIPPAGE_RATE)
    )
    if cost_per_btc <= 0:
        return None, "Invalid estimated cost"

    quantity_by_risk = risk_budget / cost_per_btc
    max_margin = equity * MAX_MARGIN_FRACTION
    max_notional = max_margin * LEVERAGE
    quantity_by_margin = max_notional / entry_price

    quantity = min(quantity_by_risk, quantity_by_margin)
    notional = quantity * entry_price
    margin_used = notional / LEVERAGE

    min_margin = equity * MIN_MARGIN_FRACTION

    if quantity <= 0 or notional <= 0:
        return None, "Position size is zero"

    # Never increase size just to satisfy the minimum margin if that would
    # violate the risk budget. Skip the entry instead.
    if margin_used < min_margin:
        return None, (
            f"Risk-sized margin ${margin_used:.2f} is below "
            f"the ${min_margin:.2f} minimum"
        )

    planned_risk = quantity * cost_per_btc
    if planned_risk > risk_budget * 1.000001:
        return None, "Planned risk exceeds the risk budget"

    return {
        "quantity": quantity,
        "notional": notional,
        "margin_used": margin_used,
        "risk_budget": risk_budget,
        "planned_risk": planned_risk,
        "risk_distance": stop_distance,
    }, None


# ============================================================
# OPEN AND CLOSE POSITIONS
# ============================================================

def open_position(state, side, market_price, atr_value, signal):
    if side not in ("LONG", "SHORT"):
        return False, "Invalid direction"

    slip_price = market_price * SLIPPAGE_RATE
    entry_price = (
        market_price + slip_price
        if side == "LONG"
        else market_price - slip_price
    )

    stop_distance = max(atr_value * ATR_STOP_MULTIPLIER, market_price * 0.001)
    target_distance = max(
        atr_value * ATR_TARGET_MULTIPLIER,
        stop_distance * MIN_REWARD_RISK,
    )

    if side == "LONG":
        stop_price = entry_price - stop_distance
        target_price = entry_price + target_distance
    else:
        stop_price = entry_price + stop_distance
        target_price = entry_price - target_distance

    sizing, error = calculate_position_size(state, entry_price, stop_price)
    if error:
        return False, error

    quantity = sizing["quantity"]
    notional = sizing["notional"]
    entry_fee = notional * FEE_RATE

    # Entry fee is deducted immediately from equity.
    state["equity"] = safe_float(state.get("equity")) - entry_fee

    now = unix_now()
    position = {
        "side": side,
        "entry_price": entry_price,
        "quantity": quantity,
        "leverage": LEVERAGE,
        "margin_used": sizing["margin_used"],
        "notional": notional,
        "risk_distance": stop_distance,
        "risk_budget": sizing["risk_budget"],
        "planned_risk": sizing["planned_risk"],
        "stop_price": stop_price,
        "target_price": target_price,
        "opened_timestamp": now,
        "entry_time": iso_now(),
        "entry_fee": entry_fee,
        "funding_paid": 0.0,
        "last_funding_timestamp": now,
        "initial_stop_price": stop_price,
        "initial_target_price": target_price,
        "runner_mode": False,
        "entry_signal_score": safe_int(signal.get("score")),
        "entry_rsi": safe_float(signal.get("rsi")),
        "build": BUILD,
    }

    state["position"] = position
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        safe_float(state.get("equity")),
    )
    return True, f"{side} opened"


def close_position(state, market_price, reason):
    position = state.get("position")
    if not position:
        return False

    side = position.get("side")
    quantity = safe_float(position.get("quantity"))
    entry_price = safe_float(position.get("entry_price"))

    slip_price = market_price * SLIPPAGE_RATE
    exit_price = (
        market_price - slip_price
        if side == "LONG"
        else market_price + slip_price
    )

    if side == "LONG":
        gross_pnl = (exit_price - entry_price) * quantity
    else:
        gross_pnl = (entry_price - exit_price) * quantity

    exit_fee = abs(exit_price * quantity) * FEE_RATE
    entry_fee = safe_float(position.get("entry_fee"))
    funding_paid = safe_float(position.get("funding_paid"))

    # Entry fee was deducted when opening. Funding is applied as it accrues.
    # Therefore only gross PnL and exit fee are applied here.
    state["equity"] = (
        safe_float(state.get("equity")) + gross_pnl - exit_fee
    )
    state["realized_pnl"] = safe_float(state.get("realized_pnl")) + (
        gross_pnl - entry_fee - exit_fee + funding_paid
    )

    net_pnl = gross_pnl - entry_fee - exit_fee + funding_paid
    margin_used = max(safe_float(position.get("margin_used")), 0.01)
    net_return_pct = net_pnl / margin_used * 100.0

    trade = {
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": quantity,
        "leverage": safe_float(position.get("leverage"), LEVERAGE),
        "margin_used": safe_float(position.get("margin_used")),
        "notional": safe_float(position.get("notional")),
        "opened_timestamp": safe_int(position.get("opened_timestamp")),
        "entry_time": position.get("entry_time"),
        "exit_timestamp": unix_now(),
        "exit_time": iso_now(),
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": entry_fee + exit_fee,
        "funding_paid": funding_paid,
        "net_pnl": net_pnl,
        "net_return_pct": net_return_pct,
        "reason": reason,
        "planned_risk": safe_float(position.get("planned_risk")),
        "build": BUILD,
    }

    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-1000:]

    if net_pnl < 0:
        state["consecutive_losses"] = (
            safe_int(state.get("consecutive_losses")) + 1
        )
    else:
        state["consecutive_losses"] = 0

    state["last_close_ts"] = unix_now()
    state["position"] = None
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        safe_float(state.get("equity")),
    )

    if safe_int(state.get("consecutive_losses")) >= MAX_CONSECUTIVE_LOSSES:
        state["consecutive_loss_lock"] = True

    return True


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def apply_funding(state, funding_rate, current_price):
    position = state.get("position")
    if not position:
        return

    now = unix_now()
    previous = safe_int(position.get("last_funding_timestamp"), now)
    elapsed_hours = max(0.0, min((now - previous) / 3600.0, 24.0))

    if elapsed_hours <= 0:
        return

    notional = abs(safe_float(position.get("quantity")) * current_price)
    # Approximate funding for elapsed time. Historical funding rates are
    # not fetched, so this uses the currently observed rate.
    funding_amount = notional * funding_rate * (elapsed_hours / 8.0)

    # Positive funding means longs pay shorts.
    if position.get("side") == "SHORT":
        funding_amount *= -1.0

    position["funding_paid"] = (
        safe_float(position.get("funding_paid")) - funding_amount
    )
    state["funding_total"] = (
        safe_float(state.get("funding_total")) - funding_amount
    )
    state["equity"] = safe_float(state.get("equity")) - funding_amount
    position["last_funding_timestamp"] = now


def manage_position(state, candles, current_price, funding_rate, atr_value):
    position = state.get("position")
    if not position:
        return

    apply_funding(state, funding_rate, current_price)
    position = state.get("position")
    if not position:
        return

    side = position.get("side")
    stop = safe_float(position.get("stop_price"))
    target = safe_float(position.get("target_price"))
    entry = safe_float(position.get("entry_price"))
    risk_distance = max(safe_float(position.get("risk_distance")), 0.01)

    # Check completed candles since the last processed candle.
    last_ts = safe_int(state.get("last_candle_ts"))
    new_candles = [c for c in candles if c["t"] > last_ts]

    for candle in new_candles:
        high = candle["h"]
        low = candle["l"]

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

        # Move stop to breakeven and trail only after sufficient movement.
        if favorable_move >= risk_distance * TRAIL_ACTIVATION_R:
            position["runner_mode"] = True
            trail_distance = max(atr_value * ATR_TRAIL_MULTIPLIER, 0.01)

            if side == "LONG":
                new_stop = max(entry, high - trail_distance)
                position["stop_price"] = max(stop, new_stop)
            else:
                new_stop = min(entry, low + trail_distance)
                position["stop_price"] = min(stop, new_stop)

            stop = safe_float(position.get("stop_price"))

    opened = safe_int(position.get("opened_timestamp"))
    if opened and unix_now() - opened >= MAX_HOLD_SECONDS:
        close_position(state, current_price, "MAX_HOLD_TIME")


# ============================================================
# METRICS AND DASHBOARD
# ============================================================

def calculate_metrics(state):
    trades = state.get("trades", [])
    closed = [t for t in trades if isinstance(t, dict) and "net_pnl" in t]

    wins = [t for t in closed if safe_float(t.get("net_pnl")) > 0]
    losses = [t for t in closed if safe_float(t.get("net_pnl")) < 0]

    total_net = sum(safe_float(t.get("net_pnl")) for t in closed)
    gross_wins = sum(safe_float(t.get("net_pnl")) for t in wins)
    gross_losses = abs(sum(safe_float(t.get("net_pnl")) for t in losses))
    profit_factor = (
        gross_wins / gross_losses if gross_losses > 0
        else (999.0 if gross_wins > 0 else 0.0)
    )

    win_rate = len(wins) / len(closed) * 100.0 if closed else 0.0
    average_trade = total_net / len(closed) if closed else 0.0

    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "total_net_pnl": round(total_net, 8),
        "average_trade": round(average_trade, 8),
        "profit_factor": round(profit_factor, 4),
        "best_trade": round(
            max((safe_float(t.get("net_pnl")) for t in closed), default=0.0), 8
        ),
        "worst_trade": round(
            min((safe_float(t.get("net_pnl")) for t in closed), default=0.0), 8
        ),
    }


def make_dashboard(
    state, price, funding_rate, signal, trends, market_status="OK"
):
    position = state.get("position")
    unrealized = position_unrealized(position, price) if position else 0.0
    marked_equity = safe_float(state.get("equity")) + unrealized
    net_pnl = marked_equity - safe_float(state.get("initial_equity"))
    initial_equity = max(safe_float(state.get("initial_equity")), 0.01)
    net_pnl_pct = net_pnl / initial_equity * 100.0

    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")), marked_equity
    )

    risk = refresh_risk_locks(state)
    metrics = calculate_metrics(state)
    trades = state.get("trades", [])

    history = state.setdefault("equity_history", [])
    history.append({
        "timestamp": iso_now(),
        "equity": round(safe_float(state.get("equity")), 8),
        "marked_equity": round(marked_equity, 8),
        "price": round(price, 6),
    })
    state["equity_history"] = history[-1000:]

    position_view = None
    if position:
        position_view = dict(position)
        position_view["unrealized_pnl"] = round(unrealized, 8)
        position_view["marked_pnl_after_estimated_costs"] = round(
            position_marked_pnl(position, price), 8
        )

    data = {
        "build": BUILD,
        "generated_at": iso_now(),
        "market_status": market_status,
        "price": round(price, 6),
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_rate * 100.0,
        "initial_equity": round(initial_equity, 8),
        "equity": round(safe_float(state.get("equity")), 8),
        "marked_equity": round(marked_equity, 8),
        "realized_pnl": round(safe_float(state.get("realized_pnl")), 8),
        "unrealized_pnl": round(unrealized, 8),
        "net_pnl": round(net_pnl, 8),
        "net_pnl_pct": round(net_pnl_pct, 6),
        "position": position_view,
        "open_position": position_view,
        "has_position": bool(position),
        "signal": signal,
        "risk": risk,
        "current_drawdown_pct": risk["drawdown_pct"],
        "max_drawdown_pct": risk["drawdown_pct"],
        "funding_total": round(safe_float(state.get("funding_total")), 8),
        "metrics": metrics,
        "closed_trades": metrics["closed_trades"],
        "win_rate": metrics["win_rate"],
        "trades": trades[-200:],
        "paper_trades": trades[-200:],
        "equity_history": state.get("equity_history", [])[-300:],
        "timeframes": trends,
        "config": {
            "risk_per_trade_pct": pct(RISK_PER_TRADE),
            "min_margin_pct": pct(MIN_MARGIN_FRACTION),
            "max_margin_pct": pct(MAX_MARGIN_FRACTION),
            "leverage": LEVERAGE,
            "daily_loss_limit_pct": pct(DAILY_LOSS_LIMIT),
            "max_drawdown_limit_pct": pct(MAX_DRAWDOWN_LIMIT),
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "fee_rate_per_side": FEE_RATE,
            "slippage_rate_per_side": SLIPPAGE_RATE,
            "paper_only": True,
            "live_trading_enabled": False,
        },
        "last_error": state.get("last_error"),
    }
    return data


# ============================================================
# MAIN ENGINE
# ============================================================

def main():
    state = load_state()
    state["last_error"] = None

    try:
        price, funding_rate = fetch_market_data()
        c15 = fetch_candles("15m")
        c1h = fetch_candles("1h")
        c4h = fetch_candles("4h")

        signal = build_signal(c15, c1h, c4h, funding_rate)
        atr_value = safe_float(signal.get("atr"), price * 0.002)

        trends = {
            "15m": signal.get("trend_15m"),
            "1h": signal.get("trend_1h"),
            "4h": signal.get("trend_4h"),
            "rsi": signal.get("rsi"),
            "atr": signal.get("atr"),
            "ema9": signal.get("ema9"),
            "ema21": signal.get("ema21"),
        }

        # Manage an existing position before considering a new entry.
        if state.get("position"):
            manage_position(state, c15, price, funding_rate, atr_value)

        # Record the latest completed candle after managing existing positions.
        if c15:
            state["last_candle_ts"] = max(
                safe_int(state.get("last_candle_ts")),
                safe_int(c15[-1].get("t")),
            )

        risk = refresh_risk_locks(state)
        cooldown_remaining = max(
            0,
            COOLDOWN_SECONDS - (unix_now() - safe_int(state.get("last_close_ts"))),
        )

        entry_message = "No entry"
        if state.get("position"):
            entry_message = "Position already open"
        elif risk["daily_loss_lock"]:
            entry_message = "Daily loss lock active"
        elif risk["drawdown_lock"]:
            entry_message = "Drawdown lock active"
        elif risk["consecutive_loss_lock"]:
            entry_message = "Consecutive-loss lock active"
        elif cooldown_remaining > 0:
            entry_message = f"Cooldown: {cooldown_remaining}s remaining"
        elif signal["direction"] in ("LONG", "SHORT"):
            ok, message = open_position(
                state,
                signal["direction"],
                price,
                atr_value,
                signal,
            )
            entry_message = message if ok else f"Entry skipped: {message}"

        signal["entry_status"] = entry_message
        signal["cooldown_remaining_seconds"] = cooldown_remaining

        data = make_dashboard(
            state,
            price,
            funding_rate,
            signal,
            trends,
            market_status="OK",
        )

        # Save both the engine state and dashboard data.
        write_json(STATE_FILE, state)
        write_json(DATA_FILE, data)

        print(f"BUILD: {BUILD}")
        print(f"PRICE: {price:.2f}")
        print(
            f"SIGNAL: {signal['direction']} / {signal['score']} "
            f"(long={signal['long_score']}, short={signal['short_score']})"
        )
        print(f"ENTRY STATUS: {entry_message}")
        print(f"EQUITY: {safe_float(state.get('equity')):.6f}")
        print(f"MARKED EQUITY: {data['marked_equity']:.6f}")
        print(f"UNREALIZED PNL: {data['unrealized_pnl']:.6f}")
        print(f"CLOSED TRADES: {data['closed_trades']}")
        print(f"WIN RATE: {data['win_rate']:.2f}%")
        print(f"RISK PER TRADE: {pct(RISK_PER_TRADE):.2f}%")
        print(
            f"MARGIN TARGET: {pct(MIN_MARGIN_FRACTION):.0f}% - "
            f"{pct(MAX_MARGIN_FRACTION):.0f}%"
        )
        print(f"DAILY LOSS LOCK: {risk['daily_loss_lock']}")
        print(f"DRAWDOWN LOCK: {risk['drawdown_lock']}")
        print("PAPER ONLY: True")
        print("LIVE TRADING ENABLED: False")

    except Exception as exc:
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["build"] = BUILD

        # If market data fails, preserve existing state and try to publish
        # the last known dashboard values instead of destroying the history.
        try:
            old_data = read_json(DATA_FILE, {})
            if isinstance(old_data, dict) and old_data:
                old_data["build"] = BUILD
                old_data["generated_at"] = iso_now()
                old_data["market_status"] = "ERROR"
                old_data["last_error"] = state["last_error"]
                write_json(DATA_FILE, old_data)
        except Exception:
            pass

        write_json(STATE_FILE, state)
        print(f"ERROR: {state['last_error']}")
        raise


if __name__ == "__main__":
    main()
