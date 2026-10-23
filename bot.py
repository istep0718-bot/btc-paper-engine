
#!/usr/bin/env python3
"""BTC Paper Engine V15 - paper trading only."""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V15_2026-10-23"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0

# V15 settings
RISK_PER_TRADE = 0.010
MIN_MARGIN_FRACTION = 0.05
MAX_MARGIN_FRACTION = 0.50

DAILY_LOSS_LIMIT = 0.03
MAX_DRAWDOWN_LIMIT = 0.10
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_SECONDS = 3600
MAX_HOLD_SECONDS = 86400

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


def now_ts():
    return int(time.time())


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
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
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    temp.replace(path)


def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except (OSError, json.JSONDecodeError):
        return default


def post(payload):
    request = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine-V15/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_timestamp(value):
    """Normalize timestamps to Unix seconds; reject invalid future times."""
    ts = safe_int(value)
    # Milliseconds usually have 13 digits; seconds usually have 10.
    if ts > 100_000_000_000:
        ts //= 1000

    now = now_ts()
    # A close time far in the future is invalid; don't lock trading forever.
    if ts < 0 or ts > now + 60:
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
        "daily_date": str(datetime.now(timezone.utc).date()),
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
    state["equity"] = safe_float(state.get("equity"), INITIAL_EQUITY)
    state["peak_equity"] = max(
        safe_float(state.get("peak_equity"), state["equity"]),
        state["equity"],
    )
    state["realized_pnl"] = safe_float(state.get("realized_pnl"))
    state["funding_total"] = safe_float(state.get("funding_total"))

    old_trades = state.get("trades")
    if not isinstance(old_trades, list):
        old_trades = state.get("paper_trades", [])
    state["trades"] = old_trades if isinstance(old_trades, list) else []

    history = state.get("equity_history", [])
    state["equity_history"] = history if isinstance(history, list) else []

    position = state.get("position") or state.get("open_position")
    state["position"] = position if isinstance(position, dict) else None

    # Critical V15 fix: old state may contain milliseconds, not seconds.
    state["last_close_ts"] = normalize_timestamp(
        state.get("last_close_ts", 0)
    )
    state["last_candle_ts"] = normalize_timestamp(
        state.get("last_candle_ts", 0)
    )
    state["consecutive_losses"] = max(
        0, safe_int(state.get("consecutive_losses"))
    )
    state["consecutive_loss_lock"] = bool(
        state.get("consecutive_loss_lock", False)
    )
    state["daily_start_equity"] = safe_float(
        state.get("daily_start_equity"), state["equity"]
    )
    state["build"] = BUILD
    return state


def fetch_price_and_funding():
    mids = post({"type": "allMids"})
    price = safe_float(mids.get(COIN)) if isinstance(mids, dict) else 0
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
        print(f"Funding unavailable; using zero this run: {exc}")

    return price, funding


def fetch_candles(interval, limit=CANDLE_LIMIT):
    interval_ms = {
        "15m": 15 * 60 * 1000,
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
    }[interval]
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
        try:
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
            if c["t"] > 0 and c["c"] > 0:
                candles.append(c)
        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda c: c["t"])
    now_ms = int(time.time() * 1000)
    candles = [c for c in candles if c["t"] + interval_ms <= now_ms]

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
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
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
    if fast > slow * 1.001:
        return "UP"
    if fast < slow * 0.999:
        return "DOWN"
    return "SIDEWAYS"


def make_signal(c15, c1h, c4h, funding):
    values = [c["c"] for c in c15]
    e9 = ema(values, 9)
    e21 = ema(values, 21)
    rsi_value = calc_rsi(values)
    atr_value = calc_atr(c15)

    t15, t1h, t4h = trend(c15), trend(c1h), trend(c4h)
    long_score = 0
    short_score = 0

    if e9[-1] > e21[-1]:
        long_score += 2
    elif e9[-1] < e21[-1]:
        short_score += 2

    for t, weight in ((t15, 1), (t1h, 2), (t4h, 2)):
        if t == "UP":
            long_score += weight
        elif t == "DOWN":
            short_score += weight

    if 45 <= rsi_value <= 68:
        long_score += 1
    if 32 <= rsi_value <= 55:
        short_score += 1
    if rsi_value > 75:
        long_score -= 1
    if rsi_value < 25:
        short_score -= 1
    if funding > 0.0003:
        short_score += 1
    elif funding < -0.0003:
        long_score += 1

    direction = "WAIT"
    if long_score >= MIN_SIGNAL_SCORE and long_score > short_score:
        direction = "LONG"
    elif short_score >= MIN_SIGNAL_SCORE and short_score > long_score:
        direction = "SHORT"

    return {
        "direction": direction,
        "score": max(long_score, short_score),
        "long_score": long_score,
        "short_score": short_score,
        "rsi": round(rsi_value, 4),
        "atr": round(atr_value, 6),
        "ema9": round(e9[-1], 6),
        "ema21": round(e21[-1], 6),
        "trend_15m": t15,
        "trend_1h": t1h,
        "trend_4h": t4h,
        "timestamp": iso_now(),
        "reference_price": values[-1],
        "reasons": ["Multi-timeframe score; paper-only signal"],
    }


