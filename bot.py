#!/usr/bin/env python3
"""
BTC Paper Engine V19.0
Public market data only.
Paper trading only. No wallet, private keys, or live orders.
"""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V19_3_2026-10-29"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

# ============================================================
# CONFIGURATION
# ============================================================

INITIAL_EQUITY = 100.0

# Fixed leverage.
LEVERAGE = 40.0

# Planned risk per trade ≈ 20% of equity at full stop-out.
# Actual loss can still exceed planned risk due to gaps, fees, and slippage.
RISK_PER_TRADE = 0.20

# Maximum simulated margin allocation.
MAX_MARGIN_FRACTION = 0.25

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

# Stop ~ATR×2.6. Partial/trail set for high R:R so 100%+ equity is reachable
# (≈5R when planned risk is 20%).
ATR_STOP = 2.6
PARTIAL_TP_ATR = 13.0
PARTIAL_TP_FRACTION = 0.25
TRAIL_ATR = 4.0
TRAIL_START_ATR = 3.5

# Slightly stricter entry for higher win-rate selectivity.
MIN_SIGNAL_SCORE = 7
MIN_SCORE_EDGE = 3

COOLDOWN_SECONDS = 300
MAX_HOLD_SECONDS = 86400

# Allow room for a full 20% stop without immediately locking the day.
DAILY_LOSS_LIMIT = 0.40
MAX_DRAWDOWN_LIMIT = 0.50
DRAWDOWN_PAUSE_SECONDS = 21600

MAX_CONSECUTIVE_LOSSES = 6
CONSECUTIVE_PAUSE_SECONDS = 3600

HTTP_TIMEOUT = 20
HISTORY_LIMIT = 1000
TRADE_LIMIT = 1000

# ============================================================
# UTILITIES
# ============================================================

def now_ts():
    return int(time.time())


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def utc_date():
    return datetime.now(timezone.utc).date().isoformat()


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
            "User-Agent": "BTC-Paper-Engine-V19/1.0",
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
        "drawdown_pause_until": 0,
        "last_error": None,
        "events": [],
    }


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
        p = s["position"]
        p.setdefault("entry_fee", 0.0)
        p.setdefault("funding_pnl", 0.0)
        p.setdefault("partial_taken", False)
        p.setdefault("best_price", f(p.get("entry_price")))
        p.setdefault("last_funding_ts", now_ts())
        p.setdefault("last_checked_candle_ts", 0)
        p.setdefault("opened_timestamp", now_ts())
        p.setdefault("leverage", LEVERAGE)

    s["build"] = BUILD
    return s


# ============================================================
# MARKET DATA
# ============================================================

def fetch_price_funding():
    mids = post({"type": "allMids"})
    price = f(mids.get(COIN)) if isinstance(mids, dict) else 0.0

    if price <= 0:
        raise RuntimeError("BTC price unavailable from Hyperliquid")

    funding = 0.0

    try:
        result = post({"type": "metaAndAssetCtxs"})

        if isinstance(result, list) and len(result) > 1:
            universe = result[0].get("universe", [])
            contexts = result[1]

            for index, asset in enumerate(universe):
                if asset.get("name") == COIN and index < len(contexts):
                    funding = f(contexts[index].get("funding"))
                    break

    except Exception as exc:
        print("Funding unavailable:", exc)

    return price, funding


def fetch_candles(interval, limit=200):
    minutes = {
        "15m": 15,
        "1h": 60,
        "4h": 240,
    }[interval]

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

    if len(candles) < 35:
        raise RuntimeError("Not enough closed candles: " + interval)

    return candles[-limit:]


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


def atr(candles, period=14):
    true_ranges = []

    for index in range(1, len(candles)):
        current = candles[index]
        previous_close = candles[index - 1]["c"]

        true_ranges.append(max(
            current["h"] - current["l"],
            abs(current["h"] - previous_close),
            abs(current["l"] - previous_close),
        ))

    if not true_ranges:
        return candles[-1]["c"] * 0.002

    seed = true_ranges[:period]
    value = sum(seed) / len(seed)

    for item in true_ranges[period:]:
        value = (value * (period - 1) + item) / period

    return max(value, 0.01)


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


# ============================================================
# SIGNAL ENGINE
# ============================================================

