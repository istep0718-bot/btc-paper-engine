
#!/usr/bin/env python3
"""BTC Paper Engine V9 — paper trading only; never sends live orders."""

import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V9_2026-10-22"
API = "https://api.hyperliquid.xyz/info"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_SECONDS = 3600
MAX_HOLD_SECONDS = 24 * 3600

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"


def now():
    return datetime.now(timezone.utc)


def iso(ts=None):
    return datetime.fromtimestamp(
        ts if ts is not None else time.time(), timezone.utc
    ).isoformat()


def request_json(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "BTC-Paper-Engine/9"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())


def candles(interval, count=240):
    end = int(time.time() * 1000)
    start = end - count * {
        "15m": 15 * 60_000,
        "1h": 60 * 60_000,
        "4h": 4 * 60 * 60_000,
    }[interval]
    raw = request_json({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": start,
            "endTime": end,
        },
    })
    result = []
    for c in raw:
        try:
            row = {
                "t": int(c["t"]),
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c["v"]),
            }
            # Exclude the candle that is still forming.
            if row["t"] + {
                "15m": 900_000, "1h": 3_600_000,
                "4h": 14_400_000
            }[interval] <= end:
                if all(math.isfinite(row[k]) for k in ("o", "h", "l", "c", "v")):
                    result.append(row)
        except (KeyError, TypeError, ValueError):
            continue
    result.sort(key=lambda x: x["t"])
    if len(result) < 110:
        raise RuntimeError(f"Insufficient closed {interval} candles: {len(result)}")
    return result


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = alpha * x + (1 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(rows, period=14):
    trs = []
    for i in range(1, len(rows)):
        cur, prev = rows[i], rows[i - 1]
        trs.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    if len(trs) < period:
        return None
    value = sum(trs[:period]) / period
    for x in trs[period:]:
        value = (value * (period - 1) + x) / period
    return value


def adx(rows, period=14):
    if len(rows) < period * 2 + 2:
        return None
    tr_list, plus_list, minus_list = [], [], []
    for i in range(1, len(rows)):
        cur, prev = rows[i], rows[i - 1]
        up = cur["h"] - prev["h"]
        down = prev["l"] - cur["l"]
        plus_list.append(up if up > down and up > 0 else 0.0)
        minus_list.append(down if down > up and down > 0 else 0.0)
        tr_list.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    dx = []
    for i in range(period, len(tr_list)):
        trv = sum(tr_list[i - period + 1:i + 1])
        if trv <= 0:
            continue
        p = 100 * sum(plus_list[i - period + 1:i + 1]) / trv
        m = 100 * sum(minus_list[i - period + 1:i + 1]) / trv
        dx.append(100 * abs(p - m) / max(p + m, 1e-9))
    if len(dx) < period:
        return None
    return sum(dx[-period:]) / period


def funding_rate():
    try:
        data = request_json({"type": "metaAndAssetCtxs"})
        contexts = data[1]
        meta = data[0]
        names = [x["name"] for x in meta["universe"]]
        idx = names.index("BTC")
        return float(contexts[idx].get("funding", 0.0))
    except Exception:
        return None


def analyze(rows):
    close = [x["c"] for x in rows]
    volume = [x["v"] for x in rows]
    price = close[-1]
    e20, e50, e100 = ema(close, 20), ema(close, 50), ema(close, 100)
    r = rsi(close)
    a = atr(rows)
    d = adx(rows)
    macd_line = (ema(close, 12) or price) - (ema(close, 26) or price)
    macd_series = []
    for i in range(26, len(close) + 1):
        macd_series.append(
            (ema(close[:i], 12) or close[i - 1]) -
            (ema(close[:i], 26) or close[i - 1])
        )
    macd_signal = ema(macd_series, 9) if len(macd_series) >= 9 else macd_line
    hist = macd_line - (macd_signal or macd_line)
    momentum = (price / close[-6] - 1) * 100 if close[-6] else 0
    avg_vol = sum(volume[-21:-1]) / max(len(volume[-21:-1]), 1)
    vol_ratio = volume[-1] / avg_vol if avg_vol > 0 else 1.0
    trend = "UP" if e20 > e50 > e100 else (
        "DOWN" if e20 < e50 < e100 else "SIDEWAYS"
    )
    return {
        "price": price, "ema20": e20, "ema50": e50, "ema100": e100,
        "rsi": r, "atr": a, "adx": d, "macd_hist": hist,
        "momentum_pct": momentum, "volume_ratio": vol_ratio,
        "trend": trend,
    }


def default_state():
    return {
        "build": BUILD, "equity": INITIAL_EQUITY,
        "initial_equity": INITIAL_EQUITY, "position": None,
        "trades": [], "equity_history": [],
        "last_exit_timestamp": 0, "consecutive_losses": 0,
        "day": now().date().isoformat(), "day_start_equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(s, dict):
            raise ValueError("State is not an object")
        s.setdefault("equity", INITIAL_EQUITY)
        s.setdefault("initial_equity", INITIAL_EQUITY)
        s.setdefault("position", None)
        s.setdefault("trades", [])
        s.setdefault("equity_history", [])
        s.setdefault("last_exit_timestamp", 0)
        s.setdefault("consecutive_losses", 0)
        s.setdefault("day", now().date().isoformat())
        s.setdefault("day_start_equity", s["equity"])
        s.setdefault("peak_equity", s["equity"])
        return s
    except Exception:
        backup = STATE_FILE.with_suffix(".corrupt.json")
        try:
            backup.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("state.json is invalid; preserved backup and stopped")


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def close_position(state, price, reason):
    p = state["position"]
    if not p:
        return
    side_sign = 1 if p["side"] == "LONG" else -1
    exit_fill = price * (1 - SLIPPAGE_RATE * side_sign)
    gross = (exit_fill - p["entry_price"]) * p["quantity"] * side_sign
    fees = (p["entry_price"] * p["quantity"] + exit_fill * p["quantity"]) * FEE_RATE
    net = gross - fees
    state["equity"] += net
    trade = {
        **p, "exit_price": exit_fill, "exit_time": iso(),
        "gross_pnl": round(gross, 8), "fees": round(fees, 8),
        "net_pnl": round(net, 8), "pnl": round(net, 8),
        "reason": reason,
    }
    state["trades"].append(trade)
    state["trades"] = state["trades"][-500:]
    state["last_exit_timestamp"] = time.time()
    state["consecutive_losses"] = (
        state["consecutive_losses"] + 1 if net < 0 else 0
    )
    state["position"] = None


def main():
    state = load_state()
    market = {}
    for tf in ("15m", "1h", "4h"):
        market[tf] = analyze(candles(tf))

    price = market["15m"]["price"]
    funding = funding_rate()
    sig = market["15m"]
    long_score = 0
    short_score = 0
    reasons = []

    for tf in ("15m", "1h", "4h"):
        if market[tf]["trend"] == "UP":
            long_score += 2
        elif market[tf]["trend"] == "DOWN":
            short_score += 2

    if sig["macd_hist"] > 0:
        long_score += 1
    elif sig["macd_hist"] < 0:
        short_score += 1

    if sig["momentum_pct"] > 0:
        long_score += 1
    elif sig["momentum_pct"] < 0:
        short_score += 1

    if sig["rsi"] is not None:
        if 50 <= sig["rsi"] <= 68:
            long_score += 1
        if 32 <= sig["rsi"] <= 50:
            short_score += 1

    if sig["volume_ratio"] >= 1.2:
        if sig["momentum_pct"] > 0:
            long_score += 1
        elif sig["momentum_pct"] < 0:
            short_score += 1

    if funding is not None:
        if funding > 0.0005:
            short_score += 1
        elif funding < -0.0005:
            long_score += 1

    direction = "WAIT"
    if long_score >= 5 and long_score >= short_score + 2:
        direction = "LONG"
    elif short_score >= 5 and short_score >= long_score + 2:
        direction = "SHORT"

    if market["1h"]["trend"] == market["4h"]["trend"] and \
       market["1h"]["trend"] in ("UP", "DOWN") and \
       (sig["adx"] or 0) >= 22:
        runner_mode = True
    else:
        runner_mode = False

    # Manage an existing paper position first.
    p = state["position"]
    if p:
        side_sign = 1 if p["side"] == "LONG" else -1
        risk = p["risk_distance"]
        favorable = (price - p["entry_price"]) * side_sign
        if favorable >= 1.5 * risk:
            trail = price - side_sign * 1.35 * risk
            if p["side"] == "LONG":
                p["stop_price"] = max(p["stop_price"], trail)
            else:
                p["stop_price"] = min(p["stop_price"], trail)

        held = time.time() - p["opened_timestamp"]
        stop_hit = price <= p["stop_price"] if p["side"] == "LONG" else price >= p["stop_price"]
        target_hit = p.get("target_price") is not None and (
            price >= p["target_price"] if p["side"] == "LONG"
            else price <= p["target_price"]
        )
        if stop_hit:
            close_position(state, p["stop_price"], "STOP_OR_TRAIL")
        elif target_hit:
            close_position(state, p["target_price"], "TAKE_PROFIT")
        elif held >= MAX_HOLD_SECONDS:
            close_position(state, price, "MAX_HOLD")
        elif direction not in ("WAIT", p["side"]) and \
             long_score >= 5 and short_score >= 5:
            close_position(state, price, "OPPOSITE_SIGNAL")

    today = now().date().isoformat()
    if state["day"] != today:
        state["day"] = today
        state["day_start_equity"] = state["equity"]
    state["peak_equity"] = max(state["peak_equity"], state["equity"])
    daily_loss = (state["day_start_equity"] - state["equity"]) / max(state["day_start_equity"], 1e-9)
    drawdown = (state["peak_equity"] - state["equity"]) / max(state["peak_equity"], 1e-9)

    can_open = (
        state["position"] is None and
        direction in ("LONG", "SHORT") and
        daily_loss < MAX_DAILY_LOSS and
        drawdown < MAX_DRAWDOWN and
        state["consecutive_losses"] < MAX_CONSECUTIVE_LOSSES and
        time.time() - state["last_exit_timestamp"] >= COOLDOWN_SECONDS
    )

    if can_open and sig["atr"] and sig["atr"] > 0:
        risk_distance = min(max(sig["atr"] * 1.6, price * 0.0015), price * 0.012)
        risk_cash = state["equity"] * RISK_PER_TRADE
        quantity_by_risk = risk_cash / risk_distance
        margin_cap = state["equity"] * MAX_MARGIN_FRACTION
        quantity_by_margin = margin_cap * LEVERAGE / price
        quantity = min(quantity_by_risk, quantity_by_margin)
        if quantity > 0 and math.isfinite(quantity):
            side_sign = 1 if direction == "LONG" else -1
            entry = price * (1 + SLIPPAGE_RATE * side_sign)
            stop = entry - side_sign * risk_distance
            target = None if runner_mode else entry + side_sign * 2.4 * risk_distance
            state["position"] = {
                "side": direction, "entry_price": entry,
                "quantity": quantity, "leverage": LEVERAGE,
                "margin_used": quantity * entry / LEVERAGE,
                "risk_distance": risk_distance, "stop_price": stop,
                "target_price": target, "opened_timestamp": time.time(),
                "entry_time": iso(), "runner_mode": runner_mode,
            }

    closed = state["trades"]
    wins = sum(1 for t in closed if t.get("net_pnl", 0) > 0)
    losses = sum(1 for t in closed if t.get("net_pnl", 0) < 0)
    gross_profit = sum(t.get("net_pnl", 0) for t in closed if t.get("net_pnl", 0) > 0)
    gross_loss = abs(sum(t.get("net_pnl", 0) for t in closed if t.get("net_pnl", 0) < 0))
    pf = gross_profit / gross_loss if gross_loss else (gross_profit if gross_profit else 0)
    unrealized = 0.0
    if state["position"]:
        p = state["position"]
        sign = 1 if p["side"] == "LONG" else -1
        unrealized = (price - p["entry_price"]) * p["quantity"] * sign
        unrealized -= (price * p["quantity"] + p["entry_price"] * p["quantity"]) * FEE_RATE

    state["equity_history"].append({
        "time": iso(), "equity": round(state["equity"], 8),
        "price": price,
    })
    state["equity_history"] = state["equity_history"][-500:]

    payload = {
        "build": BUILD, "generated_at": iso(),
        "price": price, "leverage": LEVERAGE,
        "funding_rate": funding,
        "funding_rate_pct": funding * 100 if funding is not None else None,
        "equity": round(state["equity"], 8),
        "initial_equity": state["initial_equity"],
        "realized_pnl": round(state["equity"] - state["initial_equity"], 8),
        "unrealized_pnl": round(unrealized, 8),
        "signal": {
            "direction": direction,
            "score": max(long_score, short_score),
            "long_score": long_score, "short_score": short_score,
            "reason": f"15m {sig['trend']}; 1h {market['1h']['trend']}; 4h {market['4h']['trend']}",
        },
        "rsi_15m": sig["rsi"],
        "atr_15m": sig["atr"], "adx_15m": sig["adx"],
        "macd_hist_15m": sig["macd_hist"],
        "momentum_pct": sig["momentum_pct"],
        "volume_ratio": sig["volume_ratio"],
        "timeframes": {
            tf: {"trend": market[tf]["trend"], **market[tf]}
            for tf in market
        },
        "metrics": {
            "trades": len(closed), "wins": wins, "losses": losses,
            "win_rate": wins / len(closed) * 100 if closed else 0,
            "profit_factor": pf,
            "max_drawdown_pct": drawdown * 100,
        },
        "win_rate": wins / len(closed) * 100 if closed else 0,
        "trade_count": len(closed), "profit_factor": pf,
        "max_drawdown_pct": drawdown * 100,
        "position": state["position"], "trades": closed[-20:],
        "equity_history": state["equity_history"],
        "paper_only": True, "live_orders_enabled": False,
    }

    save_json(STATE_FILE, state)
    save_json(DATA_FILE, payload)
    print(json.dumps({
        "build": BUILD, "price": price, "leverage": LEVERAGE,
        "signal": direction, "equity": state["equity"],
        "trades": len(closed), "paper_only": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        raise
