
#!/usr/bin/env python3
"""BTC Paper Engine V4 - paper trading only; no live orders or wallet keys."""

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DASH_FILE = ROOT / "docs" / "data.json"

BUILD = "BTC_PAPER_V4_2026-10-20"

INITIAL_EQUITY = 20.0
LEVERAGE = 3.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
FEE_PER_SIDE = 0.0005
SLIPPAGE_PER_SIDE = 0.0002
MIN_NOTIONAL = 1.0
MAX_HOLD_HOURS = 24
MAX_TRADES = 500
MAX_HISTORY = 2000


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def now_ms():
    return int(time.time() * 1000)


def finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def request_api(payload):
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Research/4.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def get_candles(interval, hours):
    end = now_ms()
    raw = request_api({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": end - hours * 3600000,
            "endTime": end,
        },
    })

    if not isinstance(raw, list):
        raise RuntimeError("Invalid candle API response")

    candles = []
    for item in raw:
        try:
            c = {
                "t": int(item["t"]),
                "T": int(item["T"]),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "v": float(item["v"]),
            }
            prices = [c[k] for k in ("o", "h", "l", "c")]
            if (
                all(math.isfinite(x) and x > 0 for x in prices)
                and math.isfinite(c["v"]) and c["v"] >= 0
                and c["h"] >= max(c["o"], c["c"], c["l"])
                and c["l"] <= min(c["o"], c["c"], c["h"])
                and c["T"] < now_ms()
            ):
                candles.append(c)
        except (KeyError, TypeError, ValueError):
            continue

    unique = {c["t"]: c for c in candles}
    return sorted(unique.values(), key=lambda c: c["t"])


def ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)
    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [values[i] - values[i - 1]
               for i in range(1, len(values))]
    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        ranges.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))

    value = sum(ranges[:period]) / period
    for x in ranges[period:]:
        value = (value * (period - 1) + x) / period
    return value


def fib_levels(candles, lookback=96):
    chunk = candles[-lookback:]
    if len(chunk) < 10:
        return {}

    high = max(c["h"] for c in chunk)
    low = min(c["l"] for c in chunk)
    span = high - low
    if span <= 0:
        return {}

    return {
        "swing_high": round(high, 2),
        "swing_low": round(low, 2),
        "retracement_382": round(high - span * 0.382, 2),
        "retracement_500": round(high - span * 0.500, 2),
        "retracement_618": round(high - span * 0.618, 2),
    }


def find_fvg(candles, price):
    gaps = []

    for i in range(2, len(candles)):
        first, third = candles[i - 2], candles[i]

        if third["l"] > first["h"]:
            gaps.append({
                "type": "bullish",
                "low": round(first["h"], 2),
                "high": round(third["l"], 2),
                "formed_at": third["t"],
            })

        if third["h"] < first["l"]:
            gaps.append({
                "type": "bearish",
                "low": round(third["h"], 2),
                "high": round(first["l"], 2),
                "formed_at": third["t"],
            })

    active = []
    for gap in gaps[-30:]:
        if gap["type"] == "bullish" and price >= gap["low"]:
            active.append(gap)
        elif gap["type"] == "bearish" and price <= gap["high"]:
            active.append(gap)

    return active[-6:]


def default_state():
    return {
        "build": BUILD,
        "created_at": iso_now(),
        "cash": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
        "trades": [],
        "open_trade": None,
        "daily": {
            "date": utc_now().date().isoformat(),
            "start_equity": INITIAL_EQUITY,
        },
        "last_signal_key": None,
        "halt_reason": None,
        "equity_history": [],
    }


def load_state():
    if not STATE_FILE.exists():
        return default_state()

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "state.json unreadable; refusing to reset trading state"
        ) from exc

    if not isinstance(state, dict):
        raise RuntimeError("Invalid state.json format")

    if state.get("open_trade") and state.get("build") != BUILD:
        raise RuntimeError(
            "Existing open position detected during version change. "
            "Preserve state.json; review before upgrading."
        )

    # Preserve existing cash, closed trades and equity history.
    state.setdefault("cash", INITIAL_EQUITY)
    state.setdefault("peak_equity", float(state["cash"]))
    state.setdefault("trades", [])
    state.setdefault("open_trade", None)
    state.setdefault("daily", {
        "date": utc_now().date().isoformat(),
        "start_equity": float(state["cash"]),
    })
    state.setdefault("last_signal_key", None)
    state.setdefault("halt_reason", None)
    state.setdefault("equity_history", [])

    if not isinstance(state["trades"], list):
        raise RuntimeError("Invalid trades data; refusing to reset state")
    if not isinstance(state["equity_history"], list):
        raise RuntimeError("Invalid equity history; refusing to reset state")

    state["build"] = BUILD
    return state


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temp.replace(path)