def drawdown(state, equity=None):
    value = safe_float(state.get("equity")) if equity is None else equity
    peak = max(safe_float(state.get("peak_equity")), value, 0.01)
    return max(0.0, (peak - value) / peak)


def refresh_locks(state):
    today = str(datetime.now(timezone.utc).date())
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_start_equity"] = safe_float(state.get("equity"))
        state["daily_loss_lock"] = False

    equity = safe_float(state.get("equity"))
    day_start = max(safe_float(state.get("daily_start_equity")), 0.01)
    daily_loss = max(0.0, (day_start - equity) / day_start)
    dd = drawdown(state)

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
        "consecutive_losses": safe_int(state.get("consecutive_losses")),
        "consecutive_loss_lock": bool(state.get("consecutive_loss_lock")),
        "risk_per_trade_pct": RISK_PER_TRADE * 100,
        "min_margin_pct": MIN_MARGIN_FRACTION * 100,
        "max_margin_pct": MAX_MARGIN_FRACTION * 100,
        "leverage": LEVERAGE,
        "paper_only": True,
        "live_trading_enabled": False,
    }


def unrealized(position, price):
    if not position:
        return 0.0
    qty = safe_float(position.get("quantity"))
    entry = safe_float(position.get("entry_price"))
    if position.get("side") == "LONG":
        return (price - entry) * qty
    return (entry - price) * qty


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
    qty_margin = equity * MAX_MARGIN_FRACTION * LEVERAGE / entry
    qty = min(qty_risk, qty_margin)
    notional = qty * entry
    margin = notional / LEVERAGE

    # Never force a bigger position to meet the minimum margin.
    if margin < equity * MIN_MARGIN_FRACTION:
        return None, (
            f"Risk-sized margin ${margin:.2f} is below the "
            f"${equity * MIN_MARGIN_FRACTION:.2f} minimum"
        )
    if qty <= 0 or qty * estimated_cost > risk_budget * 1.000001:
        return None, "Risk budget check failed"

    return {
        "quantity": qty,
        "notional": notional,
        "margin_used": margin,
        "risk_budget": risk_budget,
        "planned_risk": qty * estimated_cost,
    }, None


def open_position(state, side, price, atr_value, signal):
    slip = price * SLIPPAGE_RATE
    entry = price + slip if side == "LONG" else price - slip
    distance = max(atr_value * ATR_STOP_MULTIPLIER, price * 0.001)
    target_distance = max(
        atr_value * ATR_TARGET_MULTIPLIER,
        distance * MIN_REWARD_RISK,
    )

    stop = entry - distance if side == "LONG" else entry + distance
    target = (
        entry + target_distance if side == "LONG"
        else entry - target_distance
    )

    sizing, error = size_position(state, entry, stop)
    if error:
        return False, error

    entry_fee = sizing["notional"] * FEE_RATE
    state["equity"] = safe_float(state.get("equity")) - entry_fee
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
    exit_price = (
        price * (1 - SLIPPAGE_RATE) if side == "LONG"
        else price * (1 + SLIPPAGE_RATE)
    )
    gross = (
        (exit_price - entry) * qty if side == "LONG"
        else (entry - exit_price) * qty
    )
    entry_fee = safe_float(p.get("entry_fee"))
    exit_fee = abs(exit_price * qty) * FEE_RATE
    funding_paid = safe_float(p.get("funding_paid"))
    net = gross - entry_fee - exit_fee + funding_paid

    # Entry fee and funding were already deducted from account equity.
    state["equity"] = safe_float(state.get("equity")) + gross - exit_fee
    state["realized_pnl"] = safe_float(state.get("realized_pnl")) + net

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
        "build": BUILD,
    }
    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-1000:]

    state["consecutive_losses"] = (
        safe_int(state.get("consecutive_losses")) + 1 if net < 0 else 0
    )
    if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
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
    last = normalize_timestamp(p.get("last_funding_timestamp", now))
    elapsed = max(0.0, min((now - last) / 3600.0, 24.0))
    if elapsed <= 0:
        p["last_funding_timestamp"] = now
        return

    notional = abs(safe_float(p.get("quantity")) * price)
    amount = notional * funding * elapsed / 8.0
    if p.get("side") == "SHORT":
        amount *= -1.0

    # Positive amount means funding is paid by this position.
    p["funding_paid"] = safe_float(p.get("funding_paid")) - amount
    state["funding_total"] = safe_float(state.get("funding_total")) - amount
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
    risk_distance = max(safe_float(p.get("risk_distance")), 0.01)

    # If an older saved state has no cursor, start at the latest closed candle.
    # This avoids replaying the full historical candle list against a position.
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
            trail = max(atr_value * ATR_TRAIL_MULTIPLIER, 0.01)
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
    loss_sum = abs(sum(safe_float(t.get("net_pnl")) for t in losses))
    pf = win_sum / loss_sum if loss_sum else (999.0 if win_sum else 0.0)

    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
        "total_net_pnl": total,
        "average_trade": total / len(trades) if trades else 0.0,
        "profit_factor": pf,
        "best_trade": max(
            (safe_float(t.get("net_pnl")) for t in trades), default=0.0
        ),
        "worst_trade": min(
            (safe_float(t.get("net_pnl")) for t in trades), default=0.0
        ),
    }


