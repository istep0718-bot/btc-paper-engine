#!/usr/bin/env python3
"""BTC Paper Engine V2. Paper trading only. No live orders or wallet keys."""

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

BUILD = "BTC_PAPER_V2_2026-10-20"

INITIAL_EQUITY = 20.0
LEVERAGE = 3.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.10
MAX_DAILY_LOSS_FRACTION = 0.03
MAX_DRAWDOWN_FRACTION = 0.10

FEE_RATE_PER_SIDE = 0.0005
SLIPPAGE_RATE_PER_SIDE = 0.0002
MAX_HOLD_HOURS = 24
MIN_NOTIONAL = 1.0
MAX_TRADES_STORED = 500


def now():
    return datetime.now(timezone.utc)


def iso_now():
    return now().isoformat()


def now_ms():
    return int(time.time() * 1000)


def api_post(payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Research/2.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def candles(interval, hours):
    end = now_ms()
    raw = api_post({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": end - hours * 3600000,
            "endTime": end,
        },
    })

    if not isinstance(raw, list):
        raise RuntimeError(f"Invalid candle response: {interval}")

    result = []
    for item in raw:
        try:
            c = {
                "t": int(item["t"]),
                "T": int(item.get("T", item["t"])),
                "o": float(item["o"]),
                "h": float(item["h"]),
                "l": float(item["l"]),
                "c": float(item["c"]),
                "v": float(item["v"]),
            }
            values = [c[k] for k in ("o", "h", "l", "c", "v")]
            if all(math.isfinite(x) and x > 0 for x in values):
                result.append(c)
        except (KeyError, TypeError, ValueError):
            continue

    result.sort(key=lambda x: x["t"])

    # Use only candles whose reported close time has passed.
    timestamp = now_ms()
    result = [c for c in result if c["T"] <= timestamp]

    # Remove duplicate timestamps.
    unique = {}
    for c in result:
        unique[c["t"]] = c

    return sorted(unique.values(), key=lambda x: x["t"])


def ema(values, period):
    if len(values) < period:
        return None

    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1.0)

    for x in values[period:]:
        value = alpha * x + (1.0 - alpha) * value

    return value


def rsi(values, period=14):
    """Wilder RSI."""
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0.0) for x in changes]
    losses = [max(-x, 0.0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(cs, period=14):
    """Wilder ATR."""
    if len(cs) < period + 1:
        return None

    ranges = []
    for i in range(1, len(cs)):
        cur = cs[i]
        prev = cs[i - 1]

        ranges.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))

    value = sum(ranges[:period]) / period

    for x in ranges[period:]:
        value = (value * (period - 1) + x) / period

    return value


def fib_levels(cs, lookback=96):
    chunk = cs[-lookback:]
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


def find_fvg(cs, price):
    gaps = []

    for i in range(2, len(cs)):
        a = cs[i - 2]
        c = cs[i]

        if c["l"] > a["h"]:
            gaps.append({
                "type": "bullish FVG",
                "low": round(a["h"], 2),
                "high": round(c["l"], 2),
                "formed_at": c["t"],
            })

        if c["h"] < a["l"]:
            gaps.append({
                "type": "bearish FVG",
                "low": round(c["h"], 2),
                "high": round(a["l"], 2),
                "formed_at": c["t"],
            })

    active = []

    for gap in gaps[-30:]:
        if (
            gap["type"] == "bullish FVG"
            and price >= gap["low"]
        ):
            active.append(gap)
        elif (
            gap["type"] == "bearish FVG"
            and price <= gap["high"]
        ):
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
            "date": now().date().isoformat(),
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
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise RuntimeError(
            "state.json could not be read. "
            "Preserve the file and inspect it before continuing."
        ) from exc

    if not isinstance(state, dict):
        raise RuntimeError("state.json is not a JSON object.")

    # Preserve old records and positions during migration.
    state.setdefault("cash", INITIAL_EQUITY)
    state.setdefault("peak_equity", float(state["cash"]))
    state.setdefault("trades", [])
    state.setdefault("open_trade", None)
    state.setdefault("daily", {
        "date": now().date().isoformat(),
        "start_equity": float(state["cash"]),
    })
    state.setdefault("last_signal_key", None)
    state.setdefault("halt_reason", None)
    state.setdefault("equity_history", [])

    state["build"] = BUILD
    return state


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temp.replace(path)