def market_context():
    result = request_api({"type": "metaAndAssetCtxs"})
    if not isinstance(result, list) or len(result) < 2:
        raise RuntimeError("Invalid market metadata response")

    meta, contexts = result[0], result[1]
    index = next(
        (i for i, asset in enumerate(meta.get("universe", []))
         if asset.get("name") == "BTC"),
        None,
    )
    if index is None or index >= len(contexts):
        raise RuntimeError("BTC market data unavailable")

    ctx = contexts[index]
    c15 = get_candles("15m", 24 * 10)
    c1 = get_candles("1h", 24 * 45)
    c4 = get_candles("4h", 24 * 120)

    if min(len(c15), len(c1), len(c4)) < 60:
        raise RuntimeError(
            f"Insufficient candles: 15m={len(c15)}, "
            f"1h={len(c1)}, 4h={len(c4)}"
        )

    price = float(ctx.get("markPx") or c15[-1]["c"])
    funding = float(ctx.get("funding") or 0)
    oi = float(ctx.get("openInterest") or 0)
    premium = float(ctx.get("premium") or 0)
    volume = float(ctx.get("dayNtlVlm") or 0)

    if not finite(price) or price <= 0:
        raise RuntimeError("Invalid mark price")
    if not all(finite(x) for x in (funding, oi, premium, volume)):
        raise RuntimeError("Invalid market context values")

    return {
        "price": price,
        "funding": funding,
        "open_interest_btc": oi,
        "premium": premium,
        "volume_24h_usd": volume,
        "candles_15m": c15,
        "candles_1h": c1,
        "candles_4h": c4,
    }


def make_signal(market):
    c15 = market["candles_15m"]
    c1 = market["candles_1h"]
    c4 = market["candles_4h"]

    closes15 = [c["c"] for c in c15]
    closes1 = [c["c"] for c in c1]
    closes4 = [c["c"] for c in c4]

    e20_1, e50_1 = ema(closes1, 20), ema(closes1, 50)
    e20_4, e50_4 = ema(closes4, 20), ema(closes4, 50)
    rsi15, atr15 = rsi(closes15), atr(c15)
    price = market["price"]
    fib = fib_levels(c1)
    fvgs = find_fvg(c15, price)

    signal = {
        "direction": "WAIT",
        "score": 0,
        "reasons": [],
        "price": price,
        "rsi_15m": rsi15,
        "atr_15m": atr15,
        "ema20_1h": e20_1,
        "ema50_1h": e50_1,
        "ema20_4h": e20_4,
        "ema50_4h": e50_4,
        "fib": fib,
        "fvgs": fvgs,
    }

    indicators = [e20_1, e50_1, e20_4, e50_4, rsi15, atr15]
    if (
        any(x is None or not finite(x) for x in indicators)
        or atr15 <= 0 or not fib
    ):
        signal["reasons"] = ["指標不足または異常値のため見送り"]
        return signal

    score = 0
    reasons = []

    # Higher-timeframe direction.
    trend1 = 1 if e20_1 > e50_1 else -1
    trend4 = 1 if e20_4 > e50_4 else -1

    score += trend1
    reasons.append(
        "1時間足EMA20 > EMA50" if trend1 > 0
        else "1時間足EMA20 < EMA50"
    )

    score += trend4
    reasons.append(
        "4時間足EMA20 > EMA50" if trend4 > 0
        else "4時間足EMA20 < EMA50"
    )

    if price > e20_1:
        score += 1
        reasons.append("価格は1時間足EMA20より上")
    else:
        score -= 1
        reasons.append("価格は1時間足EMA20より下")

    # Momentum score.
    if 50 <= rsi15 <= 67:
        score += 1
        reasons.append(f"RSI {rsi15:.1f}: 上向き候補")
    elif 33 <= rsi15 < 50:
        score -= 1
        reasons.append(f"RSI {rsi15:.1f}: 弱め")
    else:
        reasons.append(f"RSI {rsi15:.1f}: 中立条件外")

    if market["funding"] > 0.0003:
        score -= 1
        reasons.append("資金調達率は高め")
    elif market["funding"] < -0.0003:
        score += 1
        reasons.append("資金調達率は低め")
    else:
        reasons.append("資金調達率に極端な偏りなし")

    # Avoid entries after an unusually large completed candle.
    move = abs(closes15[-1] - closes15[-2]) / closes15[-2]
    if move > 0.012:
        reasons.append("直近確定足の変動が大きいため見送り")

    direction = "WAIT"

    # New rule: both 1h and 4h trends must agree.
    if trend1 == 1 and trend4 == 1 and score >= 3:
        if 50 <= rsi15 <= 68 and move <= 0.012:
            direction = "LONG"
        else:
            reasons.append("LONG条件: RSIまたは変動幅が不適合")

    elif trend1 == -1 and trend4 == -1 and score <= -3:
        if 32 <= rsi15 <= 50 and move <= 0.012:
            direction = "SHORT"
        else:
            reasons.append("SHORT条件: RSIまたは変動幅が不適合")

    else:
        reasons.append("上位足の方向または総合スコアが不一致")

    signal.update({
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "candle_move_pct": round(move * 100, 4),
        "signal_candle_time": c15[-1]["t"],
    })
    return signal


