#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chart_data.py — ダッシュボード「チャート分析」用データ生成スクリプト。

GitHub Actions (btc-paper.yml) で bot.py の直後に実行され、Hyperliquid
公開APIから BTC の各時間足ローソクを取得し、自動ライン（水平線・
トレンドライン・平行チャネル）を算出して docs/data.json の "chart"
キーへ書き込む。取引ロジック (bot.py) には一切触れない。

失敗してもワークフロー全体を止めないよう、例外時は data.json を
変更せず終了コード 0 で終わる（チャートは前回分が残るだけ）。

ライン算出の根拠:
  * スイングポイント = 前後 k 本の中で最高値/最安値になる足（フラクタル）。
    k は 1h/4h で 2、それ以外で 3。
  * レジスタンス = 現在値より上の直近スイング高値（最大2本）、
    サポート = 現在値より下の直近スイング安値（最大2本）。
    該当が無い場合は直近のものを1本だけ使う。近すぎる重複は除く。
  * トレンドライン = 直近2つのスイング高値同士 / 安値同士を結び、
    チャート右端の少し先まで延長した線。
  * 平行チャネル = 高値線と安値線の傾きの符号が同じで傾きの差が
    40% 以内のときだけ、主トレンドラインと平行な線を反対側の
    スイング極値に当てて引く。
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "docs" / "data.json"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"

# interval -> (表示用のおおよその足の長さ[ms], スイング検出の窓 k, 取得本数)
TIMEFRAMES = {
    "1h": (3_600_000, 2, 150),
    "4h": (14_400_000, 2, 150),
    "1d": (86_400_000, 3, 150),
    "1w": (604_800_000, 3, 150),
    "1M": (2_592_000_000, 3, 150),  # 月足は約30日として取得範囲を見積もる
}
MIN_CANDLES = {"1h": 40, "4h": 40, "1d": 40, "1w": 20, "1M": 12}