def make_signal(c15, c1, c4, funding):
    values = [x["c"] for x in c15]
    ema9 = ema(values, 9)
    ema21 = ema(values, 21)

    current = values[-1]
    previous = values[-2]

    rv = rsi(values)
    av = atr(c15)
    bb = bollinger(values)

    previous_bb = bollinger(values[:-1])

    trends = {
        "15m": trend(c15),
        "1h": trend(c1),
        "4h": trend(c4),
    }

    long_score = 0
    short_score = 0
    reasons = []
    flags = []

    # EMA direction.
    if ema9[-1] > ema21[-1]:
        long_score += 2
        reasons.append("EMA上向き")
    elif ema9[-1] < ema21[-1]:
        short_score += 2
        reasons.append("EMA下向き")

    # Multi-timeframe trends.
    for timeframe, direction in trends.items():
        points = 1 if timeframe == "15m" else 2

        if direction == "UP":
            long_score += points
            reasons.append(timeframe + "上昇")
        elif direction == "DOWN":
            short_score += points
            reasons.append(timeframe + "下降")

    # RSI is a supporting indicator, not a standalone entry.
    if 43 <= rv <= 68:
        long_score += 1

    if 32 <= rv <= 57:
        short_score += 1

    if rv > 78:
        flags.append("RSI買われ過ぎ")
    elif rv < 22:
        flags.append("RSI売られ過ぎ")

    # Short-term momentum.
    if len(values) >= 5:
        momentum = current / values[-5] - 1.0

        if momentum > 0.0008:
            long_score += 1
            reasons.append("短期モメンタム上")
        elif momentum < -0.0008:
            short_score += 1
            reasons.append("短期モメンタム下")

    # Bollinger Band logic.
    width_expanding = bb["width_pct"] > previous_bb["width_pct"]

    if width_expanding:
        flags.append("BB幅拡大")
    else:
        flags.append("BB幅縮小")

    # Trend continuation: a band touch alone is NOT a reversal signal.
    if (
        bb["state"] in ("NEAR_UPPER", "ABOVE_UPPER")
        and trends["1h"] == "UP"
        and trends["4h"] != "DOWN"
    ):
        long_score += 1
        reasons.append("BB上側・上昇継続候補")

    if (
        bb["state"] in ("NEAR_LOWER", "BELOW_LOWER")
        and trends["1h"] == "DOWN"
        and trends["4h"] != "UP"
    ):
        short_score += 1
        reasons.append("BB下側・下降継続候補")

    # A tight band can signal compression, but is not enough to enter alone.
    if bb["width_pct"] < 0.35:
        flags.append("BB収縮・ブレイク待ち")

    # Funding is a small factor, never the sole trigger.
    if funding > 0.0003:
        short_score += 1
        reasons.append("プラス資金調達率")
    elif funding < -0.0003:
        long_score += 1
        reasons.append("マイナス資金調達率")

    edge = abs(long_score - short_score)
    direction = "WAIT"

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

    # Fibonacci levels from recent 1h candles.
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
        "direction": direction,
        "score": max(long_score, short_score),
        "long_score": long_score,
        "short_score": short_score,
        "score_edge": edge,
        "rsi": round(rv, 3),
        "atr": round(av, 5),
        "ema9": ema9[-1],
        "ema21": ema21[-1],
        "trend_15m": trends["15m"],
        "trend_1h": trends["1h"],
        "trend_4h": trends["4h"],
        "timestamp": iso_now(),
        "reference_price": current,
        "previous_close": previous,
        "reasons": reasons or ["明確な方向優位性なし"],
        "flags": flags,
        "bollinger": {
            **bb,
            "width_expanding": width_expanding,
            "previous_width_pct": previous_bb["width_pct"],
        },
        "fibonacci": {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "levels": fib_levels,
            "trend": trends["1h"],
        },
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


def size_position(equity, entry, stop):
    distance = abs(entry - stop)
    risk_budget = max(0.0, equity) * RISK_PER_TRADE

    estimated_cost_per_unit = (
        distance + entry * (2 * FEE_RATE + 2 * SLIPPAGE_RATE)
    )

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

    return {
        "quantity": quantity,
        "margin_used": notional / LEVERAGE,
        "notional": notional,
        "planned_risk": quantity * estimated_cost_per_unit,
        "effective_risk_pct": (
            quantity * estimated_cost_per_unit / equity * 100
        ),
    }