def unrealized_pnl(trade, price):
    direction = 1 if trade["direction"] == "LONG" else -1
    return (
        float(trade["notional"]) * direction
        * (price - float(trade["entry_price"]))
        / float(trade["entry_price"])
    )


def equity(state, price):
    trade = state.get("open_trade")
    if not trade:
        return float(state["cash"])
    return (
        float(state["cash"])
        + float(trade["margin"])
        + unrealized_pnl(trade, price)
    )


def simulated_fill(price, direction, entering):
    sign = 1 if direction == "LONG" else -1
    adjustment = sign if entering else -sign
    return price * (1 + adjustment * SLIPPAGE_PER_SIDE)


def close_trade(state, price, reason):
    trade = state.get("open_trade")
    if not trade:
        return

    exit_price = simulated_fill(price, trade["direction"], entering=False)
    gross = unrealized_pnl(trade, exit_price)
    fees = float(trade["notional"]) * FEE_PER_SIDE * 2
    net = gross - fees

    state["cash"] = max(
        0.0,
        float(state["cash"]) + float(trade["margin"]) + net,
    )

    trade.update({
        "mark_exit_price": price,
        "exit_price": round(exit_price, 2),
        "closed_at": iso_now(),
        "close_reason": reason,
        "gross_pnl_usd": round(gross, 6),
        "fees_est_usd": round(fees, 6),
        "net_pnl_usd": round(net, 6),
        "return_on_margin_pct": round(
            net / max(float(trade["margin"]), 1e-9) * 100, 3
        ),
    })

    state["trades"].append(trade)
    state["trades"] = state["trades"][-MAX_TRADES:]
    state["open_trade"] = None


def calculate_halt(state, current_equity):
    peak = max(float(state["peak_equity"]), current_equity)
    drawdown = (peak - current_equity) / max(peak, 1e-9)

    daily_start = float(state["daily"]["start_equity"])
    daily_loss = (
        (daily_start - current_equity) / max(daily_start, 1e-9)
    )

    if current_equity <= 0:
        return "EQUITY_DEPLETED"
    if drawdown >= MAX_DRAWDOWN:
        return "MAX_DRAWDOWN_10_PERCENT"
    if daily_loss >= MAX_DAILY_LOSS:
        return "DAILY_LOSS_3_PERCENT"
    return None