def post(payload, timeout=20):
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_candles(interval, span_ms, count):
    """確定済みローソクだけを [t(秒), o, h, l, c] の配列で返す。"""
    now_ms = int(time.time() * 1000)
    result = post(
        {
            "type": "candleSnapshot",
            "req": {
                "coin": COIN,
                "interval": interval,
                "startTime": now_ms - span_ms * (count + 5),
                "endTime": now_ms,
            },
        }
    )
    candles = []
    for raw in result or []:
        try:
            t_open = int(raw["t"])
            t_close = int(raw["T"])
            o, h, l, c = (float(raw[k]) for k in ("o", "h", "l", "c"))
        except (KeyError, TypeError, ValueError):
            continue
        # 未確定の足（終値時刻が未来）は除外する
        if t_close > now_ms or c <= 0 or h < l:
            continue
        candles.append([t_open // 1000, round(o, 1), round(h, 1), round(l, 1), round(c, 1)])
    candles.sort(key=lambda x: x[0])
    # 時刻の重複を除く
    deduped = []
    for cd in candles:
        if deduped and deduped[-1][0] == cd[0]:
            deduped[-1] = cd
        else:
            deduped.append(cd)
    return deduped[-count:]


def swing_points(candles, k):
    """フラクタルでスイング高値/安値を検出。(index, time, price) のリスト。"""
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        window = candles[i - k : i + k + 1]
        hi = candles[i][2]
        lo = candles[i][3]
        if all(hi >= w[2] for w in window) and any(hi > w[2] for w in window if w is not candles[i]):
            highs.append((i, candles[i][0], hi))
        if all(lo <= w[3] for w in window) and any(lo < w[3] for w in window if w is not candles[i]):
            lows.append((i, candles[i][0], lo))
    return highs, lows


def distinct_recent(points, limit=2, min_gap_ratio=0.001):
    """直近のスイングから、価格が近すぎる重複を除いて最大 limit 個返す。"""
    picked = []
    for p in reversed(points):
        if all(abs(p[2] - q[2]) / q[2] > min_gap_ratio for q in picked):
            picked.append(p)
        if len(picked) >= limit:
            break
    return picked


def trend_line(points):
    """直近2つのスイングを結ぶ線。(t1, p1, t2, p2) / 引けなければ None。

    2点が近すぎる（5本未満）場合は、もう1つ前のスイングを使う。
    """
    if len(points) < 2:
        return None
    a = points[-1]
    b = points[-2]
    if a[0] - b[0] < 5 and len(points) >= 3:
        b = points[-3]
    if a[0] - b[0] < 2:
        return None
    return (b[1], b[2], a[1], a[2])


def extend(line, t_end):
    """線を t_end まで延長した2点 [t, price] を返す。"""
    t1, p1, t2, p2 = line
    if t2 == t1:
        return None
    slope = (p2 - p1) / (t2 - t1)
    return [[t1, round(p1, 1)], [t_end, round(p1 + slope * (t_end - t1), 1)]]


def build_lines(candles, k):
    highs, lows = swing_points(candles, k)
    last_t = candles[-1][0]
    first_t = candles[0][0]
    last_close = candles[-1][4]
    t_end = last_t + int((last_t - first_t) * 0.12)

    # レジスタンスは現在値より上のスイング高値、サポートは現在値より下の
    # スイング安値を優先する（該当が無ければ直近のものを1本だけ使う）。
    res_points = distinct_recent([p for p in highs if p[2] >= last_close])
    if not res_points:
        res_points = distinct_recent(highs, limit=1)
    sup_points = distinct_recent([p for p in lows if p[2] <= last_close])
    if not sup_points:
        sup_points = distinct_recent(lows, limit=1)
    resistance = [round(p[2], 1) for p in res_points]
    support = [round(p[2], 1) for p in sup_points]

    high_line = trend_line(highs)
    low_line = trend_line(lows)
    trend_high = extend(high_line, t_end) if high_line else None
    trend_low = extend(low_line, t_end) if low_line else None

    # 平行チャネル: 高値線と安値線の傾きの符号が一致し、差が40%以内のときだけ。
    channel = None
    if high_line and low_line:
        sh = (high_line[3] - high_line[1]) / (high_line[2] - high_line[0])
        sl = (low_line[3] - low_line[1]) / (low_line[2] - low_line[0])
        if sh != 0 and sl != 0 and (sh > 0) == (sl > 0):
            base = max(abs(sh), abs(sl))
            if abs(sh - sl) / base <= 0.4:
                if sl > 0:
                    # 上昇チャネル: 安値線と平行に、区間内の最高スイング高値を通る線
                    span = [p for p in highs if low_line[0] <= p[1] <= last_t]
                    if span:
                        peak = max(span, key=lambda p: p[2])
                        offset = peak[2] - (low_line[1] + sl * (peak[1] - low_line[0]))
                        channel = {
                            "kind": "up",
                            "points": [
                                [low_line[0], round(low_line[1] + offset, 1)],
                                [t_end, round(low_line[1] + sl * (t_end - low_line[0]) + offset, 1)],
                            ],
                        }
                else:
                    # 下降チャネル: 高値線と平行に、区間内の最安スイング安値を通る線
                    span = [p for p in lows if high_line[0] <= p[1] <= last_t]
                    if span:
                        trough = min(span, key=lambda p: p[2])
                        offset = trough[2] - (high_line[1] + sh * (trough[1] - high_line[0]))
                        channel = {
                            "kind": "down",
                            "points": [
                                [high_line[0], round(high_line[1] + offset, 1)],
                                [t_end, round(high_line[1] + sh * (t_end - high_line[0]) + offset, 1)],
                            ],
                        }

    return {
        "resistance": resistance,
        "support": support,
        "trend_high": trend_high,
        "trend_low": trend_low,
        "channel": channel,
    }


def main():
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"chart_data: data.json を読めませんでした: {exc}")
        return 0

    timeframes = {}
    for interval, (span_ms, k, count) in TIMEFRAMES.items():
        try:
            candles = fetch_candles(interval, span_ms, count)
            if len(candles) < MIN_CANDLES[interval]:
                print(f"chart_data: {interval} は足が不足 ({len(candles)}本) のためスキップ")
                continue
            timeframes[interval] = {
                "candles": candles,
                "lines": build_lines(candles, k),
            }
            print(f"chart_data: {interval} {len(candles)}本 OK")
        except Exception as exc:  # 時間足単位で失敗を隔離する
            print(f"chart_data: {interval} の取得に失敗: {exc}")

    if not timeframes:
        print("chart_data: 有効な時間足が無いため data.json は変更しません")
        return 0

    data["chart"] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "Hyperliquid candleSnapshot",
        "timeframes": timeframes,
    }
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    print("chart_data: data.json に chart を書き込みました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