def apply_funding(s, funding):
    """
    Approximate paper funding cost using the current funding rate.
    Hyperliquid funding is paid hourly. This estimate assumes the
    current rate applies throughout elapsed time; historical rates
    are not reconstructed, so this is an approximation.
    """
    pos = s.get("position")
    if not pos:
        return

    current_time = now_ts()
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


def close_position(s, price, reason, fraction=1.0):
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

    quantity = old_quantity * fraction
    side = pos["side"]
    entry = f(pos.get("entry_price"))

    # Simulate worse execution on exit.
    exit_price = (
        price * (1 - SLIPPAGE_RATE)
        if side == "LONG"
        else price * (1 + SLIPPAGE_RATE)
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

    trade = {
        "side": side,
        "direction": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": quantity,
        "leverage": pos.get("leverage", LEVERAGE),
        "margin_used": f(pos.get("margin_used")) * fraction,
        "entry_time": pos.get("entry_time"),
        "exit_time": iso_now(),
        "exit_timestamp": now_ts(),
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
    )

    if fraction >= 0.999999:
        s["consecutive_losses"] = (
            s["consecutive_losses"] + 1 if net < 0 else 0
        )

        if s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            s["consecutive_lock_until"] = (
                now_ts() + CONSECUTIVE_PAUSE_SECONDS
            )

        s["last_close_ts"] = now_ts()
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


def open_position(s, side, price, av, sig, candle_ts):
    entry = (
        price * (1 + SLIPPAGE_RATE)
        if side == "LONG"
        else price * (1 - SLIPPAGE_RATE)
    )

    distance = max(av * ATR_STOP, price * 0.0012)

    stop = (
        entry - distance if side == "LONG"
        else entry + distance
    )

    sizing = size_position(s["equity"], entry, stop)
    if not sizing:
        return False, "数量計算失敗"

    quantity = sizing["quantity"]
    notional = sizing["notional"]
    fee = notional * FEE_RATE

    if fee >= s["equity"]:
        return False, "手数料が資金を超過"

    s["equity"] -= fee

    s["position"] = {
        "side": side,
        "entry_price": entry,
        "quantity": quantity,
        "leverage": LEVERAGE,
        "margin_used": sizing["margin_used"],
        "notional": notional,
        "planned_risk": sizing["planned_risk"],
        "effective_risk_pct": sizing["effective_risk_pct"],
        "stop_price": stop,
        "initial_stop_price": stop,
        "risk_distance": distance,
        "entry_fee": fee,
        "funding_pnl": 0.0,
        "entry_time": iso_now(),
        "opened_timestamp": now_ts(),
        "last_funding_ts": now_ts(),
        "last_checked_candle_ts": candle_ts,
        "entry_signal_score": sig["score"],
        "entry_rsi": sig["rsi"],
        "entry_bb_width_pct": sig["bollinger"]["width_pct"],
        "partial_taken": False,
        "best_price": price,
        "trail_active": False,
    }

    s["last_entry_candle"] = candle_ts

    event(
        s,
        "POSITION_OPENED",
        side=side,
        entry_price=entry,
        stop_price=stop,
        quantity=quantity,
        leverage=LEVERAGE,
        effective_risk_pct=sizing["effective_risk_pct"],
    )

    return True, side + " opened"


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def manage_position(s, candles, price, sig, funding):
    pos = s.get("position")
    if not pos:
        return

    apply_funding(s, funding)

    pos = s.get("position")
    if not pos:
        return

    side = pos["side"]
    stop = f(pos.get("stop_price"))
    entry = f(pos.get("entry_price"))
    distance = max(f(pos.get("risk_distance")), 0.01)
    target = (
        entry + distance * PARTIAL_TP_ATR / ATR_STOP
        if side == "LONG"
        else entry - distance * PARTIAL_TP_ATR / ATR_STOP
    )

    cursor = i(pos.get("last_checked_candle_ts"))

    for candle in candles:
        if candle["t"] <= cursor:
            continue

        high = candle["h"]
        low = candle["l"]
        close = candle["c"]

        # Conservative ordering: if both stop and target are touched
        # in one candle, process the stop first.
        if side == "LONG" and low <= stop:
            close_position(s, stop, "TRAIL_STOP" if pos.get("trail_active") else "STOP_LOSS")
            return

        if side == "SHORT" and high >= stop:
            close_position(s, stop, "TRAIL_STOP" if pos.get("trail_active") else "STOP_LOSS")
            return

        if not pos.get("partial_taken"):
            hit_target = (
                high >= target if side == "LONG"
                else low <= target
            )

            if hit_target:
                if close_position(
                    s,
                    target,
                    "PARTIAL_TAKE_PROFIT",
                    PARTIAL_TP_FRACTION,
                ):
                    pos = s.get("position")
                    if not pos:
                        return

                    # Once a partial profit is booked, move stop toward
                    # entry to reduce the chance of giving back gains.
                    if side == "LONG":
                        pos["stop_price"] = max(
                            f(pos.get("stop_price")),
                            entry + distance * 0.05,
                        )
                    else:
                        pos["stop_price"] = min(
                            f(pos.get("stop_price")),
                            entry - distance * 0.05,
                        )

                    stop = f(pos.get("stop_price"))

        # Track the best price after entry.
        if side == "LONG":
            pos["best_price"] = max(
                f(pos.get("best_price"), entry), high
            )
            favorable_move = f(pos["best_price"]) - entry
        else:
            old_best = f(pos.get("best_price"), entry)
            pos["best_price"] = min(old_best, low)
            favorable_move = entry - f(pos["best_price"])

        # Activate trailing only after the move has progressed.
        if favorable_move >= distance * TRAIL_START_ATR / ATR_STOP:
            pos["trail_active"] = True

        if pos.get("trail_active"):
            trail_distance = max(
                f(sig.get("atr")) * TRAIL_ATR,
                distance * 0.75,
            )

            if side == "LONG":
                new_stop = f(pos["best_price"]) - trail_distance
                pos["stop_price"] = max(
                    f(pos.get("stop_price")), new_stop
                )
            else:
                new_stop = f(pos["best_price"]) + trail_distance
                pos["stop_price"] = min(
                    f(pos.get("stop_price")), new_stop
                )

        # If momentum and trend both turn against the position,
        # exit the remaining size rather than waiting indefinitely.
        trend_15m = sig.get("trend_15m")
        trend_1h = sig.get("trend_1h")

        if pos.get("partial_taken"):
            if (
                side == "LONG"
                and trend_15m == "DOWN"
                and trend_1h == "DOWN"
                and close < f(sig.get("bollinger", {}).get("middle"), close)
            ):
                close_position(s, close, "TREND_REVERSAL")
                return

            if (
                side == "SHORT"
                and trend_15m == "UP"
                and trend_1h == "UP"
                and close > f(sig.get("bollinger", {}).get("middle"), close)
            ):
                close_position(s, close, "TREND_REVERSAL")
                return

        pos["last_checked_candle_ts"] = candle["t"]
        cursor = candle["t"]

    if now_ts() - i(pos.get("opened_timestamp")) >= MAX_HOLD_SECONDS:
        close_position(s, price, "MAX_HOLD_TIME")


# ============================================================
# RISK AND DASHBOARD DATA
# ============================================================

def make_dashboard(s, price, funding, sig, status):
    pos = s.get("position")
    upnl = unrealized(pos, price) if pos else 0.0
    marked_equity = s["equity"] + upnl

    initial = max(f(s["initial_equity"]), 0.01)

    s["peak_equity"] = max(
        f(s.get("peak_equity"), initial),
        marked_equity,
    )

    current_dd = max(
        0.0,
        (s["peak_equity"] - marked_equity)
        / max(s["peak_equity"], 0.01) * 100.0,
    )

    s["max_drawdown_pct"] = max(
        f(s.get("max_drawdown_pct")),
        current_dd,
    )

    if s["daily_date"] != utc_date():
        s["daily_date"] = utc_date()
        s["daily_start_equity"] = marked_equity
        s["daily_loss_lock"] = False
        event(s, "DAILY_RESET")

    daily_start = max(f(s.get("daily_start_equity")), 0.01)

    daily_loss = max(
        0.0,
        (daily_start - marked_equity) / daily_start * 100.0,
    )

    if daily_loss >= DAILY_LOSS_LIMIT * 100:
        s["daily_loss_lock"] = True

    if (
        current_dd >= MAX_DRAWDOWN_LIMIT * 100
        and s["drawdown_pause_until"] < now_ts()
    ):
        s["drawdown_pause_until"] = (
            now_ts() + DRAWDOWN_PAUSE_SECONDS
        )
        event(
            s,
            "DRAWDOWN_PAUSE_STARTED",
            drawdown_pct=current_dd,
        )

    if (
        s["drawdown_pause_until"]
        and s["drawdown_pause_until"] <= now_ts()
    ):
        s["drawdown_pause_until"] = 0
        event(s, "DRAWDOWN_PAUSE_ENDED")

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
            "drawdown_pct": current_dd,
            "current_drawdown_pct": current_dd,
            "max_drawdown_pct": s["max_drawdown_pct"],
            "drawdown_limit_pct": MAX_DRAWDOWN_LIMIT * 100,
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
        "equity_history": s["equity_history"][-300:],
        "events": s["events"][-100:],
        "timeframes": {
            "15m": {"direction": sig["trend_15m"]},
            "1h": {"direction": sig["trend_1h"]},
            "4h": {"direction": sig["trend_4h"]},
        },
        "indicators": {
            "rsi_15m": sig["rsi"],
            "atr": sig["atr"],
            "bollinger": sig["bollinger"],
        },
        "fibonacci": sig["fibonacci"],
        "config": {
            "leverage": LEVERAGE,
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "paper_only": True,
            "live_trading_enabled": False,
            "min_signal_score": MIN_SIGNAL_SCORE,
            "min_score_edge": MIN_SCORE_EDGE,
            "partial_tp_fraction": PARTIAL_TP_FRACTION,
            "trail_atr": TRAIL_ATR,
        },
        "last_error": s.get("last_error"),
    }


