#!/usr/bin/env python3
"""
BTC PAPER ENGINE V7
Paper trading only. No wallet, private key, or live orders.
Uses Hyperliquid public market-data endpoints and writes state.json + docs/data.json.
Designed for GitHub Actions Free: one compact run, no external packages.
"""
import json
import math
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V7_2026-10-21"
API = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
DATA_PATH = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
FEE_RATE = 0.0005       # per side
SLIPPAGE_RATE = 0.0002  # per side
RISK_PER_TRADE = 0.005  # 0.5% of equity at stop
MAX_MARGIN_FRACTION = 0.10
MAX_VIRTUAL_LEVERAGE = 30.0
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_MINUTES = 60
MAX_HOLD_HOURS = 24
MIN_BARS = 60
REQUEST_TIMEOUT = 12
USER_AGENT = "BTC-Paper-Engine-V7/1.0"

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None

def request_json(payload):
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))

def get_candles(interval, count=180):
    end_ms = int(time.time() * 1000)
    minutes = {"15m": 15, "1h": 60, "4h": 240}[interval]
    start_ms = end_ms - minutes * 60_000 * count
    rows = request_json({
        "type": "candleSnapshot",
        "req": {"coin": "BTC", "interval": interval, "startTime": start_ms, "endTime": end_ms},
    })
    candles = []
    for x in rows if isinstance(rows, list) else []:
        try:
            candles.append({
                "time": int(x["t"]),
                "open": float(x["o"]),
                "high": float(x["h"]),
                "low": float(x["l"]),
                "close": float(x["c"]),
                "volume": float(x.get("v", 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["time"])
    # Deduplicate candle timestamps.
    unique = {}
    for c in candles:
        unique[c["time"]] = c
    return list(unique.values())

def get_market_context():
    meta = request_json({"type": "metaAndAssetCtxs"})
    contexts = meta[1] if isinstance(meta, list) and len(meta) > 1 else []
    funding = None
    mark_price = None
    if contexts:
        ctx = contexts[0]
        try:
            funding = float(ctx.get("funding"))
        except (TypeError, ValueError):
            pass
        for key in ("markPx", "midPx", "oraclePx"):
            try:
                if ctx.get(key) is not None:
                    mark_price = float(ctx[key])
                    break
            except (TypeError, ValueError):
                pass
    return funding, mark_price

def ema(values, period):
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(float(value) * alpha + out[-1] * (1.0 - alpha))
    return out

def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    tr = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        tr.append(max(c["high"] - c["low"], abs(c["high"] - prev["close"]), abs(c["low"] - prev["close"])))
    value = sum(tr[:period]) / period
    for x in tr[period:]:
        value = (value * (period - 1) + x) / period
    return value

def pct_change(a, b):
    return ((a / b) - 1.0) * 100.0 if b else 0.0

def get_signal(c15, c1h, c4h):
    closes15 = [x["close"] for x in c15]
    closes1h = [x["close"] for x in c1h]
    closes4h = [x["close"] for x in c4h]
    price = closes15[-1]
    e9_15, e21_15 = ema(closes15, 9), ema(closes15, 21)
    e9_1h, e21_1h, e50_1h = ema(closes1h, 9), ema(closes1h, 21), ema(closes1h, 50)
    e20_4h, e50_4h = ema(closes4h, 20), ema(closes4h, 50)
    r = rsi(closes15, 14)
    a = atr(c15, 14)
    mom3 = pct_change(closes15[-1], closes15[-4])
    mom12 = pct_change(closes15[-1], closes15[-13])
    trend1h = 1 if e9_1h[-1] > e21_1h[-1] > e50_1h[-1] else (-1 if e9_1h[-1] < e21_1h[-1] < e50_1h[-1] else 0)
    trend4h = 1 if e20_4h[-1] > e50_4h[-1] else (-1 if e20_4h[-1] < e50_4h[-1] else 0)
    trend15 = 1 if e9_15[-1] > e21_15[-1] else (-1 if e9_15[-1] < e21_15[-1] else 0)

    long_score = 0
    short_score = 0
    reasons = []
    if trend1h == 1: long_score += 2; reasons.append("1時間足のEMAは上向き")
    elif trend1h == -1: short_score += 2; reasons.append("1時間足のEMAは下向き")
    if trend4h == 1: long_score += 2; reasons.append("4時間足のトレンドは上向き")
    elif trend4h == -1: short_score += 2; reasons.append("4時間足のトレンドは下向き")
    if trend15 == 1: long_score += 1
    elif trend15 == -1: short_score += 1
    if mom3 > 0.08: long_score += 1
    elif mom3 < -0.08: short_score += 1
    if mom12 > 0.15: long_score += 1
    elif mom12 < -0.15: short_score += 1
    if r is not None:
        if 42 <= r <= 68: long_score += 1
        if 32 <= r <= 58: short_score += 1
        if r > 78: long_score -= 2; reasons.append("RSI過熱のためロングを抑制")
        if r < 22: short_score -= 2; reasons.append("RSI売られ過ぎのためショートを抑制")
    # Avoid chasing very extended moves; retain signals in normal volatility.
    atr_pct = (a / price * 100.0) if a and price else 0.0
    if atr_pct > 1.8:
        long_score -= 1
        short_score -= 1
        reasons.append("15分足の変動が大きいためスコアを抑制")

    direction = "WAIT"
    score = max(long_score, short_score)
    # 1h alignment plus at least one supporting timeframe/filter. Less restrictive
    # than a full 4h+1h+15m unanimity to allow more paper-trade opportunities.
    if long_score >= 4 and trend1h == 1 and long_score >= short_score + 2:
        direction = "LONG"
    elif short_score >= 4 and trend1h == -1 and short_score >= long_score + 2:
        direction = "SHORT"

    return {
        "direction": direction,
        "score": int(score),
        "long_score": int(long_score),
        "short_score": int(short_score),
        "reasons": reasons or ["複数時間足の条件が十分に一致していません"],
        "rsi_15m": round(r, 2) if r is not None else None,
        "atr_15m": round(a, 4) if a is not None else None,
        "atr_pct_15m": round(atr_pct, 4),
        "momentum_3bar_pct": round(mom3, 4),
        "momentum_12bar_pct": round(mom12, 4),
        "trend_1h": trend1h,
        "trend_4h": trend4h,
        "trend_15m": trend15,
        "ema_9_15m": round(e9_15[-1], 2),
        "ema_21_15m": round(e21_15[-1], 2),
        "ema_9_1h": round(e9_1h[-1], 2),
        "ema_21_1h": round(e21_1h[-1], 2),
    }

def default_state():
    return {
        "build": BUILD,
        "initial_equity_usd": INITIAL_EQUITY,
        "cash_usd": INITIAL_EQUITY,
        "open_trade": None,
        "recent_trades": [],
        "equity_history": [],
        "day_start_equity_usd": INITIAL_EQUITY,
        "day_key": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "peak_equity_usd": INITIAL_EQUITY,
        "consecutive_losses": 0,
        "last_close_time": None,
        "last_processed_candle": None,
        "last_price": None,
        "last_funding_rate": None,
    }

def load_state():
    if not STATE_PATH.exists():
        return default_state()
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        # Preserve the damaged file for inspection; do not silently overwrite it.
        backup = STATE_PATH.with_name("state_corrupt_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + ".json")
        try:
            STATE_PATH.replace(backup)
        except OSError:
            pass
        return default_state()

    # Migrate V6 state without wiping the account/history.
    state = default_state()
    state["initial_equity_usd"] = float(old.get("initial_equity_usd", INITIAL_EQUITY))
    state["cash_usd"] = float(old.get("cash_usd", old.get("cash", state["initial_equity_usd"])))
    state["open_trade"] = old.get("open_trade")
    state["recent_trades"] = old.get("recent_trades", old.get("trades", [])) or []
    state["equity_history"] = old.get("equity_history", []) or []
    state["day_start_equity_usd"] = float(old.get("day_start_equity_usd", old.get("day_start_equity", state["initial_equity_usd"])))
    state["day_key"] = old.get("day_key", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    state["peak_equity_usd"] = float(old.get("peak_equity_usd", old.get("peak_equity", state["initial_equity_usd"])))
    state["consecutive_losses"] = int(old.get("consecutive_losses", 0))
    state["last_close_time"] = old.get("last_close_time")
    state["last_processed_candle"] = old.get("last_processed_candle")
    state["last_price"] = old.get("last_price")
    state["last_funding_rate"] = old.get("last_funding_rate")
    state["build"] = BUILD
    return state

def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)

def trade_pnl(trade, exit_price):
    side = 1.0 if trade["direction"] == "LONG" else -1.0
    qty = float(trade["qty_btc"])
    entry = float(trade["entry_price"])
    gross = (exit_price - entry) * qty * side
    exit_fee = abs(exit_price * qty) * FEE_RATE
    exit_slippage = abs(exit_price * qty) * SLIPPAGE_RATE
    return gross - float(trade.get("entry_fee_usd", 0.0)) - float(trade.get("entry_slippage_usd", 0.0)) - exit_fee - exit_slippage

def equity_of(state, price):
    cash = float(state["cash_usd"])
    t = state.get("open_trade")
    if not t:
        return cash
    # Cash already has margin removed on entry. Add reserved margin plus unrealized PnL.
    margin = float(t.get("margin_usd", 0.0))
    side = 1.0 if t["direction"] == "LONG" else -1.0
    gross = (price - float(t["entry_price"])) * float(t["qty_btc"]) * side
    close_cost = abs(price * float(t["qty_btc"])) * (FEE_RATE + SLIPPAGE_RATE)
    unrealized = gross - float(t.get("entry_fee_usd", 0)) - float(t.get("entry_slippage_usd", 0)) - close_cost
    return cash + margin + unrealized

def close_trade(state, price, reason, stamp):
    t = state.get("open_trade")
    if not t:
        return
    pnl = trade_pnl(t, price)
    state["cash_usd"] = float(state["cash_usd"]) + float(t.get("margin_usd", 0.0)) + pnl
    item = dict(t)
    item.update({
        "exit_price": round(price, 2),
        "closed_at": stamp,
        "net_pnl_usd": round(pnl, 8),
        "close_reason": reason,
    })
    state["recent_trades"].append(item)
    state["recent_trades"] = state["recent_trades"][-300:]
    state["open_trade"] = None
    state["last_close_time"] = stamp
    if pnl < 0:
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
    else:
        state["consecutive_losses"] = 0

def can_open(state, equity, stamp):
    dt = parse_dt(stamp)
    day_key = dt.strftime("%Y-%m-%d") if dt else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_key") != day_key:
        state["day_key"] = day_key
        state["day_start_equity_usd"] = equity
    day_start = max(float(state.get("day_start_equity_usd", equity)), 1e-9)
    daily_loss = (day_start - equity) / day_start
    peak = max(float(state.get("peak_equity_usd", equity)), 1e-9)
    drawdown = (peak - equity) / peak
    if daily_loss >= MAX_DAILY_LOSS:
        return False, "日次損失上限に到達"
    if drawdown >= MAX_DRAWDOWN:
        return False, "最大ドローダウン上限に到達"
    if int(state.get("consecutive_losses", 0)) >= MAX_CONSECUTIVE_LOSSES:
        last = parse_dt(state.get("last_close_time"))
        if last and dt and (dt - last).total_seconds() < COOLDOWN_MINUTES * 60:
            return False, "連敗クールダウン中"
        # Once cooldown has passed, allow a new trade but reset the streak.
        state["consecutive_losses"] = 0
    last = parse_dt(state.get("last_close_time"))
    if last and dt and (dt - last).total_seconds() < 15 * 60:
        return False, "決済後のクールダウン中"
    return True, "稼働中"

def open_trade(state, direction, price, atr_value, equity, stamp, candle_time):
    if not atr_value or atr_value <= 0 or price <= 0:
        return False
    # Stop distance is volatility-adjusted, bounded to avoid microscopic/huge stops.
    stop_dist = max(atr_value * 1.6, price * 0.0015)
    stop_dist = min(stop_dist, price * 0.012)
    target_dist = stop_dist * 2.4
    risk_dollars = max(equity, 0.0) * RISK_PER_TRADE
    qty_by_risk = risk_dollars / stop_dist if stop_dist > 0 else 0
    margin_cap = max(equity, 0.0) * MAX_MARGIN_FRACTION
    notional_cap = margin_cap * MAX_VIRTUAL_LEVERAGE
    qty_by_cap = notional_cap / price if price > 0 else 0
    qty = min(qty_by_risk, qty_by_cap)
    if not math.isfinite(qty) or qty <= 0:
        return False
    notional = qty * price
    margin = notional / MAX_VIRTUAL_LEVERAGE
    if margin > state["cash_usd"] or notional < 10:
        return False
    # Conservative fill model: adverse entry slippage.
    fill = price * (1 + SLIPPAGE_RATE if direction == "LONG" else 1 - SLIPPAGE_RATE)
    notional = qty * fill
    margin = notional / MAX_VIRTUAL_LEVERAGE
    if margin > state["cash_usd"]:
        return False
    fee = notional * FEE_RATE
    slip_cost = abs(qty * (fill - price))
    state["cash_usd"] = float(state["cash_usd"]) - margin
    state["open_trade"] = {
        "direction": direction,
        "opened_at": stamp,
        "entry_price": round(fill, 2),
        "qty_btc": qty,
        "notional_usd": notional,
        "margin_usd": margin,
        "leverage_virtual": MAX_VIRTUAL_LEVERAGE,
        "entry_fee_usd": fee,
        "entry_slippage_usd": slip_cost,
        "stop_price": round(fill - stop_dist if direction == "LONG" else fill + stop_dist, 2),
        "target_price": round(fill + target_dist if direction == "LONG" else fill - target_dist, 2),
        "initial_stop_price": round(fill - stop_dist if direction == "LONG" else fill + stop_dist, 2),
        "trail_active": False,
        "risk_usd": risk_dollars,
        "signal_candle_time": candle_time,
    }
    return True

def manage_open_trade(state, candles, price, stamp):
    t = state.get("open_trade")
    if not t:
        return
    direction = t["direction"]
    # Use candle high/low to detect stops/targets. If both touched in same candle,
    # assume the stop occurred first (conservative).
    last = candles[-1]
    high, low = last["high"], last["low"]
    stop = float(t["stop_price"])
    target = float(t["target_price"])
    exit_price = None
    reason = None
    if direction == "LONG":
        if low <= stop:
            exit_price, reason = stop * (1 - SLIPPAGE_RATE), "損切り"
        elif high >= target:
            exit_price, reason = target * (1 - SLIPPAGE_RATE), "利確"
        else:
            # Once price moves +1.2R, trail stop to protect some profit.
            entry = float(t["entry_price"])
            initial_stop = float(t.get("initial_stop_price", stop))
            r = max(entry - initial_stop, 1e-9)
            if high >= entry + 1.2 * r:
                t["trail_active"] = True
                t["stop_price"] = round(max(float(t["stop_price"]), high - 1.4 * r), 2)
            opened = parse_dt(t.get("opened_at"))
            now = parse_dt(stamp)
            if opened and now and (now - opened).total_seconds() >= MAX_HOLD_HOURS * 3600:
                exit_price, reason = price * (1 - SLIPPAGE_RATE), "保有時間上限"
    else:
        if high >= stop:
            exit_price, reason = stop * (1 + SLIPPAGE_RATE), "損切り"
        elif low <= target:
            exit_price, reason = target * (1 + SLIPPAGE_RATE), "利確"
        else:
            entry = float(t["entry_price"])
            initial_stop = float(t.get("initial_stop_price", stop))
            r = max(initial_stop - entry, 1e-9)
            if low <= entry - 1.2 * r:
                t["trail_active"] = True
                t["stop_price"] = round(min(float(t["stop_price"]), low + 1.4 * r), 2)
            opened = parse_dt(t.get("opened_at"))
            now = parse_dt(stamp)
            if opened and now and (now - opened).total_seconds() >= MAX_HOLD_HOURS * 3600:
                exit_price, reason = price * (1 + SLIPPAGE_RATE), "保有時間上限"
    if exit_price is not None:
        close_trade(state, exit_price, reason, stamp)

def build_metrics(state, price):
    equity = equity_of(state, price)
    trades = state.get("recent_trades", [])
    pnls = [float(t.get("net_pnl_usd", 0)) for t in trades if valid_num(t.get("net_pnl_usd"))]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    breakeven = len(pnls) - wins - losses
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    peak = max(float(state.get("peak_equity_usd", equity)), equity)
    state["peak_equity_usd"] = peak
    dd = max(0.0, (peak - equity) / peak * 100.0) if peak > 0 else 0.0
    t = state.get("open_trade")
    unrealized = 0.0
    if t:
        side = 1.0 if t["direction"] == "LONG" else -1.0
        gross = (price - float(t["entry_price"])) * float(t["qty_btc"]) * side
        costs = float(t.get("entry_fee_usd", 0)) + float(t.get("entry_slippage_usd", 0)) + abs(price * float(t["qty_btc"])) * (FEE_RATE + SLIPPAGE_RATE)
        unrealized = gross - costs
    return {
        "equity_usd": round(equity, 8),
        "cash_usd": round(float(state["cash_usd"]), 8),
        "net_closed_pnl_usd": round(sum(pnls), 8),
        "closed_trades": len(pnls),
        "wins": wins,
        "losses": losses,
        "breakeven_trades": breakeven,
        "win_rate_pct": round(wins / len(pnls) * 100, 2) if pnls else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else (None if gross_win == 0 else 99.0),
        "drawdown_pct": round(dd, 4),
        "open_unrealized_pnl_usd": round(unrealized, 8),
    }

def valid_num(v):
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False

def main():
    stamp = now_iso()
    state = load_state()
    c15 = get_candles("15m", 180)
    c1h = get_candles("1h", 180)
    c4h = get_candles("4h", 180)
    if min(len(c15), len(c1h), len(c4h)) < MIN_BARS:
        raise RuntimeError(f"ローソク足データ不足: 15m={len(c15)}, 1h={len(c1h)}, 4h={len(c4h)}")

    funding, mark_price = get_market_context()
    price = float(mark_price or c15[-1]["close"])
    signal = get_signal(c15, c1h, c4h)
    atr_value = signal.get("atr_15m")
    candle_time = str(c15[-1]["time"])

    # Prevent duplicate entries on repeated workflow runs for the same candle.
    # Open trades are still managed on every run, even if the candle is unchanged.
    manage_open_trade(state, c15, price, stamp)
    if not state.get("open_trade") and state.get("last_processed_candle") != candle_time:
        equity_now = equity_of(state, price)
        allowed, halt_reason = can_open(state, equity_now, stamp)
        if allowed and signal["direction"] in ("LONG", "SHORT"):
            opened = open_trade(state, signal["direction"], price, float(atr_value or 0), equity_now, stamp, candle_time)
            if opened:
                state["last_processed_candle"] = candle_time
        elif not allowed:
            signal["reasons"].append(halt_reason)
        if signal["direction"] == "WAIT":
            state["last_processed_candle"] = candle_time
    else:
        allowed, halt_reason = can_open(state, equity_of(state, price), stamp)
    if state.get("open_trade"):
        state["last_processed_candle"] = candle_time

    state["last_price"] = price
    state["last_funding_rate"] = funding
    equity = equity_of(state, price)
    state["peak_equity_usd"] = max(float(state.get("peak_equity_usd", equity)), equity)
    state["equity_history"].append({"time": stamp, "equity_usd": round(equity, 8), "price": round(price, 2)})
    state["equity_history"] = state["equity_history"][-500:]
    metrics = build_metrics(state, price)

    data = {
        "build": BUILD,
        "generated_at": stamp,
        "price": round(price, 2),
        "funding_rate_pct": round(funding * 100, 6) if funding is not None else None,
        "signal": signal,
        "metrics": metrics,
        "risk": {
            "virtual_leverage": MAX_VIRTUAL_LEVERAGE,
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_margin_fraction_pct": MAX_MARGIN_FRACTION * 100,
            "max_daily_loss_pct": MAX_DAILY_LOSS * 100,
            "max_drawdown_pct": MAX_DRAWDOWN * 100,
            "halt_reason": halt_reason if not allowed else None,
            "paper_only": True,
        },
        "open_trade": state.get("open_trade"),
        "recent_trades": state.get("recent_trades", [])[-50:],
        "equity_history": state.get("equity_history", []),
        "market": {
            "candles_15m": len(c15),
            "candles_1h": len(c1h),
            "candles_4h": len(c4h),
            "last_candle_time": candle_time,
        },
    }
    save_json(STATE_PATH, state)
    save_json(DATA_PATH, data)
    print(json.dumps({
        "build": BUILD,
        "price": price,
        "signal": signal["direction"],
        "score": signal["score"],
        "equity_usd": metrics["equity_usd"],
        "closed_trades": metrics["closed_trades"],
        "open_position": state.get("open_trade") is not None,
        "paper_only": True,
    }, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Public market-data request failed: {exc}")
        raise