def market_context():
    result = api_post({"type": "metaAndAssetCtxs"})

    if not isinstance(result, list) or len(result) < 2:
        raise RuntimeError("Invalid market metadata response.")

    meta, contexts = result[0], result[1]
    universe = meta.get("universe", [])

    index = next(
        (
            i for i, asset in enumerate(universe)
            if asset.get("name") == "BTC"
        ),
        None,
    )

    if index is None or index >= len(contexts):
        raise RuntimeError("BTC market data not found.")

    ctx = contexts[index]

    c15 = candles("15m", 24 * 10)
    c1 = candles("1h", 24 * 45)
    c4 = candles("4h", 24 * 120)

    if min(len(c15), len(c1), len(c4)) < 60:
        raise RuntimeError(
            f"Insufficient closed candles: "
            f"15m={len(c15)}, 1h={len(c1)}, 4h={len(c4)}"
        )

    price = float(ctx.get("markPx") or c15[-1]["c"])

    if not math.isfinite(price) or price <= 0:
        raise RuntimeError("Invalid BTC price.")

    return {
        "price": price,
        "funding": float(ctx.get("funding") or 0),
        "open_interest_btc": float(ctx.get("openInterest") or 0),
        "premium": float(ctx.get("premium") or 0),
        "volume_24h_usd": float(ctx.get("dayNtlVlm") or 0),
        "candles_15m": c15,
        "candles_1h": c1,
        "candles_4h": c4,
    }


def make_signal(m):
    c15 = m["candles_15m"]
    c1 = m["candles_1h"]
    c4 = m["candles_4h"]

    cl15 = [c["c"] for c in c15]
    cl1 = [c["c"] for c in c1]
    cl4 = [c["c"] for c in c4]

    e20_1 = ema(cl1, 20)
    e50_1 = ema(cl1, 50)
    e20_4 = ema(cl4, 20)
    e50_4 = ema(cl4, 50)

    r = rsi(cl15)
    a = atr(c15)
    price = m["price"]
    fib = fib_levels(c1)
    fvgs = find_fvg(c15, price)

    signal = {
        "direction": "WAIT",
        "score": 0,
        "reasons": [],
        "price": price,
        "rsi_15m": r,
        "atr_15m": a,
        "ema20_1h": e20_1,
        "ema50_1h": e50_1,
        "ema20_4h": e20_4,
        "ema50_4h": e50_4,
        "fib": fib,
        "fvgs": fvgs,
    }

    indicators = [e20_1, e50_1, e20_4, e50_4, r, a]

    if (
        any(x is None or not math.isfinite(x) for x in indicators)
        or a <= 0
        or not fib
    ):
        signal["reasons"] = [
            "指標データ不足または異常値のため見送り"
        ]
        return signal

    score = 0
    reasons = []

    if e20_1 > e50_1:
        score += 1
        reasons.append("1時間足：EMA20がEMA50より上")
    else:
        score -= 1
        reasons.append("1時間足：EMA20がEMA50より下")

    if e20_4 > e50_4:
        score += 1
        reasons.append("4時間足：EMA20がEMA50より上")
    else:
        score -= 1
        reasons.append("4時間足：EMA20がEMA50より下")

    if price > e20_1:
        score += 1
        reasons.append("価格は1時間足EMA20より上")
    else:
        score -= 1
        reasons.append("価格は1時間足EMA20より下")

    if 50 <= r <= 67:
        score += 1
        reasons.append(f"RSI {r:.1f}：上向きモメンタム候補")
    elif 33 <= r < 50:
        score -= 1
        reasons.append(f"RSI {r:.1f}：弱めのモメンタム")
    elif r > 75:
        reasons.append(f"RSI {r:.1f}：過熱のため追いかけ回避")
    elif r < 25:
        reasons.append(f"RSI {r:.1f}：売られ過ぎの可能性")

    funding = m["funding"]

    if funding > 0.0003:
        score -= 1
        reasons.append("資金調達率が高め")
    elif funding < -0.0003:
        score += 1
        reasons.append("資金調達率が低め")
    else:
        reasons.append("資金調達率に極端な偏りなし")

    move = abs(c15[-1]["c"] - c15[-2]["c"]) / c15[-2]["c"]

    if move > 0.012:
        reasons.append("直近確定15分足の変動が大きいため見送り")

    direction = "WAIT"

    if score >= 3 and 30 < r < 72 and move <= 0.012:
        direction = "LONG"
    elif score <= -3 and 28 < r < 70 and move <= 0.012:
        direction = "SHORT"
    else:
        reasons.append(f"総合スコア {score}：条件不足、待機")

    signal.update({
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "candle_move_pct": round(move * 100, 4),
    })

    return signal


