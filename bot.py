
#!/usr/bin/env python3
"""BTC Paper Engine V16 - paper trading only. No live orders."""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V16_2026-10-23"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0

# Risk controls: preserve V15 limits
RISK_PER_TRADE = 0.010
MIN_MARGIN_FRACTION = 0.05
MAX_MARGIN_FRACTION = 0.50
DAILY_LOSS_LIMIT = 0.03
MAX_DRAWDOWN_LIMIT = 0.10
MAX_CONSECUTIVE_LOSSES = 4

# V16 frequency tuning
COOLDOWN_SECONDS = 300
MAX_HOLD_SECONDS = 86400
MIN_SIGNAL_SCORE = 5
MIN_SCORE_EDGE = 1

# Execution assumptions
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
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(
            data, f, ensure_ascii=False, indent=2, allow_nan=False
        )
    temp.replace(path)


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def post(payload):
    request = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V16/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_timestamp(value):
    ts = safe_int(value)
    if ts > 100_000_000_000:
        ts //= 1000
    if ts < 0 or ts > now_ts() + 60:
        return 0
    return ts


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
        "daily_date": utc_date(),
        "daily_start_equity": INITIAL_EQUITY,
        "daily_loss_lock": False,
        "drawdown_lock": False,
        "last_error": None,
    }


def load_state():
    state = read_json(STATE_FILE, {})
    if not isinstance(state, dict) or not state:
        return default_state()

    defaults = default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = value

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

    trades = state.get("trades")
    if not isinstance(trades, list):
        trades = state.get("paper_trades", [])
    state["trades"] = trades if isinstance(trades, list) else []

    history = state.get("equity_history", [])
    state["equity_history"] = history if isinstance(history, list) else []

    position = state.get("position") or state.get("open_position")
    state["position"] = position if isinstance(position, dict) else None

    state["last_close_ts"] = normalize_timestamp(
        state.get("last_close_ts")
    )
    state["last_candle_ts"] = normalize_timestamp(
        state.get("last_candle_ts")
    )
    state["consecutive_losses"] = max(
        0, safe_int(state.get("consecutive_losses"))
    )
    state["consecutive_loss_lock"] = bool(
        state.get("consecutive_loss_lock")
    )
    state["daily_start_equity"] = safe_float(
        state.get("daily_start_equity"), state["equity"]
    )
    state["daily_loss_lock"] = bool(state.get("daily_loss_lock"))
    state["drawdown_lock"] = bool(state.get("drawdown_lock"))
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
            for i, asset in enumerate(universe):
                if asset.get("name") == COIN and i < len(contexts):
                    funding = safe_float(contexts[i].get("funding"))
                    break
    except Exception as exc:
        print(f"Funding unavailable; using zero: {exc}")

    return price, funding


def fetch_candles(interval, limit=CANDLE_LIMIT):
    durations = {
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
    }
    interval_ms = durations[interval]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - interval_ms * (limit + 5)

    raw = post({
        "type": "candleSnapshot",
        "req": {
            "coin": COIN,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    })
    if not isinstance(raw, list):
        raise RuntimeError(f"Invalid {interval} candle response")

    candles = []
    for item in raw:
        c = {
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
            c["t"] > 0
            and c["c"] > 0
            and c["h"] >= c["l"]
            and c["h"] > 0
            and c["l"] > 0
        ):
            candles.append(c)

    candles.sort(key=lambda c: c["t"])
    now_ms = int(time.time() * 1000)
    candles = [
        c for c in candles
        if c["t"] + interval_ms <= now_ms
    ]

    if len(candles) < 35:
        raise RuntimeError(f"Not enough closed candles for {interval}")
    return candles[-limit:]


def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def calc_rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return max(candles[-1]["c"] * 0.002, 1.0)

    trs = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev_close = candles[i - 1]["c"]
        trs.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev_close),
            abs(cur["l"] - prev_close),
        ))

    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return max(value, 0.01)


def trend(candles):
    values = [c["c"] for c in candles]
    fast = ema(values, 9)[-1]
    slow = ema(values, 21)[-1]

    if fast > slow * 1.0005:
        return "UP"
    if fast < slow * 0.9995:
        return "DOWN"
    return "SIDEWAYS"