def make_dashboard(state, price, funding, signal, market_status="OK"):
    p = state.get("position")
    unreal = unrealized(p, price) if p else 0.0
    marked = safe_float(state.get("equity")) + unreal
    initial = max(safe_float(state.get("initial_equity")), 0.01)
    state["peak_equity"] = max(safe_float(state.get("peak_equity")), marked)
    risk = refresh_locks(state)
    stats = metrics(state)

    history = state.setdefault("equity_history", [])
    history.append({
        "timestamp": iso_now(),
        "equity": safe_float(state.get("equity")),
        "marked_equity": marked,
        "price": price,
    })
    state["equity_history"] = history[-1000:]

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
            "fee_rate_per_side": FEE_RATE,
            "slippage_rate_per_side": SLIPPAGE_RATE,
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
        atr_value = safe_float(signal.get("atr"), price * 0.002)

        if state.get("position"):
            manage_position(state, c15, price, funding, atr_value)

        state["last_candle_ts"] = max(
            normalize_timestamp(state.get("last_candle_ts")),
            safe_int(c15[-1]["t"]),
        )

        locks = refresh_locks(state)
        last_close = normalize_timestamp(state.get("last_close_ts"))
        now = now_ts()

        # Defensive cooldown calculation: only valid Unix seconds are used.
        if last_close <= 0 or last_close > now:
            cooldown = 0
        else:
            cooldown = max(0, COOLDOWN_SECONDS - (now - last_close))

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
                state, signal["direction"], price, atr_value, signal
            )
            entry_status = message if ok else f"Entry skipped: {message}"
        else:
            entry_status = "No entry: signal is WAIT"

        signal["entry_status"] = entry_status
        signal["cooldown_remaining_seconds"] = cooldown

        data = make_dashboard(state, price, funding, signal)
        write_json(STATE_FILE, state)
        write_json(DATA_FILE, data)

        print(f"BUILD: {BUILD}")
        print(f"PRICE: {price:.2f}")
        print(
            f"SIGNAL: {signal['direction']} / {signal['score']} "
            f"(long={signal['long_score']}, short={signal['short_score']})"
        )
        print(f"ENTRY STATUS: {entry_status}")
        print(f"COOLDOWN REMAINING: {cooldown}s")
        print(f"EQUITY: {state['equity']:.6f}")
        print(f"MARKED EQUITY: {data['marked_equity']:.6f}")
        print(f"UNREALIZED PNL: {data['unrealized_pnl']:.6f}")
        print(f"CLOSED TRADES: {data['closed_trades']}")
        print(f"WIN RATE: {data['win_rate']:.2f}%")
        print(f"RISK PER TRADE: {RISK_PER_TRADE * 100:.2f}%")
        print(
            f"MARGIN TARGET: {MIN_MARGIN_FRACTION * 100:.0f}% - "
            f"{MAX_MARGIN_FRACTION * 100:.0f}%"
        )
        print(f"DAILY LOSS LOCK: {locks['daily_loss_lock']}")
        print(f"DRAWDOWN LOCK: {locks['drawdown_lock']}")
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
