
#!/usr/bin/env python3
"""
BTC Paper Engine V8
Paper trading only. No wallet, no private keys, no live orders.
Python 3.12, standard library only.
"""

import json
import math
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V8_2026-10-22"
API = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 30.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_SECONDS = 60 * 60
MAX_HOLD_SECONDS = 24 * 60 * 60

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

CANDLE_INTERVALS = {
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
}
MIN_BARS = 100
MAX_HISTORY = 500
MAX_TRADES = 500


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def finite(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def clamp(value, low, high):
    return max(low, min(high, value))


def request_json(payload, retries=3):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine/8.0",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                if not isinstance(result, (dict, list)):
                    raise ValueError("Unexpected API response")
                return result
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Public market API failed: {last_error}")


def fetch_candles(interval, limit=MAX_HISTORY):
    end_ms = int(time.time() * 1000)
    seconds = {
        "15m": 900,
        "1h": 3600,
        "4h": 14400,
    }[interval]
    start_ms = end_ms - seconds * 1000 * limit

    rows = request_json({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    })

    if not isinstance(rows, list):
        raise ValueError(f"Invalid {interval} candle response")

    candles = []
    for row in rows:
        try:
            candle = {
                "time": int(row["t"]),
                "open": finite(row["o"], float("nan")),
                "high": finite(row["h"], float("nan")),
                "low": finite(row["l"], float("nan")),
                "close": finite(row["c"], float("nan")),
                "volume": finite(row.get("v", 0)),
            }
            if (
                all(math.isfinite(candle[k]) for k in
                    ("open", "high", "low", "close"))
                and candle["high"] >= candle["low"] > 0
                and candle["close"] > 0
                and candle["open"] > 0
            ):
                candles.append(candle)
        except (KeyError, TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x["time"])
    unique = {}
    for candle in candles:
        unique[candle["time"]] = candle
    candles = list(unique.values())

    if len(candles) < MIN_BARS:
        raise ValueError(
            f"Not enough {interval} candles: {len(candles)}"
        )

    # Exclude the currently forming candle to avoid look-ahead bias.
    candle_ms = seconds * 1000
    cutoff = int(time.time() * 1000) - candle_ms
    closed = [c for c in candles if c["time"] + candle_ms <= cutoff]

    if len(closed) < MIN_BARS:
        raise ValueError(f"Not enough closed {interval} candles")

    return closed[-limit:]


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def rsi(values, period=14):
    if len(values) <= period:
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
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(candles):
    result = []
    for i, candle in enumerate(candles):
        if i == 0:
            result.append(candle["high"] - candle["low"])
            continue
        previous_close = candles[i - 1]["close"]
        result.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - previous_close),
            abs(candle["low"] - previous_close),
        ))
    return result


def atr(candles, period=14):
    trs = true_ranges(candles)
    if len(trs) < period:
        return 0.0
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx(candles, period=14):
    if len(candles) < period * 2 + 2:
        return 0.0

    trs = true_ranges(candles)
    plus_dm = []
    minus_dm = []

    for i in range(1, len(candles)):
        up = candles[i]["high"] - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    tr_s = sum(trs[1:period + 1])
    plus_s = sum(plus_dm[:period])
    minus_s = sum(minus_dm[:period])
    dx_values = []

    for i in range(period, len(plus_dm)):
        if i > period:
            tr_s = tr_s - tr_s / period + trs[i + 1]
            plus_s = plus_s - plus_s / period + plus_dm[i]
            minus_s = minus_s - minus_s / period + minus_dm[i]

        if tr_s <= 0:
            dx_values.append(0.0)
            continue

        plus_di = 100.0 * plus_s / tr_s
        minus_di = 100.0 * minus_s / tr_s
        denom = plus_di + minus_di
        dx_values.append(
            100.0 * abs(plus_di - minus_di) / denom if denom else 0.0
        )

    if len(dx_values) < period:
        return 0.0
    value = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        value = (value * (period - 1) + dx) / period
    return value


def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)
    if fast is None or slow is None:
        return 0.0, 0.0, 0.0

    # Build MACD series to estimate the signal line.
    series = []
    for i in range(26, len(values) + 1):
        f = ema(values[:i], 12)
        s = ema(values[:i], 26)
        if f is not None and s is not None:
            series.append(f - s)

    signal = ema(series, 9) if len(series) >= 9 else 0.0
    line = fast - slow
    return line, signal or 0.0, line - (signal or 0.0)