def make_signal(c15, c1h, c4h, funding):
    values = [c["c"] for c in c15]
    e9 = ema(values, 9)
    e21 = ema(values, 21)
    rsi_value = calc_rsi(values)
    atr_value = calc_atr(c15)

    t15 = trend(c15)
    t1h = trend(c1h)
    t4h = trend(c4h)

    long_score = 0
    short_score = 0
    reasons = []

    # Short-term momentum: increased sensitivity in V16.
    if e9[-1] > e21[-1]:
        long_score += 2
        reasons.append("15m EMA bullish")
    elif e9[-1] < e21[-1]:
        short_score += 2
        reasons.append("15m EMA bearish")

    # Trend agreement: 15m is useful for timing;
    # 1h and 4h retain influence without absolute vetoes.
    for timeframe, direction, weight in (
        ("15m", t15, 2),
        ("1h", t1h, 2),
        ("4h", t4h, 1),
    ):
        if direction == "UP":
            long_score += weight
            reasons.append(f"{timeframe} uptrend")
        elif direction == "DOWN":
            short_score += weight
            reasons.append(f"{timeframe} downtrend")

    # RSI momentum and overextension filters.
    if 42 <= rsi_value <= 68:
        long_score += 1
    if 32 <= rsi_value <= 58:
        short_score += 1

    if rsi_value > 76:
        long_score -= 2
        reasons.append("RSI overbought")
    if rsi_value < 24:
        short_score -= 2
        reasons.append("RSI oversold")

    # Reward early momentum, but do not enter an extreme move blindly.
    if len(values) >= 5 and values[-5] > 0:
        momentum = (values[-1] / values[-5]) - 1.0
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
        "rsi": round(rsi_value, 4),
        "atr": round(atr_value, 6),
        "ema9": round(e9[-1], 6),
        "ema21": round(e21[-1], 6),
        "trend_15m": t15,
        "trend_1h": t1h,
        "trend_4h": t4h,
        "timestamp": iso_now(),
        "reference_price": values[-1],
        "reasons": reasons or ["No clear directional advantage"],
    }


def unrealized(position, price):
    if not position:
        return 0.0

    qty = safe_float(position.get("quantity"))
    entry = safe_float(position.get("entry_price"))

    if position.get("side") == "LONG":
        return (price - entry) * qty
    return (entry - price) * qty


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


def refresh_locks(state, marked_equity=None):
    today = utc_date()

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_start_equity"] = safe_float(
            state.get("equity")
        )
        # Reset only the daily lock at UTC day change.
        state["daily_loss_lock"] = False

    equity = safe_float(state.get("equity"))
    marked = equity if marked_equity is None else safe_float(
        marked_equity, equity
    )
    day_start = max(
        safe_float(state.get("daily_start_equity"), equity),
        0.01,
    )

    daily_loss = max(0.0, (day_start - marked) / day_start)
    dd = drawdown(state, marked)

    if daily_loss >= DAILY_LOSS_LIMIT:
        state["daily_loss_lock"] = True
    if dd >= MAX_DRAWDOWN_LIMIT:
        state["drawdown_lock"] = True
    if safe_int(state.get("consecutive_losses")) >= MAX_CONSECUTIVE_LOSSES:
        state["consecutive_loss_lock"] = True

    return {
        "daily_loss_pct": round(daily_loss * 100, 4),
        "daily_loss_lock": bool(state.get("daily_loss_lock")),
        "drawdown_pct": round(dd * 100, 4),
        "drawdown_lock": bool(state.get("drawdown_lock")),
        "consecutive_losses": safe_int(
            state.get("consecutive_losses")
        ),
        "consecutive_loss_lock": bool(
            state.get("consecutive_loss_lock")
        ),
        "risk_per_trade_pct": RISK_PER_TRADE * 100,
        "min_margin_pct": MIN_MARGIN_FRACTION * 100,
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
    estimated_cost = distance + entry * (
        2 * FEE_RATE + 2 * SLIPPAGE_RATE
    )

    if estimated_cost <= 0:
        return None, "Invalid estimated cost"

    qty_risk = risk_budget / estimated_cost
    qty_margin = (
        equity * MAX_MARGIN_FRACTION * LEVERAGE / entry
    )
    qty = min(qty_risk, qty_margin)

    if qty <= 0:
        return None, "Position quantity is zero"

    notional = qty * entry
    margin = notional / LEVERAGE
    planned_risk = qty * estimated_cost

    # V16: do not reject a safe risk-sized position merely because
    # its margin is below 5% of equity. Never enlarge it to meet
    # a minimum margin target.
    if planned_risk > risk_budget * 1.000001:
        return None, "Risk budget check failed"

    if margin > equity * MAX_MARGIN_FRACTION * 1.000001:
        return None, "Maximum margin check failed"

    return {
        "quantity": qty,
        "notional": notional,
        "margin_used": margin,
        "risk_budget": risk_budget,
        "planned_risk": planned_risk,
    }, None


def open_position(state, side, price, atr_value, signal):
    slip = price * SLIPPAGE_RATE
    entry = price + slip if side == "LONG" else price - slip

    distance = max(
        atr_value * ATR_STOP_MULTIPLIER,
        price * 0.001,
    )
    target_distance = max(
        atr_value * ATR_TARGET_MULTIPLIER,
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
        "runner_mode": False,
        "entry_signal_score": signal.get("score", 0),
        "entry_rsi": signal.get("rsi", 50),
        "entry_signal": signal.get("direction"),
        "build": BUILD,
    }

    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        safe_float(state.get("equity")),
    )
    return True, f"{side} opened"


