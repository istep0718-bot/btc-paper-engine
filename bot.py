#!/usr/bin/env python3
"""
BTC Paper Engine V10
Paper trading only. Uses public Hyperliquid market data.
Never connects a wallet and never submits live orders.
"""
import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V10_2026-10-22"
API = "https://api.hyperliquid.xyz/info"

INITIAL_EQUITY = 100.0
LEVERAGE = 40.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10
FEE_RATE = 0.0005             # per side, on notional
SLIPPAGE_RATE = 0.0002        # estimated per side
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_SECONDS = 3600
MAX_HOLD_SECONDS = 24 * 3600
FUNDING_INTERVAL_SECONDS = 3600
MAX_TRADES_STORED = 2000
MAX_HISTORY_STORED = 1000

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def now():
    return datetime.now(timezone.utc)


def iso(ts=None):
    return datetime.fromtimestamp(
        float(ts if ts is not None else time.time()), timezone.utc
    ).isoformat()


def finite_number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def request_json(payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Engine/10",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as response:
        raw = response.read()
    data = json.loads(raw.decode("utf-8"))
    if data is None:
        raise RuntimeError("Empty response from market API")
    return data


def candles(interval, count=240):
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - count * INTERVAL_MS[interval]
    raw = request_json({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    })
    if not isinstance(raw, list):
        raise RuntimeError(f"Unexpected candle response for {interval}")
    result = []
    for item in raw:
        try:
            row = {
                "t": int(item["t"]),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "v": float(item["v"]),
            }
            if not all(math.isfinite(row[k]) for k in ("o", "h", "l", "c", "v")):
                continue
            if min(row["o"], row["h"], row["l"], row["c"]) <= 0 or row["v"] < 0:
                continue
            if row["h"] < max(row["o"], row["c"], row["l"]):
                continue
            if row["l"] > min(row["o"], row["c"], row["h"]):
                continue
            # Do not use the currently forming candle.
            if row["t"] + INTERVAL_MS[interval] <= end_ms:
                result.append(row)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    result.sort(key=lambda x: x["t"])
    # De-duplicate candles by opening timestamp.
    unique = {}
    for row in result:
        unique[row["t"]] = row
    result = [unique[k] for k in sorted(unique)]
    if len(result) < 110:
        raise RuntimeError(f"Insufficient closed {interval} candles: {len(result)}")
    return result


def ema(values, period):
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1.0 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss <= 1e-15:
        return 100.0 if avg_gain > 1e-15 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(rows):
    out = []
    for i in range(1, len(rows)):
        cur, prev = rows[i], rows[i - 1]
        out.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    return out


def atr(rows, period=14):
    tr = true_ranges(rows)
    if len(tr) < period:
        return None
    value = sum(tr[:period]) / period
    for item in tr[period:]:
        value = (value * (period - 1) + item) / period
    return value


def adx(rows, period=14):
    """Wilder-smoothed ADX approximation using standard directional movement."""
    if len(rows) < period * 2 + 2:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(rows)):
        cur, prev = rows[i], rows[i - 1]
        up = cur["h"] - prev["h"]
        down = prev["l"] - cur["l"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    if len(trs) < period * 2:
        return None
    sm_tr = sum(trs[:period])
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])
    dx_values = []
    for i in range(period, len(trs)):
        sm_tr = sm_tr - sm_tr / period + trs[i]
        sm_plus = sm_plus - sm_plus / period + plus_dm[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        if sm_tr <= 1e-15:
            dx_values.append(0.0)
            continue
        pdi = 100.0 * sm_plus / sm_tr
        mdi = 100.0 * sm_minus / sm_tr
        denom = pdi + mdi
        dx_values.append(100.0 * abs(pdi - mdi) / denom if denom > 1e-15 else 0.0)
    if len(dx_values) < period:
        return None
    value = sum(dx_values[:period]) / period
    for item in dx_values[period:]:
        value = (value * (period - 1) + item) / period
    return value


def funding_rate():
    """Returns Hyperliquid BTC hourly funding rate as a decimal, or None."""
    data = request_json({"type": "metaAndAssetCtxs"})
    if not isinstance(data, list) or len(data) < 2:
        return None
    meta, contexts = data[0], data[1]
    if not isinstance(meta, dict) or not isinstance(contexts, list):
        return None
    universe = meta.get("universe", [])
    names = [asset.get("name") for asset in universe if isinstance(asset, dict)]
    if "BTC" not in names:
        return None
    idx = names.index("BTC")
    if idx >= len(contexts) or not isinstance(contexts[idx], dict):
        return None
    return finite_number(contexts[idx].get("funding"), None)


def analyze(rows):
    close = [x["c"] for x in rows]
    volume = [x["v"] for x in rows]
    price = close[-1]
    e20, e50, e100 = ema(close, 20), ema(close, 50), ema(close, 100)
    r, a, d = rsi(close), atr(rows), adx(rows)

    ema12, ema26 = ema(close, 12), ema(close, 26)
    macd_line = (ema12 if ema12 is not None else price) - (ema26 if ema26 is not None else price)
    macd_series = []
    for i in range(26, len(close) + 1):
        c = close[:i]
        macd_series.append((ema(c, 12) or c[-1]) - (ema(c, 26) or c[-1]))
    macd_signal = ema(macd_series, 9) if len(macd_series) >= 9 else macd_line
    hist = macd_line - (macd_signal if macd_signal is not None else macd_line)
    momentum = (price / close[-6] - 1.0) * 100.0 if close[-6] else 0.0
    avg_vol = sum(volume[-21:-1]) / max(len(volume[-21:-1]), 1)
    vol_ratio = volume[-1] / avg_vol if avg_vol > 0 else 1.0

    trend = "SIDEWAYS"
    if e20 is not None and e50 is not None and e100 is not None:
        if e20 > e50 > e100:
            trend = "UP"
        elif e20 < e50 < e100:
            trend = "DOWN"
    return {
        "price": price, "ema20": e20, "ema50": e50, "ema100": e100,
        "rsi": r, "atr": a, "adx": d, "macd": macd_line,
        "macd_signal": macd_signal, "macd_hist": hist,
        "momentum_pct": momentum, "volume_ratio": vol_ratio,
        "trend": trend, "candle_time": rows[-1]["t"],
        "last_high": rows[-1]["h"], "last_low": rows[-1]["l"],
    }


def default_state():
    t = now().date().isoformat()
    return {
        "build": BUILD,
        "equity": INITIAL_EQUITY,
        "initial_equity": INITIAL_EQUITY,
        "position": None,
        "trades": [],
        "equity_history": [],
        "last_exit_timestamp": 0.0,
        "consecutive_losses": 0,
        "day": t,
        "day_start_equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
        "funding_last_timestamp": 0.0,
        "funding_total": 0.0,
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()
    try:
        s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(s, dict):
            raise ValueError("state.json must contain an object")
        # Preserve account history/equity from existing versions.
        if finite_number(s.get("equity")) is None:
            s["equity"] = finite_number(s.get("initial_equity"), INITIAL_EQUITY)
        if finite_number(s.get("initial_equity")) is None:
            s["initial_equity"] = INITIAL_EQUITY
        s["equity"] = float(s["equity"])
        s["initial_equity"] = float(s["initial_equity"])
        s.setdefault("position", s.get("open_position"))
        s.setdefault("trades", s.get("paper_trades", []))
        s.setdefault("equity_history", [])
        s.setdefault("last_exit_timestamp", 0.0)
        s.setdefault("consecutive_losses", 0)
        s.setdefault("day", now().date().isoformat())
        s.setdefault("day_start_equity", s["equity"])
        s.setdefault("peak_equity", s["equity"])
        s.setdefault("funding_last_timestamp", 0.0)
        s.setdefault("funding_total", 0.0)
        if not isinstance(s["trades"], list):
            s["trades"] = []
        if not isinstance(s["equity_history"], list):
            s["equity_history"] = []
        if s["position"] is not None:
            p = s["position"]
            if not isinstance(p, dict):
                raise ValueError("position must be an object or null")
            # Normalize older position schemas; stop rather than silently drop a position.
            side = str(p.get("side", p.get("direction", ""))).upper()
            if side not in ("LONG", "SHORT"):
                raise ValueError("Open position has no valid LONG/SHORT side")
            p["side"] = side
            for key in ("entry_price", "quantity"):
                if finite_number(p.get(key)) is None or float(p[key]) <= 0:
                    raise ValueError(f"Open position has invalid {key}")
                p[key] = float(p[key])
            p.setdefault("leverage", LEVERAGE)
            p.setdefault("margin_used", p["entry_price"] * p["quantity"] / LEVERAGE)
            p.setdefault("opened_timestamp", time.time())
            p.setdefault("entry_time", iso(p["opened_timestamp"]))
            p.setdefault("risk_distance", max(p["entry_price"] * 0.003, 1.0))
            sign = 1 if side == "LONG" else -1
            p.setdefault("stop_price", p["entry_price"] - sign * p["risk_distance"])
            p.setdefault("target_price", None)
            p.setdefault("runner_mode", False)
            p.setdefault("last_funding_timestamp", p["opened_timestamp"])
        s["trades"] = s["trades"][-MAX_TRADES_STORED:]
        s["equity_history"] = s["equity_history"][-MAX_HISTORY_STORED:]
        return s
    except Exception as exc:
        backup = STATE_FILE.with_suffix(".corrupt.json")
        try:
            backup.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            f"state.json could not be safely loaded; backup attempted; stopped: {exc}"
        ) from exc


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def mark_to_market(position, price):
    if not position:
        return 0.0
    sign = 1 if position["side"] == "LONG" else -1
    gross = (price - position["entry_price"]) * position["quantity"] * sign
    # Include estimated fee to close; entry fee is charged on opening.
    close_fee = price * position["quantity"] * FEE_RATE
    return gross - close_fee


def charge_funding(state, rate, now_ts):
    """Approximate hourly funding settlement while a position is open."""
    p = state.get("position")
    if not p or rate is None or not math.isfinite(rate):
        return 0.0
    last = finite_number(p.get("last_funding_timestamp"), p.get("opened_timestamp", now_ts))
    if last is None:
        last = now_ts
    elapsed = max(0.0, now_ts - last)
    intervals = int(elapsed // FUNDING_INTERVAL_SECONDS)
    if intervals <= 0:
        return 0.0
    sign = 1 if p["side"] == "LONG" else -1
    notional = p["quantity"] * p["entry_price"]
    payment = -sign * rate * notional * intervals
    state["equity"] += payment
    state["funding_total"] = float(state.get("funding_total", 0.0)) + payment
    p["funding_paid"] = float(p.get("funding_paid", 0.0)) + payment
    p["last_funding_timestamp"] = last + intervals * FUNDING_INTERVAL_SECONDS
    return payment


def close_position(state, reference_price, reason, close_ts=None):
    p = state.get("position")
    if not p:
        return None
    close_ts = time.time() if close_ts is None else float(close_ts)
    sign = 1 if p["side"] == "LONG" else -1
    exit_fill = float(reference_price) * (1.0 - SLIPPAGE_RATE * sign)
    gross = (exit_fill - p["entry_price"]) * p["quantity"] * sign
    entry_fee = float(p.get("entry_fee", p["entry_price"] * p["quantity"] * FEE_RATE))
    exit_fee = exit_fill * p["quantity"] * FEE_RATE
    funding_paid = float(p.get("funding_paid", 0.0))
    net_trade_pnl = gross - entry_fee - exit_fee + funding_paid
    # Entry fee and funding are deducted/credited to equity when they occur.
    # At opening, entry fee is deducted immediately. At closing, add gross - exit fee.
    state["equity"] += gross - exit_fee
    trade = dict(p)
    trade.update({
        "exit_price": exit_fill,
        "exit_time": iso(close_ts),
        "gross_pnl": round(gross, 8),
        "entry_fee": round(entry_fee, 8),
        "exit_fee": round(exit_fee, 8),
        "fees": round(entry_fee + exit_fee, 8),
        "funding_paid": round(funding_paid, 8),
        "net_pnl": round(net_trade_pnl, 8),
        "pnl": round(net_trade_pnl, 8),
        "reason": reason,
    })
    state["trades"].append(trade)
    state["trades"] = state["trades"][-MAX_TRADES_STORED:]
    state["last_exit_timestamp"] = close_ts
    state["consecutive_losses"] = (
        int(state.get("consecutive_losses", 0)) + 1 if net_trade_pnl < 0 else 0
    )
    state["position"] = None
    return trade


def calc_scores(market, funding):
    sig = market["15m"]
    long_score = short_score = 0
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
    runner_mode = (
        market["1h"]["trend"] == market["4h"]["trend"]
        and market["1h"]["trend"] in ("UP", "DOWN")
        and (sig["adx"] or 0) >= 22
    )
    return direction, long_score, short_score, runner_mode


def manage_position(state, price, high, low, market, direction, long_score, short_score, funding, ts):
    p = state.get("position")
    if not p:
        return
    charge_funding(state, funding, ts)
    sign = 1 if p["side"] == "LONG" else -1
    risk = max(float(p.get("risk_distance", 1.0)), 1e-9)

    # Trailing stop uses the observed closed candle close, only moves in the favorable direction.
    favorable = (price - p["entry_price"]) * sign
    if favorable >= 1.5 * risk:
        trail = price - sign * 1.35 * risk
        if p["side"] == "LONG":
            p["stop_price"] = max(float(p["stop_price"]), trail)
        else:
            p["stop_price"] = min(float(p["stop_price"]), trail)

    # Conservative intrabar logic: if stop and target both touched, assume stop first.
    stop = float(p["stop_price"])
    target = finite_number(p.get("target_price"))
    if p["side"] == "LONG":
        stop_hit = low <= stop
        target_hit = target is not None and high >= target
    else:
        stop_hit = high >= stop
        target_hit = target is not None and low <= target

    held = ts - float(p.get("opened_timestamp", ts))
    if stop_hit:
        close_position(state, stop, "STOP_OR_TRAIL", ts)
    elif target_hit:
        close_position(state, target, "TAKE_PROFIT", ts)
    elif held >= MAX_HOLD_SECONDS:
        close_position(state, price, "MAX_HOLD", ts)
    elif direction == ("SHORT" if p["side"] == "LONG" else "LONG") and (
        (p["side"] == "LONG" and short_score >= 5 and short_score >= long_score + 2)
        or (p["side"] == "SHORT" and long_score >= 5 and long_score >= short_score + 2)
    ):
        close_position(state, price, "OPPOSITE_SIGNAL", ts)


def main():
    ts = time.time()
    state = load_state()
    market = {tf: analyze(candles(tf)) for tf in ("15m", "1h", "4h")}
    price = float(market["15m"]["price"])
    high = float(market["15m"]["last_high"])
    low = float(market["15m"]["last_low"])
    funding = funding_rate()
    direction, long_score, short_score, runner_mode = calc_scores(market, funding)

    # Handle legacy V8/V9 positions before opening any new position.
    if state.get("position"):
        manage_position(
            state, price, high, low, market, direction,
            long_score, short_score, funding, ts
        )

    # UTC daily risk limits are based on marked equity, not only realized equity.
    current_unrealized = mark_to_market(state.get("position"), price)
    marked_equity = float(state["equity"]) + current_unrealized
    today = now().date().isoformat()
    if state.get("day") != today:
        state["day"] = today
        state["day_start_equity"] = marked_equity
    state["peak_equity"] = max(float(state.get("peak_equity", state["equity"])), marked_equity)
    daily_loss = (
        (float(state["day_start_equity"]) - marked_equity)
        / max(float(state["day_start_equity"]), 1e-9)
    )
    drawdown = (
        (float(state["peak_equity"]) - marked_equity)
        / max(float(state["peak_equity"]), 1e-9)
    )

    can_open = (
        state.get("position") is None
        and direction in ("LONG", "SHORT")
        and state["equity"] > 0
        and daily_loss < MAX_DAILY_LOSS
        and drawdown < MAX_DRAWDOWN
        and int(state.get("consecutive_losses", 0)) < MAX_CONSECUTIVE_LOSSES
        and ts - float(state.get("last_exit_timestamp", 0.0)) >= COOLDOWN_SECONDS
    )

    if can_open and market["15m"]["atr"] and market["15m"]["atr"] > 0:
        risk_distance = min(
            max(float(market["15m"]["atr"]) * 1.6, price * 0.0015),
            price * 0.012,
        )
        risk_cash = max(0.0, marked_equity) * RISK_PER_TRADE
        qty_by_risk = risk_cash / risk_distance
        margin_cap = max(0.0, marked_equity) * MAX_MARGIN_FRACTION
        qty_by_margin = margin_cap * LEVERAGE / price
        quantity = min(qty_by_risk, qty_by_margin)
        if quantity > 0 and math.isfinite(quantity):
            sign = 1 if direction == "LONG" else -1
            entry = price * (1.0 + SLIPPAGE_RATE * sign)
            entry_fee = entry * quantity * FEE_RATE
            # Keep risk/margin constrained after applying entry slippage.
            margin_used = entry * quantity / LEVERAGE
            if margin_used <= max(0.0, marked_equity) * MAX_MARGIN_FRACTION * 1.001:
                stop = entry - sign * risk_distance
                target = None if runner_mode else entry + sign * 2.4 * risk_distance
                state["equity"] -= entry_fee
                state["position"] = {
                    "side": direction,
                    "entry_price": entry,
                    "quantity": quantity,
                    "leverage": LEVERAGE,
                    "margin_used": margin_used,
                    "notional": entry * quantity,
                    "risk_distance": risk_distance,
                    "stop_price": stop,
                    "target_price": target,
                    "opened_timestamp": ts,
                    "entry_time": iso(ts),
                    "runner_mode": runner_mode,
                    "entry_fee": entry_fee,
                    "funding_paid": 0.0,
                    "last_funding_timestamp": ts,
                }

    position = state.get("position")
    unrealized = mark_to_market(position, price)
    marked_equity = float(state["equity"]) + unrealized
    state["peak_equity"] = max(float(state.get("peak_equity", state["equity"])), marked_equity)
    drawdown = (
        (float(state["peak_equity"]) - marked_equity)
        / max(float(state["peak_equity"]), 1e-9)
    )

    closed = state["trades"]
    wins = sum(1 for t in closed if finite_number(t.get("net_pnl"), 0.0) > 0)
    losses = sum(1 for t in closed if finite_number(t.get("net_pnl"), 0.0) < 0)
    net_pnls = [finite_number(t.get("net_pnl"), 0.0) for t in closed]
    gross_profit = sum(x for x in net_pnls if x > 0)
    gross_loss = abs(sum(x for x in net_pnls if x < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else (None if gross_profit > 0 else 0.0)
    win_rate = wins / len(closed) * 100.0 if closed else 0.0
    realized_pnl = float(state["equity"]) - float(state["initial_equity"])
    total_net_pnl = marked_equity - float(state["initial_equity"])

    history_row = {
        "time": iso(ts),
        "timestamp": iso(ts),
        "equity": round(marked_equity, 8),
        "equity_usd": round(marked_equity, 8),
        "realized_equity": round(float(state["equity"]), 8),
        "unrealized_pnl": round(unrealized, 8),
        "price": price,
    }
    history = state.get("equity_history", [])
    if not history or history[-1].get("time") != history_row["time"]:
        history.append(history_row)
    state["equity_history"] = history[-MAX_HISTORY_STORED:]

    # Keep V8/V9 dashboard field names, while adding aliases for older HTML layouts.
    trends = {
        "15m": {"UP": 1, "DOWN": -1, "SIDEWAYS": 0}[market["15m"]["trend"]],
        "1h": {"UP": 1, "DOWN": -1, "SIDEWAYS": 0}[market["1h"]["trend"]],
        "4h": {"UP": 1, "DOWN": -1, "SIDEWAYS": 0}[market["4h"]["trend"]],
    }
    reason = (
        f"15m {market['15m']['trend']} RSI {market['15m']['rsi']:.1f}; "
        f"ADX {market['15m']['adx']:.1f}; "
        f"1h {market['1h']['trend']}, 4h {market['4h']['trend']}"
    )
    payload = {
        "build": BUILD,
        "version": BUILD,
        "generated_at": iso(ts),
        "mode": "PAPER ONLY",
        "live_trading": False,
        "wallet_connected": False,
        "paper_only": True,
        "live_orders_enabled": False,
        "leverage": LEVERAGE,
        "price": price,
        "current_price": price,
        "funding_rate": funding,
        "funding_rate_pct": funding * 100.0 if funding is not None else None,
        "initial_equity": float(state["initial_equity"]),
        "equity": round(float(state["equity"]), 8),
        "realized_pnl": round(realized_pnl, 8),
        "unrealized_pnl": round(unrealized, 8),
        "net_pnl": round(total_net_pnl, 8),
        "position": position,
        "open_position": position,
        "signal": {
            "direction": direction,
            "score": max(long_score, short_score) * 10,
            "long_score": long_score,
            "short_score": short_score,
            "reason": reason,
            "runner_setup": runner_mode,
        },
        "score": max(long_score, short_score) * 10,
        "reason": reason,
        "rsi": market["15m"]["rsi"],
        "rsi_15m": market["15m"]["rsi"],
        "adx_15m": market["15m"]["adx"],
        "atr_15m": market["15m"]["atr"],
        "macd_15m": market["15m"]["macd"],
        "macd_signal_15m": market["15m"]["macd_signal"],
        "macd_hist_15m": market["15m"]["macd_hist"],
        "momentum_15m_pct": market["15m"]["momentum_pct"],
        "momentum_pct": market["15m"]["momentum_pct"],
        "volume_ratio_15m": market["15m"]["volume_ratio"],
        "volume_ratio": market["15m"]["volume_ratio"],
        "timeframes": {
            tf: {
                **market[tf],
                "direction": trends[tf],
                "label": tf,
            }
            for tf in ("15m", "1h", "4h")
        },
        "trend_15m": trends["15m"],
        "trend_1h": trends["1h"],
        "trend_4h": trends["4h"],
        "trends": trends,
        "metrics": {
            "trades": len(closed),
            "closed_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "win_rate_pct": win_rate,
            "gross_profit": round(gross_profit, 8),
            "gross_loss": round(gross_loss, 8),
            "profit_factor": round(pf, 6) if pf is not None else None,
            "net_pnl": round(total_net_pnl, 8),
            "return_pct": (total_net_pnl / float(state["initial_equity"]) * 100.0)
                if float(state["initial_equity"]) else 0.0,
            "max_drawdown_pct": drawdown * 100.0,
            "consecutive_losses": int(state.get("consecutive_losses", 0)),
        },
        "win_rate": win_rate,
        "trade_count": len(closed),
        "closed_trades": len(closed),
        "profit_factor": round(pf, 6) if pf is not None else None,
        "max_drawdown_pct": drawdown * 100.0,
        "consecutive_losses": int(state.get("consecutive_losses", 0)),
        "trades": closed[-20:],
        "paper_trades": closed[-20:],
        "equity_history": state["equity_history"],
        "api_error": None,
        "risk": {
            "risk_per_trade_pct": RISK_PER_TRADE * 100.0,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100.0,
            "max_daily_loss_pct": MAX_DAILY_LOSS * 100.0,
            "max_drawdown_pct": MAX_DRAWDOWN * 100.0,
            "max_consecutive_losses": MAX_CONSECUTIVE_LOSSES,
        },
        "funding_total": round(float(state.get("funding_total", 0.0)), 8),
    }

    save_json(STATE_FILE, state)
    save_json(DATA_FILE, payload)
    print(json.dumps({
        "build": BUILD,
        "price": price,
        "leverage": LEVERAGE,
        "signal": direction,
        "equity": round(float(state["equity"]), 8),
        "marked_equity": round(marked_equity, 8),
        "closed_trades": len(closed),
        "position": position["side"] if position else None,
        "paper_only": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        raise