# ============================================================
# MAIN
# ============================================================

def main():
    s = load_state()
    s["last_error"] = None

    try:
        price, funding = fetch_price_funding()

        c15 = fetch_candles("15m")
        c1 = fetch_candles("1h")
        c4 = fetch_candles("4h")

        sig = make_signal(c15, c1, c4, funding)
        latest_candle = c15[-1]["t"]

        had_position = bool(s.get("position"))

        if had_position:
            manage_position(s, c15, price, sig, funding)

        if had_position and not s.get("position"):
            s["last_entry_candle"] = latest_candle

        status = "WAIT: シグナル条件未達"
        now = now_ts()

        if s.get("position"):
            status = "保有中"
        elif s["daily_loss_lock"]:
            status = "日次損失停止中（UTC日付更新で解除）"
        elif s["drawdown_pause_until"] > now:
            remaining = (s["drawdown_pause_until"] - now) // 60
            status = f"DD休止中（残り{remaining}分）"
        elif s["consecutive_lock_until"] > now:
            remaining = (s["consecutive_lock_until"] - now) // 60
            status = f"連敗休止中（残り{remaining}分）"
        elif now - s["last_close_ts"] < COOLDOWN_SECONDS:
            status = "クールダウン中"
        elif latest_candle == s["last_entry_candle"]:
            status = "同じ15分足での再エントリー防止"
        elif sig["direction"] in ("LONG", "SHORT"):
            ok, message = open_position(
                s,
                sig["direction"],
                price,
                sig["atr"],
                sig,
                latest_candle,
            )
            status = message if ok else "エントリー見送り: " + message
        else:
            status = (
                f"WAIT（LONG {sig['long_score']} / "
                f"SHORT {sig['short_score']}、"
                f"必要スコア {MIN_SIGNAL_SCORE}）"
            )

        sig["entry_status"] = status

        dashboard = make_dashboard(
            s, price, funding, sig, status
        )

        write_json(STATE_FILE, s)
        write_json(DATA_FILE, dashboard)

        print(
            f"BUILD: {BUILD}\n"
            f"PRICE: {price:.2f}\n"
            f"SIGNAL: {sig['direction']} "
            f"score={sig['score']} "
            f"long={sig['long_score']} "
            f"short={sig['short_score']}\n"
            f"BB: {sig['bollinger']['state']} "
            f"width={sig['bollinger']['width_pct']:.4f}%\n"
            f"ENTRY: {status}\n"
            f"EQUITY: {s['equity']:.4f}\n"
            f"REALIZED PNL: {s['realized_pnl']:.4f}\n"
            f"CLOSED RECORDS: {len(s['trades'])}\n"
            f"LEVERAGE: {LEVERAGE}x\n"
            f"PLANNED RISK: {RISK_PER_TRADE * 100:.1f}%\n"
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