def trend_info(candles):
    closes = [c["close"] for c in candles]
    price = closes[-1]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e100 = ema(closes, 100)
    direction = 0
    if e20 is not None and e50 is not None:
        if price > e20 > e50:
            direction = 1
        elif price < e20 < e50:
            direction = -1

    line, signal, hist = macd(closes)
    return {
        "direction": direction,
        "price": price,
        "ema20": e20,
        "ema50": e50,
        "ema100": e100,
        "rsi": rsi(closes),
        "atr": atr(candles),
        "adx": adx(candles),
        "macd": line,
        "macd_signal": signal,
        "macd_hist": hist,
        "momentum_pct": (
            (closes[-1] / closes[-5] - 1.0) * 100.0
            if len(closes) >= 5 and closes[-5] else 0.0
        ),
        "volume_ratio": (
            candles[-1]["volume"] /
            max(1e-12, sum(c["volume"] for c in candles[-21:-1]) / 20)
            if len(candles) >= 21 else 1.0
        ),
        "candle_time": candles[-1]["time"],
    }


def fetch_funding():
    result = request_json({"type": "metaAndAssetCtxs"})
    try:
        contexts = result[1]
        btc = contexts[0]
        # Hyperliquid funding is a per-hour rate expressed as a decimal.
        rate = finite(btc.get("funding", 0.0))
        return rate, rate * 100.0
    except (IndexError, KeyError, TypeError):
        return 0.0, 0.0


def signal_analysis(t15, t1h, t4h, funding_pct):
    long_score = 0
    short_score = 0

    if t15["direction"] == 1:
        long_score += 2
    elif t15["direction"] == -1:
        short_score += 2

    if t1h["direction"] == 1:
        long_score += 3
    elif t1h["direction"] == -1:
        short_score += 3

    if t4h["direction"] == 1:
        long_score += 3
    elif t4h["direction"] == -1:
        short_score += 3

    if t15["macd_hist"] > 0:
        long_score += 1
    elif t15["macd_hist"] < 0:
        short_score += 1

    if t15["momentum_pct"] > 0:
        long_score += 1
    elif t15["momentum_pct"] < 0:
        short_score += 1

    if 50 <= t15["rsi"] <= 68:
        long_score += 1
    elif 32 <= t15["rsi"] <= 50:
        short_score += 1

    if t15["volume_ratio"] >= 1.2:
        if t15["momentum_pct"] > 0:
            long_score += 1
        elif t15["momentum_pct"] < 0:
            short_score += 1

    # Avoid chasing extreme RSI.
    if t15["rsi"] >= 76:
        long_score -= 2
    if t15["rsi"] <= 24:
        short_score -= 2

    # Extreme funding is a caution, not a standalone signal.
    if funding_pct > 0.03:
        long_score -= 1
    elif funding_pct < -0.03:
        short_score -= 1

    long_score = max(0, long_score)
    short_score = max(0, short_score)

    aligned_long = t1h["direction"] == 1 and t4h["direction"] == 1
    aligned_short = t1h["direction"] == -1 and t4h["direction"] == -1

    direction = "WAIT"
    if aligned_long and long_score >= 7 and long_score >= short_score + 2:
        direction = "LONG"
    elif aligned_short and short_score >= 7 and short_score >= long_score + 2:
        direction = "SHORT"

    score = max(long_score, short_score)
    return {
        "direction": direction,
        "score": int(clamp(score * 100 / 12, 0, 100)),
        "long_score": long_score,
        "short_score": short_score,
        "reason": (
            f"15m RSI {t15['rsi']:.1f}, ADX {t15['adx']:.1f}; "
            f"1h trend {t1h['direction']}, 4h trend {t4h['direction']}"
        ),
        "runner_setup": (
            direction == "LONG" and aligned_long and t15["adx"] >= 22
        ) or (
            direction == "SHORT" and aligned_short and t15["adx"] >= 22
        ),
    }