def unrealized_pnl(trade, price):
    direction = 1 if trade["direction"] == "LONG" else -1

    return (
        float(trade["notional"])
        * direction
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


def fill_price(price, direction, entering):
    sign = 1 if direction == "LONG" else -1
    adjustment = sign if entering else -sign

    return price * (
        1 + adjustment * SLIPPAGE_RATE_PER_SIDE
    )


def close_trade(state, price, reason):
    trade = state.get("open_trade")

    if not trade:
        return

    exit_price = fill_price(
        price, trade["direction"], entering=False
    )

    gross = unrealized_pnl(trade, exit_price)
    fees = float(trade["notional"]) * FEE_RATE_PER_SIDE * 2
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
            net / max(float(trade["margin"]), 1e-9) * 100,
            3,
        ),
    })

    state.setdefault("trades", []).append(trade)
    state["trades"] = state["trades"][-MAX_TRADES_STORED:]
    state["open_trade"] = None


def halt_reason(state, eq):
    peak = max(float(state["peak_equity"]), eq)
    drawdown = (peak - eq) / max(peak, 1e-9)

    start = float(state["daily"]["start_equity"])
    daily_loss = (start - eq) / max(start, 1e-9)

    if eq <= 0:
        return "EQUITY_DEPLETED"
    if drawdown >= MAX_DRAWDOWN_FRACTION:
        return "MAX_DRAWDOWN_10_PERCENT"
    if daily_loss >= MAX_DAILY_LOSS_FRACTION:
        return "DAILY_LOSS_3_PERCENT"

    return None