def close_position(state, price, reason):
    p = state.get("position")
    if not p:
        return False

    side = p.get("side")
    qty = safe_float(p.get("quantity"))
    entry = safe_float(p.get("entry_price"))

    if qty <= 0 or entry <= 0 or price <= 0:
        state["last_error"] = "Invalid position values at close"
        return False

    exit_price = (
        price * (1 - SLIPPAGE_RATE)
        if side == "LONG"
        else price * (1 + SLIPPAGE_RATE)
    )

    gross = (
        (exit_price - entry) * qty
        if side == "LONG"
        else (entry - exit_price) * qty
    )

    entry_fee = safe_float(p.get("entry_fee"))
    exit_fee = abs(exit_price * qty) * FEE_RATE
    funding_paid = safe_float(p.get("funding_paid"))
    net = gross - entry_fee - exit_fee + funding_paid

    # Entry fee and funding have already been reflected in equity.
    state["equity"] = (
        safe_float(state.get("equity")) + gross - exit_fee
    )
    state["realized_pnl"] = (
        safe_float(state.get("realized_pnl")) + net
    )

    margin = max(safe_float(p.get("margin_used")), 0.01)
    trade = {
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": qty,
        "leverage": safe_float(p.get("leverage"), LEVERAGE),
        "margin_used": margin,
        "notional": safe_float(p.get("notional")),
        "opened_timestamp": safe_int(p.get("opened_timestamp")),
        "entry_time": p.get("entry_time"),
        "exit_timestamp": now_ts(),
        "exit_time": iso_now(),
        "gross_pnl": gross,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees": entry_fee + exit_fee,
        "funding_paid": funding_paid,
        "net_pnl": net,
        "net_return_pct": net / margin * 100,
        "reason": reason,
        "planned_risk": safe_float(p.get("planned_risk")),
        "entry_signal_score": safe_int(p.get("entry_signal_score")),
        "entry_rsi": safe_float(p.get("entry_rsi"), 50),
        "build": BUILD,
    }

    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-MAX_TRADES_STORED:]

    if net < 0:
        state["consecutive_losses"] = (
            safe_int(state.get("consecutive_losses")) + 1
        )
    elif net > 0:
        state["consecutive_losses"] = 0

    if (
        safe_int(state.get("consecutive_losses"))
        >= MAX_CONSECUTIVE_LOSSES
    ):
        state["consecutive_loss_lock"] = True

    state["last_close_ts"] = now_ts()
    state["position"] = None
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")),
        safe_float(state.get("equity")),
    )
    return True


def apply_funding(state, funding, price):
    p = state.get("position")
    if not p:
        return

    now = now_ts()
    last = normalize_timestamp(
        p.get("last_funding_timestamp", now)
    )
    elapsed = max(0.0, min((now - last) / 3600.0, 24.0))

    if elapsed <= 0:
        p["last_funding_timestamp"] = now
        return

    notional = abs(
        safe_float(p.get("quantity")) * price
    )
    amount = notional * funding * elapsed / 8.0

    if p.get("side") == "SHORT":
        amount *= -1.0

    # Positive amount means the position pays funding.
    p["funding_paid"] = (
        safe_float(p.get("funding_paid")) - amount
    )
    state["funding_total"] = (
        safe_float(state.get("funding_total")) - amount
    )
    state["equity"] = safe_float(state.get("equity")) - amount
    p["last_funding_timestamp"] = now