def default_state():
    return {
        "build": BUILD,
        "initial_equity": INITIAL_EQUITY,
        "equity": INITIAL_EQUITY,
        "open_trade": None,
        "trades": [],
        "equity_history": [],
        "last_entry_candle": None,
        "last_exit_timestamp": 0,
        "consecutive_losses": 0,
        "day_start": datetime.now(timezone.utc).date().isoformat(),
        "day_start_equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            old = json.load(f)
        if not isinstance(old, dict):
            return default_state()
    except (OSError, json.JSONDecodeError):
        # Preserve the damaged file rather than silently overwriting it.
        backup = STATE_FILE.with_suffix(".corrupt.json")
        try:
            if not backup.exists():
                backup.write_text(
                    STATE_FILE.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
        except OSError:
            pass
        return default_state()

    state = default_state()
    state.update(old)
    state["build"] = BUILD
    state["initial_equity"] = finite(
        old.get("initial_equity"), INITIAL_EQUITY
    )
    state["equity"] = finite(old.get("equity"), state["initial_equity"])
    state["trades"] = old.get("trades", old.get("closed_trades", []))
    if not isinstance(state["trades"], list):
        state["trades"] = []
    state["equity_history"] = old.get("equity_history", [])
    if not isinstance(state["equity_history"], list):
        state["equity_history"] = []

    # Keep any existing open position; don't reset paper history on version change.
    if "open_trade" not in old:
        state["open_trade"] = old.get("position")

    state["consecutive_losses"] = int(
        finite(old.get("consecutive_losses"), 0)
    )
    state["peak_equity"] = max(
        finite(old.get("peak_equity"), state["equity"]),
        state["equity"],
    )
    return state


def save_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temp.replace(path)


def current_drawdown_pct(state):
    peak = max(1e-9, finite(state.get("peak_equity"), INITIAL_EQUITY))
    return max(0.0, (peak - state["equity"]) / peak * 100.0)


def day_loss_pct(state):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day_start") != today:
        state["day_start"] = today
        state["day_start_equity"] = state["equity"]
    start = max(1e-9, finite(state.get("day_start_equity"), state["equity"]))
    return max(0.0, (start - state["equity"]) / start)


def calc_metrics(state):
    trades = state["trades"]
    pnls = [finite(t.get("net_pnl", t.get("pnl", 0))) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    peak = 0.0
    max_dd = 0.0
    for row in state["equity_history"]:
        eq = finite(row.get("equity"), state["initial_equity"])
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak * 100.0)

    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(pnls) * 100 if pnls else 0.0,
        "gross_profit": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": gross_win / gross_loss if gross_loss else (
            999.0 if gross_win else 0.0
        ),
        "net_pnl": sum(pnls),
        "return_pct": (
            (state["equity"] / state["initial_equity"] - 1) * 100
            if state["initial_equity"] else 0.0
        ),
        "max_drawdown_pct": max(max_dd, current_drawdown_pct(state)),
        "consecutive_losses": state["consecutive_losses"],
    }


def open_trade(state, price, signal, t15, t1h, t4h):
    equity = finite(state["equity"])
    if equity <= 0 or price <= 0 or t15["atr"] <= 0:
        return

    side = signal["direction"]
    if side not in ("LONG", "SHORT"):
        return

    stop_distance = max(t15["atr"] * 1.8, price * 0.002)
    stop_distance = min(stop_distance, price * 0.012)

    risk_dollars = equity * RISK_PER_TRADE
    qty_by_risk = risk_dollars / stop_distance
    margin_cap = equity * MAX_MARGIN_FRACTION
    qty_by_margin = margin_cap * LEVERAGE / price
    qty = min(qty_by_risk, qty_by_margin)

    if not math.isfinite(qty) or qty <= 0:
        return

    entry = price * (1 + SLIPPAGE_RATE if side == "LONG"
                     else 1 - SLIPPAGE_RATE)
    stop = entry - stop_distance if side == "LONG" else entry + stop_distance
    aligned = (
        (side == "LONG" and t1h["direction"] == 1 and t4h["direction"] == 1)
        or (side == "SHORT" and t1h["direction"] == -1
            and t4h["direction"] == -1)
    )
    runner = bool(signal.get("runner_setup") and aligned)

    state["open_trade"] = {
        "side": side,
        "entry_price": entry,
        "mark_entry_price": price,
        "quantity": qty,
        "stop_price": stop,
        "initial_stop_price": stop,
        "target_price": None if runner else (
            entry + stop_distance * 2.4 if side == "LONG"
            else entry - stop_distance * 2.4
        ),
        "runner_mode": runner,
        "trail_active": False,
        "trail_extreme": entry,
        "entry_time": now_iso(),
        "entry_timestamp": time.time(),
        "entry_candle": t15["candle_time"],
        "risk_dollars": risk_dollars,
        "entry_fee": abs(entry * qty) * FEE_RATE,
        "entry_slippage": abs(entry - price) * qty,
        "entry_score": signal["score"],
        "entry_rsi": t15["rsi"],
        "entry_adx": t15["adx"],
        "entry_atr": t15["atr"],
        "trend_1h": t1h["direction"],
        "trend_4h": t4h["direction"],
        "mfe": 0.0,
        "mae": 0.0,
    }
    state["last_entry_candle"] = t15["candle_time"]


