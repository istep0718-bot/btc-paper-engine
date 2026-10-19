
#!/usr/bin/env python3
"""BTC paper-trading engine. No live orders or wallet keys."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DASH_FILE = ROOT / "docs" / "data.json"

BUILD = "BTC_PAPER_V1_2026-10-20"
INITIAL_EQUITY = 20.0
LEVERAGE = 40.0  # 仮想レバレッジのみ
RISK_PER_TRADE = 0.01
MAX_MARGIN_FRACTION = 0.05
MAX_DAILY_LOSS_FRACTION = 0.03
MAX_DRAWDOWN_FRACTION = 0.10
FEE_RATE_PER_SIDE = 0.0005


def now_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def post(payload, timeout=15):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        API,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "BTC-Paper-Research/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def candles(interval, hours):
    end = now_ms()
    raw = post({
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": interval,
            "startTime": end - hours * 3600000,
            "endTime": end,
        },
    })
    out = []
    for c in raw:
        try:
            out.append({
                "t": int(c["t"]),
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c["v"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x["t"])


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value = x * k + value * (1 - k)
    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]
    gains = sum(max(x, 0) for x in changes[-period:]) / period
    losses = sum(max(-x, 0) for x in changes[-period:]) / period
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def atr(cs, period=14):
    if len(cs) < period + 1:
        return None
    trs = []
    for i in range(1, len(cs)):
        cur, prev = cs[i], cs[i - 1]
        trs.append(max(
            cur["h"] - cur["l"],
            abs(cur["h"] - prev["c"]),
            abs(cur["l"] - prev["c"]),
        ))
    return sum(trs[-period:]) / period


def fib_levels(cs, lookback=96):
    chunk = cs[-lookback:]
    if len(chunk) < 10:
        return {}
    high = max(c["h"] for c in chunk)
    low = min(c["l"] for c in chunk)
    span = high - low
    return {
        "swing_high": high,
        "swing_low": low,
        "retracement_382": high - span * 0.382,
        "retracement_500": high - span * 0.5,
        "retracement_618": high - span * 0.618,
    }


def find_fvg(cs):
    found = []
    for i in range(2, len(cs)):
        a, c = cs[i - 2], cs[i]
        if c["l"] > a["h"]:
            found.append({
                "type": "bullish FVG",
                "low": a["h"],
                "high": c["l"],
                "formed_at": c["t"],
            })
        if c["h"] < a["l"]:
            found.append({
                "type": "bearish FVG",
                "low": c["h"],
                "high": a["l"],
                "formed_at": c["t"],
            })

    price = cs[-1]["c"] if cs else 0
    active = []
    for gap in found[-12:]:
        if gap["type"] == "bullish FVG" and price > gap["low"]:
            active.append(gap)
        elif gap["type"] == "bearish FVG" and price < gap["high"]:
            active.append(gap)
    return active[-6:]


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("build") == BUILD:
                return state
        except Exception:
            pass

    return {
        "build": BUILD,
        "created_at": iso_now(),
        "cash": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY,
        "trades": [],
        "open_trade": None,
        "daily": {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "start_equity": INITIAL_EQUITY,
        },
        "last_signal_key": None,
        "halt_reason": None,
    }


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    temp.replace(path)


def market_context():
    result = post({"type": "metaAndAssetCtxs"})
    meta, contexts = result[0], result[1]

    index = next(
        (
            i for i, asset in enumerate(meta.get("universe", []))
            if asset.get("name") == "BTC"
        ),
        None,
    )
    if index is None:
        raise RuntimeError("BTC not found in perpetual market data")

    ctx = contexts[index]
    c15 = candles("15m", 24 * 10)
    c1h = candles("1h", 24 * 45)
    c4h = candles("4h", 24 * 120)

    if min(len(c15), len(c1h), len(c4h)) < 30:
        raise RuntimeError("Not enough candle history; signal cancelled")

    return {
        "price": float(ctx.get("markPx") or c15[-1]["c"]),
        "funding": float(ctx.get("funding", 0)),
        "open_interest_btc": float(ctx.get("openInterest", 0)),
        "premium": float(ctx.get("premium", 0)),
        "volume_24h_usd": float(ctx.get("dayNtlVlm", 0)),
        "candles_15m": c15,
        "candles_1h": c1h,
        "candles_4h": c4h,
    }


def make_signal(m):
    c15 = m["candles_15m"]
    c1h = m["candles_1h"]
    c4h = m["candles_4h"]

    cl15 = [c["c"] for c in c15]
    cl1 = [c["c"] for c in c1h]
    cl4 = [c["c"] for c in c4h]

    e20_1, e50_1 = ema(cl1, 20), ema(cl1, 50)
    e20_4, e50_4 = ema(cl4, 20), ema(cl4, 50)
    r = rsi(cl15)
    a = atr(c15)
    price = m["price"]
    fib = fib_levels(c1h)
    fvgs = find_fvg(c15)

    reasons = []
    score = 0

    if None in (e20_1, e50_1, e20_4, e50_4, r, a) or not a or not fib:
        return {
            "direction": "WAIT",
            "score": 0,
            "reasons": ["指標データ不足のため見送り"],
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

    if 45 <= r <= 68:
        score += 1
        reasons.append(f"15分足RSI {r:.1f}：ロング候補の範囲")
    elif 32 <= r < 45:
        score -= 1
        reasons.append(f"15分足RSI {r:.1f}：弱さが残る")
    elif r > 75:
        reasons.append(f"15分足RSI {r:.1f}：過熱気味、追いかけを避ける")
    elif r < 25:
        reasons.append(f"15分足RSI {r:.1f}：売られ過ぎの可能性")

    funding = m["funding"]
    if funding > 0.0003:
        score -= 1
        reasons.append("資金調達率が高め：ロング偏重の可能性")
    elif funding < -0.0003:
        score += 1
        reasons.append("資金調達率が低め：ショート偏重の可能性")
    else:
        reasons.append("資金調達率に極端な偏りなし")

    candle_move = (
        abs(c15[-1]["c"] - c15[-2]["c"])
        / max(c15[-2]["c"], 1e-9)
    )

    if candle_move > 0.012:
        reasons.append("直近15分足の変動が大きいため見送り")

    direction = "WAIT"
    if score >= 3 and r < 72 and candle_move <= 0.012:
        direction = "LONG"
    elif score <= -3 and r > 28 and candle_move <= 0.012:
        direction = "SHORT"
    else:
        reasons.append(f"総合スコア {score}：条件不足、待機")

    return {
        "direction": direction,
        "score": score,
        "reasons": reasons,
        "price": price,
        "rsi_15m": r,
        "atr_15m": a,
        "ema20_1h": e20_1,
        "ema50_1h": e50_1,
        "ema20_4h": e20_4,
        "ema50_4h": e50_4,
        "fib": fib,
        "fvgs": fvgs,
        "candle_move_pct": candle_move * 100,
    }


def equity(state, price):
    cash = float(state["cash"])
    trade = state.get("open_trade")
    if not trade:
        return cash

    direction = 1 if trade["direction"] == "LONG" else -1
    pnl = (
        trade["notional"]
        * direction
        * (price - trade["entry_price"])
        / trade["entry_price"]
    )
    return cash + trade["margin"] + pnl


def close_trade(state, price, reason):
    trade = state.get("open_trade")
    if not trade:
        return

    direction = 1 if trade["direction"] == "LONG" else -1
    gross = (
        trade["notional"]
        * direction
        * (price - trade["entry_price"])
        / trade["entry_price"]
    )
    fees = trade["notional"] * FEE_RATE_PER_SIDE * 2
    net = gross - fees

    state["cash"] += trade["margin"] + net
    trade.update({
        "exit_price": price,
        "closed_at": iso_now(),
        "close_reason": reason,
        "gross_pnl_usd": round(gross, 6),
        "fees_est_usd": round(fees, 6),
        "net_pnl_usd": round(net, 6),
        "return_on_margin_pct": round(
            net / max(trade["margin"], 1e-9) * 100, 3
        ),
    })
    state["trades"].append(trade)
    state["open_trade"] = None


def run():
    state = load_state()
    market = market_context()
    signal = make_signal(market)
    price = market["price"]

    today = datetime.now(timezone.utc).date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {
            "date": today,
            "start_equity": equity(state, price),
        }

    eq = equity(state, price)
    state["peak_equity"] = max(float(state["peak_equity"]), eq)
    drawdown = (
        state["peak_equity"] - eq
    ) / max(state["peak_equity"], 1e-9)
    daily_loss = (
        float(state["daily"]["start_equity"]) - eq
    ) / max(float(state["daily"]["start_equity"]), 1e-9)

    if drawdown >= MAX_DRAWDOWN_FRACTION:
        state["halt_reason"] = "MAX_DRAWDOWN_10_PERCENT"
    elif daily_loss >= MAX_DAILY_LOSS_FRACTION:
        state["halt_reason"] = "DAILY_LOSS_3_PERCENT"

    trade = state.get("open_trade")
    if trade:
        d = 1 if trade["direction"] == "LONG" else -1
        move = d * (price - trade["entry_price"]) / trade["entry_price"]

        # Approximation only; not the actual exchange liquidation formula.
        liquidation_guard = (1 / LEVERAGE) * 0.88

        if move <= -liquidation_guard:
            close_trade(state, price, "APPROX_40X_LIQUIDATION_GUARD")
        elif (
            (d == 1 and price <= trade["stop_price"])
            or (d == -1 and price >= trade["stop_price"])
        ):
            close_trade(state, price, "STOP_LOSS")
        elif (
            (d == 1 and price >= trade["target_price"])
            or (d == -1 and price <= trade["target_price"])
        ):
            close_trade(state, price, "TAKE_PROFIT")
        elif now_ms() - trade["opened_at_ms"] >= 24 * 3600000:
            close_trade(state, price, "MAX_HOLD_24H")

    eq = equity(state, price)
    state["peak_equity"] = max(float(state["peak_equity"]), eq)

    if (
        not state.get("open_trade")
        and not state.get("halt_reason")
        and signal["direction"] in ("LONG", "SHORT")
    ):
        key = f'{signal["direction"]}:{int(market["candles_15m"][-1]["t"])}'

        if state.get("last_signal_key") != key:
            atr_value = signal.get("atr_15m") or 0
            stop_dist = max(1.4 * atr_value / price, 0.0035)
            target_dist = stop_dist * 1.8
            risk_usd = max(0, eq * RISK_PER_TRADE)
            notional_by_risk = risk_usd / stop_dist if stop_dist else 0
            margin_cap = max(0, eq * MAX_MARGIN_FRACTION)
            notional = min(notional_by_risk, margin_cap * LEVERAGE)
            margin = notional / LEVERAGE

            if stop_dist >= (1 / LEVERAGE * 0.88) or notional < 1.0:
                signal["reasons"].append(
                    "40倍想定の清算リスクまたは取引サイズの条件により見送り"
                )
                signal["direction"] = "WAIT"
            else:
                d = 1 if signal["direction"] == "LONG" else -1
                stop = price * (1 - d * stop_dist)
                target = price * (1 + d * target_dist)

                state["cash"] -= margin
                state["open_trade"] = {
                    "id": key,
                    "direction": signal["direction"],
                    "entry_price": price,
                    "opened_at": iso_now(),
                    "opened_at_ms": now_ms(),
                    "notional": round(notional, 6),
                    "margin": round(margin, 6),
                    "leverage_virtual": LEVERAGE,
                    "stop_price": round(stop, 2),
                    "target_price": round(target, 2),
                    "stop_distance_pct": round(stop_dist * 100, 3),
                    "signal_score": signal["score"],
                    "entry_reasons": signal["reasons"],
                }
                state["last_signal_key"] = key

    eq = equity(state, price)
    state["last_run_at"] = iso_now()
    state["last_price"] = price
    state["last_signal"] = signal

    state["metrics"] = {
        "equity_usd": round(eq, 6),
        "cash_usd": round(state["cash"], 6),
        "peak_equity_usd": round(state["peak_equity"], 6),
        "drawdown_pct": round(
            (state["peak_equity"] - eq)
            / max(state["peak_equity"], 1e-9) * 100, 3
        ),
        "closed_trades": len(state["trades"]),
        "wins": sum(1 for x in state["trades"] if x.get("net_pnl_usd", 0) > 0),
        "losses": sum(1 for x in state["trades"] if x.get("net_pnl_usd", 0) <= 0),
        "net_closed_pnl_usd": round(
            sum(x.get("net_pnl_usd", 0) for x in state["trades"]), 6
        ),
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
            "warning": "Simplified liquidation estimate; not an execution simulator.",
        },
        "open_trade": state.get("open_trade"),
        "recent_trades": state.get("trades", [])[-20:],
        "macro_note": (
            "経済指標は自動取得していません。CPI、米雇用統計、FOMC、"
            "FRB要人発言の前後は手動で確認してください。"
        ),
        "method_notes": [
            "1h・4h EMAトレンド",
            "15m RSI・ATR",
            "1hフィボナッチ水準",
            "15m FVG候補",
            "資金調達率・建玉情報",
            "シグナルは推定であり利益を保証しない",
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