def manage_position(state, candles, price, funding, atr_value):
    p = state.get("position")
    if not p:
        return

    apply_funding(state, funding, price)
    p = state.get("position")
    if not p:
        return

    side = p.get("side")
    entry = safe_float(p.get("entry_price"))
    stop = safe_float(p.get("stop_price"))
    target = safe_float(p.get("target_price"))
    risk_distance = max(
        safe_float(p.get("risk_distance")), 0.01
    )

    cursor = safe_int(state.get("last_candle_ts"))
    if cursor <= 0:
        state["last_candle_ts"] = candles[-1]["t"]
        cursor = candles[-1]["t"]

    for c in candles:
        if c["t"] <= cursor:
            continue

        high, low = c["h"], c["l"]

        if side == "LONG":
            if low <= stop:
                close_position(state, stop, "STOP_LOSS")
                return
            if high >= target:
                close_position(state, target, "TAKE_PROFIT")
                return
            favorable = high - entry
        else:
            if high >= stop:
                close_position(state, stop, "STOP_LOSS")
                return
            if low <= target:
                close_position(state, target, "TAKE_PROFIT")
                return
            favorable = entry - low

        if favorable >= risk_distance * TRAIL_ACTIVATION_R:
            p["runner_mode"] = True
            trail = max(
                atr_value * ATR_TRAIL_MULTIPLIER, 0.01
            )
            if side == "LONG":
                p["stop_price"] = max(stop, entry, high - trail)
            else:
                p["stop_price"] = min(stop, entry, low + trail)
            stop = safe_float(p.get("stop_price"))

    opened = normalize_timestamp(p.get("opened_timestamp"))
    if opened and now_ts() - opened >= MAX_HOLD_SECONDS:
        close_position(state, price, "MAX_HOLD_TIME")


def metrics(state):
    trades = [
        t for t in state.get("trades", [])
        if isinstance(t, dict) and "net_pnl" in t
    ]
    wins = [t for t in trades if safe_float(t.get("net_pnl")) > 0]
    losses = [t for t in trades if safe_float(t.get("net_pnl")) < 0]

    total = sum(safe_float(t.get("net_pnl")) for t in trades)
    win_sum = sum(safe_float(t.get("net_pnl")) for t in wins)
    loss_sum = abs(
        sum(safe_float(t.get("net_pnl")) for t in losses)
    )

    pf = win_sum / loss_sum if loss_sum else (
        999.0 if win_sum else 0.0
    )
    average_win = (
        win_sum / len(wins) if wins else 0.0
    )
    average_loss = (
        -loss_sum / len(losses) if losses else 0.0
    )

    today = utc_date()
    today_trades = [
        t for t in trades
        if str(t.get("exit_time", "")).startswith(today)
    ]

    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (
            len(wins) / len(trades) * 100 if trades else 0.0
        ),
        "total_net_pnl": total,
        "average_trade": total / len(trades) if trades else 0.0,
        "average_win": average_win,
        "average_loss": average_loss,
        "profit_factor": pf,
        "expectancy_per_trade": (
            total / len(trades) if trades else 0.0
        ),
        "best_trade": max(
            (safe_float(t.get("net_pnl")) for t in trades),
            default=0.0,
        ),
        "worst_trade": min(
            (safe_float(t.get("net_pnl")) for t in trades),
            default=0.0,
        ),
        "trades_today_utc": len(today_trades),
        "wins_today_utc": sum(
            1 for t in today_trades
            if safe_float(t.get("net_pnl")) > 0
        ),
        "losses_today_utc": sum(
            1 for t in today_trades
            if safe_float(t.get("net_pnl")) < 0
        ),
    }