def run():
    state = load_state()
    market = market_context()
    signal = make_signal(market)
    price = market["price"]

    today = now().date().isoformat()

    if state["daily"].get("date") != today:
        state["daily"] = {
            "date": today,
            "start_equity": equity(state, price),
        }

        if state.get("halt_reason") == "DAILY_LOSS_3_PERCENT":
            state["halt_reason"] = None

    eq = equity(state, price)
    state["peak_equity"] = max(
        float(state["peak_equity"]), eq
    )

    reason = halt_reason(state, eq)
    if reason:
        state["halt_reason"] = reason

    # Manage an existing paper position even during a halt.
    trade = state.get("open_trade")

    if trade:
        direction = 1 if trade["direction"] == "LONG" else -1
        move = (
            direction
            * (price - float(trade["entry_price"]))
            / float(trade["entry_price"])
        )

        if move <= -0.08:
            close_trade(state, price, "EMERGENCY_LOSS_GUARD")
        elif (
            direction == 1 and price <= float(trade["stop_price"])
        ) or (
            direction == -1 and price >= float(trade["stop_price"])
        ):
            close_trade(state, price, "STOP_LOSS")
        elif (
            direction == 1 and price >= float(trade["target_price"])
        ) or (
            direction == -1 and price <= float(trade["target_price"])
        ):
            close_trade(state, price, "TAKE_PROFIT")
        elif (
            now_ms() - int(trade.get("opened_at_ms", now_ms()))
            >= MAX_HOLD_HOURS * 3600000
        ):
            close_trade(state, price, "MAX_HOLD_24H")

    eq = equity(state, price)
    state["peak_equity"] = max(
        float(state["peak_equity"]), eq
    )

    reason = halt_reason(state, eq)
    if reason:
        state["halt_reason"] = reason

    # Entry sizing uses equity risk and a margin cap.
    if (
        not state.get("open_trade")
        and not state.get("halt_reason")
        and signal["direction"] in ("LONG", "SHORT")
    ):
        candle_time = market["candles_15m"][-1]["t"]
        key = f'{signal["direction"]}:{candle_time}'

        if state.get("last_signal_key") != key:
            atr_value = float(signal.get("atr_15m") or 0)
            stop_dist = max(1.5 * atr_value / price, 0.005)
            target_dist = stop_dist * 1.8

            risk_usd = max(0.0, eq * RISK_PER_TRADE)
            notional_risk = risk_usd / stop_dist
            margin_cap = max(0.0, eq * MAX_MARGIN_FRACTION)
            notional = min(
                notional_risk,
                margin_cap * LEVERAGE,
            )
            margin = notional / LEVERAGE

            if (
                not math.isfinite(notional)
                or notional < MIN_NOTIONAL
                or margin > float(state["cash"])
            ):
                signal["reasons"].append(
                    "取引サイズまたは資金制限により見送り"
                )
                signal["direction"] = "WAIT"
            else:
                direction = (
                    1 if signal["direction"] == "LONG" else -1
                )
                entry = fill_price(
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
                    "stop_distance_pct": round(
                        stop_dist * 100, 3
                    ),
                    "signal_score": signal["score"],
                    "entry_reasons": signal["reasons"],
                }

                state["last_signal_key"] = key

    eq = equity(state, price)
    state["peak_equity"] = max(
        float(state["peak_equity"]), eq
    )

    state["last_run_at"] = iso_now()
    state["last_price"] = price
    state["last_signal"] = signal

    history = state.setdefault("equity_history", [])
    history.append({
        "time": state["last_run_at"],
        "equity_usd": round(eq, 6),
        "price": price,
    })
    state["equity_history"] = history[-2000:]

    trades = state["trades"]
    wins = sum(
        1 for t in trades if float(t.get("net_pnl_usd", 0)) > 0
    )
    losses = sum(
        1 for t in trades if float(t.get("net_pnl_usd", 0)) <= 0
    )

    drawdown = (
        (float(state["peak_equity"]) - eq)
        / max(float(state["peak_equity"]), 1e-9)
        * 100
    )

    state["metrics"] = {
        "equity_usd": round(eq, 6),
        "cash_usd": round(float(state["cash"]), 6),
        "peak_equity_usd": round(float(state["peak_equity"]), 6),
        "drawdown_pct": round(drawdown, 3),
        "closed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(
            wins / len(trades) * 100, 2
        ) if trades else 0.0,
        "net_closed_pnl_usd": round(
            sum(float(t.get("net_pnl_usd", 0)) for t in trades),
            6,
        ),
        "open_unrealized_pnl_usd": round(
            unrealized_pnl(state["open_trade"], price), 6
        ) if state.get("open_trade") else 0.0,
    }

    dashboard = {
        "build": BUILD,
        "mode": "PAPER ONLY — LIVE ORDERS DISABLED",
        "generated_at": iso_now(),
        "exchange_data": "Hyperliquid public API",
        "symbol": "BTC perpetual",
        "price": price,
        "funding_rate": market["funding"],
        "funding_rate_pct": market["funding"] * 100,
        "open_interest_btc": market["open_interest_btc"],
        "premium": market["premium"],
        "volume_24h_usd": market["volume_24h_usd"],
        "signal": signal,
        "metrics": state["metrics"],
        "risk": {
            "virtual_leverage": LEVERAGE,
            "risk_per_trade_pct": RISK_PER_TRADE * 100,
            "max_margin_pct": MAX_MARGIN_FRACTION * 100,
            "halt_reason": state.get("halt_reason"),
            "warning": (
                "Paper simulation only. Fees, slippage and liquidation "
                "are simplified; not for live trading."
            ),
        },
        "open_trade": state.get("open_trade"),
        "recent_trades": trades[-20:],
        "equity_history": state["equity_history"][-200:],
        "macro_note": (
            "経済指標は自動取得していません。CPI、米雇用統計、"
            "FOMCなどは別途確認してください。"
        ),
        "method_notes": [
            "確定済みローソク足を使用",
            "Wilder RSI・Wilder ATR",
            "1h・4h EMAトレンドと15mモメンタム",
            "FVGは参考候補であり利益を保証しない",
            "仮想手数料・スリッページを概算",
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
        "equity": state["metrics"]["equity_usd"],
        "closed_trades": state["metrics"]["closed_trades"],
        "halt": state.get("halt_reason"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "time": iso_now(),
        }, ensure_ascii=False))
        raise