def close_trade(state, exit_price, reason):
    trade = state.get("open_trade")
    if not isinstance(trade, dict):
        return

    side = trade.get("side")
    qty = finite(trade.get("quantity"))
    entry = finite(trade.get("entry_price"))
    if side not in ("LONG", "SHORT") or qty <= 0 or entry <= 0:
        state["open_trade"] = None
        return

    mark = max(0.01, finite(exit_price, entry))
    exit_fill = mark * (1 - SLIPPAGE_RATE if side == "LONG"
                        else 1 + SLIPPAGE_RATE)
    gross = (exit_fill - entry) * qty
    if side == "SHORT":
        gross = (entry - exit_fill) * qty

    exit_fee = abs(exit_fill * qty) * FEE_RATE
    entry_fee = finite(trade.get("entry_fee"))
    net = gross - entry_fee - exit_fee

    closed = dict(trade)
    closed.update({
        "exit_price": exit_fill,
        "mark_exit_price": mark,
        "exit_time": now_iso(),
        "exit_timestamp": time.time(),
        "gross_pnl": gross,
        "exit_fee": exit_fee,
        "net_pnl": net,
        "pnl": net,
        "reason": reason,
        "leverage": LEVERAGE,
        "mode": "PAPER ONLY",
    })

    state["equity"] = finite(state["equity"]) + net
    if net < 0:
        state["consecutive_losses"] += 1
    else:
        state["consecutive_losses"] = 0

    state["trades"].append(closed)
    state["trades"] = state["trades"][-MAX_TRADES:]
    state["last_exit_timestamp"] = time.time()
    state["peak_equity"] = max(
        finite(state.get("peak_equity"), state["equity"]),
        state["equity"],
    )
    state["open_trade"] = None


def manage_open_trade(state, candles, price):
    trade = state.get("open_trade")
    if not isinstance(trade, dict):
        return

    if not candles or price <= 0:
        return

    side = trade.get("side")
    if side not in ("LONG", "SHORT"):
        state["open_trade"] = None
        return

    candle = candles[-1]
    high = candle["high"]
    low = candle["low"]
    entry = finite(trade.get("entry_price"))
    stop = finite(trade.get("stop_price"), entry)
    initial_stop = finite(trade.get("initial_stop_price"), stop)
    risk = abs(entry - initial_stop)
    if entry <= 0 or risk <= 0:
        close_trade(state, price, "無効なポジション保護")
        return

    if side == "LONG":
        favorable = max(finite(trade.get("trail_extreme"), entry), high)
        favorable_move = favorable - entry
        adverse_move = max(0.0, entry - low)
    else:
        favorable = min(finite(trade.get("trail_extreme"), entry), low)
        favorable_move = entry - favorable
        adverse_move = max(0.0, high - entry)

    trade["trail_extreme"] = favorable
    trade["mfe"] = max(finite(trade.get("mfe")), favorable_move)
    trade["mae"] = max(finite(trade.get("mae")), adverse_move)

    # Conservative intrabar assumption: if both stop and target are touched,
    # the stop is assumed to have executed first.
    target = trade.get("target_price")
    target = finite(target, float("nan")) if target is not None else None

    if side == "LONG":
        if low <= stop:
            close_trade(state, stop, "損切り/トレーリング")
            return
        if target is not None and math.isfinite(target) and high >= target:
            close_trade(state, target, "固定利確")
            return
    else:
        if high >= stop:
            close_trade(state, stop, "損切り/トレーリング")
            return
        if target is not None and math.isfinite(target) and low <= target:
            close_trade(state, target, "固定利確")
            return

    # Start trailing once price has moved 1.5R in a favorable direction.
    if favorable_move >= risk * 1.5:
        trade["trail_active"] = True

    if trade.get("trail_active"):
        # Wider ATR-like trail to allow strong trends to continue.
        trail_distance = max(risk * 1.35, price * 0.0015)
        if side == "LONG":
            new_stop = favorable - trail_distance
            trade["stop_price"] = max(stop, new_stop)
        else:
            new_stop = favorable + trail_distance
            trade["stop_price"] = min(stop, new_stop)

    age = time.time() - finite(trade.get("entry_timestamp"), time.time())
    if age >= MAX_HOLD_SECONDS:
        close_trade(state, price, "最大保有時間")
        return

    # Persisted stop only moves in the protective direction.
    state["open_trade"] = trade