def make_dashboard(
    state, price, funding, signal, market_status="OK"
):
    p = state.get("position")
    unreal = unrealized(p, price) if p else 0.0
    marked = safe_float(state.get("equity")) + unreal
    initial = max(
        safe_float(state.get("initial_equity")), 0.01
    )

    state["peak_equity"] = max(
        safe_float(state.get("peak_equity")), marked
    )
    risk = refresh_locks(state, marked)
    stats = metrics(state)

    history = state.setdefault("equity_history", [])
    history.append({
        "timestamp": iso_now(),
        "equity": safe_float(state.get("equity")),
        "marked_equity": marked,
        "price": price,
    })
    state["equity_history"] = history[-MAX_HISTORY_STORED:]

    position_view = dict(p) if p else None
    if position_view:
        position_view["unrealized_pnl"] = unreal

    return {
        "build": BUILD,
        "generated_at": iso_now(),
        "market_status": market_status,
        "price": price,
        "funding_rate": funding,
        "funding_rate_pct": funding * 100,
        "initial_equity": initial,
        "equity": safe_float(state.get("equity")),
        "marked_equity": marked,
        "realized_pnl": safe_float(state.get("realized_pnl")),
        "unrealized_pnl": unreal,
        "net_pnl": marked - initial,
        "net_pnl_pct": (marked - initial) / initial * 100,
        "position": position_view,
        "open_position": position_view,
        "has_position": bool(p),
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
        "timeframes": {
            "15m": signal.get("trend_15m"),
            "1h": signal.get("trend_1h"),
            "4h": signal.get("trend_4h"),
            "rsi": signal.get("rsi"),
            "atr": signal.get("atr"),
            "ema9": signal.get("ema9"),
            "ema21": signal.get("ema21"),
        },
        "config": {
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "min_margin_pct": MIN_MARGIN_FRACTION * 100,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100,
            "leverage": LEVERAGE,
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT * 100,
            "max_drawdown_limit_pct": MAX_DRAWDOWN_LIMIT * 100,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "min_signal_score": MIN_SIGNAL_SCORE,
            "min_score_edge": MIN_SCORE_EDGE,
            "fee_rate_per_side": FEE_RATE,
            "slippage_rate_per_side": SLIPPAGE_RATE,
            "target_entries_per_day": "10-20 (not guaranteed)",
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
        c15 = fetch_candles("15m")
        c1h = fetch_candles("1h")
        c4h = fetch_candles("4h")

        signal = make_signal(c15, c1h, c4h, funding)
        atr_value = max(
            safe_float(signal.get("atr"), price * 0.002),
            price * 0.0005,
        )

        if state.get("position"):
            manage_position(
                state, c15, price, funding, atr_value
            )

        state["last_candle_ts"] = max(
            normalize_timestamp(state.get("last_candle_ts")),
            safe_int(c15[-1]["t"]),
        )

        # Use marked equity so unrealized losses can trigger locks.
        current_unreal = (
            unrealized(state.get("position"), price)
            if state.get("position") else 0.0
        )
        marked = safe_float(state.get("equity")) + current_unreal
        locks = refresh_locks(state, marked)

        last_close = normalize_timestamp(
            state.get("last_close_ts")
        )
        now = now_ts()
        cooldown = (
            max(0, COOLDOWN_SECONDS - (now - last_close))
            if 0 < last_close <= now else 0
        )

        if state.get("position"):
            entry_status = "Position already open"
        elif locks["daily_loss_lock"]:
            entry_status = "Daily loss lock active"
        elif locks["drawdown_lock"]:
            entry_status = "Drawdown lock active"
        elif locks["consecutive_loss_lock"]:
            entry_status = "Consecutive-loss lock active"
        elif cooldown > 0:
            entry_status = f"Cooldown: {cooldown}s remaining"
        elif signal["direction"] in ("LONG", "SHORT"):
            ok, message = open_position(
                state, signal["direction"], price,
                atr_value, signal
            )
            entry_status = message if ok else (
                f"Entry skipped: {message}"
            )
        else:
            entry_status = "No entry: signal is WAIT"

        signal["entry_status"] = entry_status
        signal["cooldown_remaining_seconds"] = cooldown

        data = make_dashboard(
            state, price, funding, signal
        )

        write_json(STATE_FILE, state)
        write_json(DATA_FILE, data)

        print(f"BUILD: {BUILD}")
        print(f"PRICE: {price:.2f}")
        print(
            f"SIGNAL: {signal['direction']} / {signal['score']} "
            f"(long={signal['long_score']}, "
            f"short={signal['short_score']})"
        )
        print(f"ENTRY STATUS: {entry_status}")
        print(f"COOLDOWN REMAINING: {cooldown}s")
        print(f"EQUITY: {state['equity']:.6f}")
        print(f"MARKED EQUITY: {data['marked_equity']:.6f}")
        print(f"UNREALIZED PNL: {data['unrealized_pnl']:.6f}")
        print(f"CLOSED TRADES: {data['closed_trades']}")
        print(f"WIN RATE: {data['win_rate']:.2f}%")
        print(
            f"TRADES TODAY UTC: "
            f"{data['metrics']['trades_today_utc']}"
        )
        print(
            f"EXPECTANCY: "
            f"{data['metrics']['expectancy_per_trade']:.6f}"
        )
        print(
            f"PROFIT FACTOR: "
            f"{data['metrics']['profit_factor']:.4f}"
        )
        print(f"RISK PER TRADE: {RISK_PER_TRADE * 100:.2f}%")
        print(f"DAILY LOSS LOCK: {locks['daily_loss_lock']}")
        print(f"DRAWDOWN LOCK: {locks['drawdown_lock']}")
        print(
            f"CONSECUTIVE LOSS LOCK: "
            f"{locks['consecutive_loss_lock']}"
        )
        print("PAPER ONLY: True")
        print("LIVE TRADING ENABLED: False")

    except Exception as exc:
        state["last_error"] = f"{type(exc).__name__}: {exc}"
        state["build"] = BUILD

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