def trade_metrics(trades):
    pnl_values = [
        float(t.get("net_pnl_usd", 0))
        for t in trades
        if finite(t.get("net_pnl_usd", 0))
    ]

    wins_list = [p for p in pnl_values if p > 0]
    losses_list = [p for p in pnl_values if p < 0]
    wins = len(wins_list)
    losses = len(losses_list)
    count = len(pnl_values)

    gross_profit = sum(wins_list)
    gross_loss = abs(sum(losses_list))
    pf = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (None if gross_profit == 0 else 999.0)
    )

    return {
        "closed_trades": count,
        "wins": wins,
        "losses": losses,
        "breakeven_trades": sum(1 for p in pnl_values if p == 0),
        "win_rate_pct": round(wins / count * 100, 2) if count else 0.0,
        "net_closed_pnl_usd": round(sum(pnl_values), 6),
        "gross_profit_usd": round(gross_profit, 6),
        "gross_loss_usd": round(gross_loss, 6),
        "profit_factor": round(pf, 4) if pf is not None else None,
        "average_win_usd": round(
            sum(wins_list) / wins if wins else 0.0, 6
        ),
        "average_loss_usd": round(
            sum(losses_list) / losses if losses else 0.0, 6
        ),
        "best_trade_usd": round(max(pnl_values), 6) if pnl_values else 0.0,
        "worst_trade_usd": round(min(pnl_values), 6) if pnl_values else 0.0,
        "expectancy_usd": round(sum(pnl_values) / count, 6) if count else 0.0,
    }