def get_unrealized(trade, price):
    if not isinstance(trade, dict) or price <= 0:
        return 0.0
    entry = finite(trade.get("entry_price"))
    qty = finite(trade.get("quantity"))
    side = trade.get("side")
    if side == "LONG":
        gross = (price - entry) * qty
    elif side == "SHORT":
        gross = (entry - price) * qty
    else:
        return 0.0
    estimated_exit_fee = abs(price * qty) * FEE_RATE
    return gross - estimated_exit_fee - finite(trade.get("entry_fee"))


def record_equity(state, price):
    trade = state.get("open_trade")
    unreal = get_unrealized(trade, price) if trade else 0.0
    row = {
        "timestamp": now_iso(),
        "equity": round(state["equity"] + unreal, 8),
        "realized_equity": round(state["equity"], 8),
        "unrealized_pnl": round(unreal, 8),
        "price": round(price, 2),
    }
    history = state.get("equity_history", [])
    if not isinstance(history, list):
        history = []
    history.append(row)
    state["equity_history"] = history[-MAX_HISTORY:]


def build_dashboard(state, price, funding_rate, funding_pct,
                    t15, t1h, t4h, signal, api_error=None):
    trade = state.get("open_trade")
    unreal = get_unrealized(trade, price) if trade else 0.0
    metrics = calc_metrics(state)
    position = None

    if isinstance(trade, dict):
        position = {
            "side": trade.get("side"),
            "direction": trade.get("side"),
            "entry_price": trade.get("entry_price"),
            "quantity": trade.get("quantity"),
            "size": trade.get("quantity"),
            "stop_price": trade.get("stop_price"),
            "target_price": trade.get("target_price"),
            "runner_mode": trade.get("runner_mode", False),
            "trail_active": trade.get("trail_active", False),
            "unrealized_pnl": unreal,
            "entry_time": trade.get("entry_time"),
            "leverage": LEVERAGE,
        }

    recent = list(reversed(state["trades"][-50:]))
    payload = {
        "build": BUILD,
        "version": BUILD,
        "generated_at": now_iso(),
        "mode": "PAPER ONLY",
        "live_trading": False,
        "wallet_connected": False,
        "leverage": LEVERAGE,
        "price": price,
        "current_price": price,
        "funding_rate": funding_rate,
        "funding_rate_pct": funding_pct,
        "initial_equity": state["initial_equity"],
        "equity": state["equity"],
        "realized_pnl": state["equity"] - state["initial_equity"],
        "unrealized_pnl": unreal,
        "net_pnl": state["equity"] - state["initial_equity"] + unreal,
        "position": position,
        "open_position": position,
        "signal": signal,
        "score": signal["score"],
        "reason": signal["reason"],
        "rsi": t15["rsi"],
        "rsi_15m": t15["rsi"],
        "adx_15m": t15["adx"],
        "atr_15m": t15["atr"],
        "macd_15m": t15["macd"],
        "macd_signal_15m": t15["macd_signal"],
        "macd_hist_15m": t15["macd_hist"],
        "momentum_15m_pct": t15["momentum_pct"],
        "volume_ratio_15m": t15["volume_ratio"],
        "timeframes": {
            "15m": {**t15, "label": "15m"},
            "1h": {**t1h, "label": "1h"},
            "4h": {**t4h, "label": "4h"},
        },
        "trend_15m": t15["direction"],
        "trend_1h": t1h["direction"],
        "trend_4h": t4h["direction"],
        "trends": {
            "15m": t15["direction"],
            "1h": t1h["direction"],
            "4h": t4h["direction"],
        },
        "metrics": metrics,
        "closed_trades": len(state["trades"]),
        "trades": recent,
        "paper_trades": recent,
        "equity_history": state["equity_history"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "profit_factor": metrics["profit_factor"],
        "api_error": api_error,
        "risk": {
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100,
            "max_daily_loss_pct": MAX_DAILY_LOSS * 100,
            "max_drawdown_pct": MAX_DRAWDOWN * 100,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
        },
    }
    return payload


def main():
    state = load_state()
    api_error = None

    try:
        c15 = fetch_candles(CANDLE_INTERVALS["15m"])
        c1h = fetch_candles(CANDLE_INTERVALS["1h"])
        c4h = fetch_candles(CANDLE_INTERVALS["4h"])

        t15 = trend_info(c15)
        t1h = trend_info(c1h)
        t4h = trend_info(c4h)
        price = t15["price"]
        funding_rate, funding_pct = fetch_funding()
        signal = signal_analysis(t15, t1h, t4h, funding_pct)

        # Manage an existing paper position before considering a new one.
        if state.get("open_trade"):
            manage_open_trade(state, c15, price)

        # Daily loss, total drawdown and consecutive-loss guards.
        can_open = (
            state.get("open_trade") is None
            and day_loss_pct(state) < MAX_DAILY_LOSS
            and current_drawdown_pct(state) < MAX_DRAWDOWN
            and int(state.get("consecutive_losses", 0))
                < MAX_CONSECUTIVE_LOSSES
            and time.time() - finite(state.get("last_exit_timestamp"), 0)
                >= COOLDOWN_SECONDS
            and state.get("last_entry_candle") != t15["candle_time"]
        )

        if can_open and signal["direction"] in ("LONG", "SHORT"):
            open_trade(state, price, signal, t15, t1h, t4h)

        record_equity(state, price)
        payload = build_dashboard(
            state, price, funding_rate, funding_pct,
            t15, t1h, t4h, signal,
        )

    except Exception as exc:
        # Fail closed: never open a new position on incomplete market data.
        api_error = f"{type(exc).__name__}: {exc}"
        trade = state.get("open_trade")
        last_price = (
            finite(trade.get("mark_entry_price"), 0.0)
            if isinstance(trade, dict) else 0.0
        )
        # Preserve existing state/history if public market data is unavailable.
        fallback = {
            "build": BUILD,
            "version": BUILD,
            "generated_at": now_iso(),
            "mode": "PAPER ONLY",
            "live_trading": False,
            "wallet_connected": False,
            "leverage": LEVERAGE,
            "price": last_price,
            "current_price": last_price,
            "equity": state["equity"],
            "initial_equity": state["initial_equity"],
            "realized_pnl": state["equity"] - state["initial_equity"],
            "unrealized_pnl": get_unrealized(trade, last_price),
            "position": trade,
            "open_position": trade,
            "signal": {
                "direction": "WAIT",
                "score": 0,
                "long_score": 0,
                "short_score": 0,
                "reason": "市場データ取得エラー。新規取引を停止中。",
            },
            "trades": list(reversed(state["trades"][-50:])),
            "paper_trades": list(reversed(state["trades"][-50:])),
            "closed_trades": len(state["trades"]),
            "equity_history": state["equity_history"],
            "metrics": calc_metrics(state),
            "max_drawdown_pct": current_drawdown_pct(state),
            "api_error": api_error,
        }
        payload = fallback

    # Always persist the state and dashboard, including API error status.
    save_json_atomic(STATE_FILE, state)
    save_json_atomic(DATA_FILE, payload)
    print(json.dumps({
        "build": BUILD,
        "status": "ok" if not api_error else "market_data_error",
        "price": payload.get("price"),
        "equity": state.get("equity"),
        "leverage": LEVERAGE,
        "signal": payload.get("signal", {}).get("direction", "WAIT"),
        "closed_trades": len(state.get("trades", [])),
        "error": api_error,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