def run():
    state = load_state()
    market = market_context()
    signal = make_signal(market)
    price = market["price"]

    today = utc_now().date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {
            "date": today,
            "start_equity": equity(state, price),
        }
        if state.get("halt_reason") == "DAILY_LOSS_3_PERCENT":
            state["halt_reason"] = None

    eq = equity(state, price)
    state["peak_equity"] = max(float(state["peak_equity"]), eq)

    halt = calculate_halt(state, eq)
    if halt:
        state["halt_reason"] = halt

    # Manage an existing position before considering a new entry.
    trade = state.get("open_trade")
    if trade:
        direction = 1 if trade["direction"] == "LONG" else -1
        entry = float(trade["entry_price"])
        move = direction * (price - entry) / entry

        if move <= -0.08:
            close_trade(state, price, "EMERGENCY_LOSS_GUARD")
        elif (
            (direction == 1 and price <= float(trade["stop_price"]))
            or (direction == -1 and price >= float(trade["stop_price"]))
        ):
            close_trade(state, price, "STOP_LOSS")
        elif (
            (direction == 1 and price >= float(trade["target_price"]))
            or (direction == -1 and price <= float(trade["target_price"]))
        ):
            close_trade(state, price, "TAKE_PROFIT")
        elif (
            now_ms() - int(trade.get("opened_at_ms", now_ms()))
            >= MAX_HOLD_HOURS * 3600000
        ):
            close_trade(state, price, "MAX_HOLD_24H")

    eq = equity(state, price)
    state["peak_equity"] = max(float(state["peak_equity"]), eq)

    halt = calculate_halt(state, eq)
    if halt:
        state["halt_reason"] = halt

    # Entries are allowed only when no halt is active.
    if (
        not state.get("open_trade")
        and not state.get("halt_reason")
        and signal["direction"] in ("LONG", "SHORT")
    ):
        key = (
            f'{signal["direction"]}:'
            f'{signal.get("signal_candle_time", 0)}'
        )

        if state.get("last_signal_key") != key:
            atr_value = float(signal.get("atr_15m") or 0)
            stop_dist = max(1.5 * atr_value / price, 0.005)
            target_dist = stop_dist * 1.8
            risk_usd = max(0.0, eq * RISK_PER_TRADE)
            notional_by_risk = risk_usd / stop_dist
            margin_cap = max(0.0, eq * MAX_MARGIN_FRACTION)
            notional = min(notional_by_risk, margin_cap * LEVERAGE)
            margin = notional / LEVERAGE

            if (
                not finite(notional)
                or notional < MIN_NOTIONAL
                or margin > float(state["cash"])
                or stop_dist >= 0.08
            ):
                signal["reasons"].append(
                    "サイズ・資金・損切り幅の制限で見送り"
                )
                signal["direction"] = "WAIT"
            else:
                direction = 1 if signal["direction"] == "LONG" else -1
                entry = simulated_fill(
                    price, signal["direction"], entering=True
                )

                state["cash"] = float(state["cash"]) - margin
                state["open_trade"] = {
                    "id": key,
                    "direction": signal["direction"],
                    "mark_entry_price": price,
                    "entry_price": round(entry, 2),
                    "opened_at": iso_now(),
                    "opened_at_ms": now_ms(),
                    "notional": round(notional, 6),
                    "margin": round(margin, 6),
                    "leverage_virtual": LEVERAGE,
                    "stop_price": round(
                        entry * (1 - direction * stop_dist), 2
                    ),
                    "target_price": round(
                        entry * (1 + direction * target_dist), 2
                    ),
                    "stop_distance_pct": round(stop_dist * 100, 3),
                    "signal_score": signal["score"],
                    "entry_reasons": signal["reasons"],
                }
                state["last_signal_key"] = key

    eq = equity(state, price)
    state["peak_equity"] = max(float(state["peak_equity"]), eq)
    state["last_run_at"] = iso_now()
    state["last_price"] = price
    state["last_signal"] = signal

    history = state.setdefault("equity_history", [])
    history.append({
        "time": state["last_run_at"],
        "equity_usd": round(eq, 6),
        "price": price,
    })
    state["equity_history"] = history[-MAX_HISTORY:]

    metrics = trade_metrics(state["trades"])
    metrics.update({
        "equity_usd": round(eq, 6),
        "cash_usd": round(float(state["cash"]), 6),
        "peak_equity_usd": round(float(state["peak_equity"]), 6),
        "drawdown_pct": round(
            (float(state["peak_equity"]) - eq)
            / max(float(state["peak_equity"]), 1e-9) * 100,
            3,
        ),
        "open_unrealized_pnl_usd": round(
            unrealized_pnl(state["open_trade"], price), 6
        ) if state.get("open_trade") else 0.0,
    })
    state["metrics"] = metrics

    dashboard = {
        "build": BUILD,
        "generated_at": iso_now(),
        "mode": "PAPER ONLY — LIVE ORDERS DISABLED",
        "exchange_data": "Hyperliquid public API",
        "symbol": "BTC perpetual",
        "price": price,
        "funding_rate": market["funding"],
        "funding_rate_pct": market["funding"] * 100,
        "open_interest_btc": market["open_interest_btc"],
        "premium": market["premium"],
        "volume_24h_usd": market["volume_24h_usd"],
        "signal": signal,
        "metrics": metrics,
        "risk": {
            "virtual_leverage": LEVERAGE,
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100,
            "max_daily_loss_pct": MAX_DAILY_LOSS * 100,
            "max_drawdown_pct": MAX_DRAWDOWN * 100,
            "halt_reason": state.get("halt_reason"),
            "warning": (
                "Paper simulation only. Fees, slippage, execution and "
                "liquidation are simplified. Not suitable for live trading."
            ),
        },
        "open_trade": state.get("open_trade"),
        "recent_trades": state["trades"][-20:],
        "equity_history": state["equity_history"][-200:],
        "fib": signal.get("fib", {}),
        "fvgs": signal.get("fvgs", []),
        "macro_note": (
            "経済指標は自動取得していません。CPI、雇用統計、"
            "FOMCは別途確認してください。"
        ),
        "method_notes": [
            "確定済みローソク足を使用",
            "Wilder RSI・ATR",
            "1h・4h EMAと15mモメンタム",
            "1h・4hトレンド不一致時は新規エントリーを見送り",
            "仮想手数料とスリッページを概算",
            "検証指標: 勝率、平均利益・損失、PF、期待値",
            "実注文・ウォレット接続なし",
        ],
    }

    save_json(STATE_FILE, state)
    save_json(DASH_FILE, dashboard)

    print(json.dumps({
        "ok": True,
        "build": BUILD,
        "price": price,
        "signal": signal["direction"],
        "score": signal["score"],
        "equity": metrics["equity_usd"],
        "closed_trades": metrics["closed_trades"],
        "profit_factor": metrics["profit_factor"],
        "halt": state.get("halt_reason"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "build": BUILD,
            "error": str(exc),
            "time": iso_now(),
        }, ensure_ascii=False))
        raise
