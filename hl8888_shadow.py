#!/usr/bin/env python3
"""HL-8888 direction-signal shadow logger (measurement only).

Reads the public Hyperliquid clearinghouse state for the HL-8888 address
once per run and appends one JSON Lines record to docs/hl8888_shadow.jsonl.

This script is fully independent of bot.py: it never reads or writes the
paper-trading state, places no orders, and uses only the read-only public
info API. It exists to measure whether HL-8888's BTC/ETH net-position
direction *leads* BTC price (see the shadow measurement spec dated
2026-11-10 in the F/D Stage 0 goal files).

Record fields per line:
  ts            unix seconds (UTC) of the sample
  ts_iso        ISO-8601 UTC timestamp
  account_value total perp account value (USD)
  total_ntl_pos gross notional of all positions (USD)
  btc_mid       BTC mid price at sample time (USD, from allMids)
  eth_mid       ETH mid price at sample time (USD, from allMids)
  btc           {szi (signed coins), usd (signed notional), entry_px, upnl}
  eth           {szi, usd, entry_px, upnl}
  others        [{coin, szi, usd}] for any remaining open positions
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ADDRESS = "0x418aa6bf98a2b2bc93779f810330d88cde488888"
API_URL = "https://api.hyperliquid.xyz/info"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "docs", "hl8888_shadow.jsonl")


def post_info(payload):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def position_block(pos):
    if pos is None:
        return {"szi": 0.0, "usd": 0.0, "entry_px": None, "upnl": 0.0}
    return {
        "szi": float(pos["szi"]),
        "usd": float(pos["positionValue"]) * (1 if float(pos["szi"]) >= 0 else -1),
        "entry_px": float(pos["entryPx"]) if pos.get("entryPx") else None,
        "upnl": float(pos.get("unrealizedPnl", 0.0)),
    }


def main():
    state = post_info({"type": "clearinghouseState", "user": ADDRESS})
    mids = post_info({"type": "allMids"})

    positions = {}
    others = []
    for entry in state.get("assetPositions", []):
        pos = entry["position"]
        coin = pos["coin"]
        if coin in ("BTC", "ETH"):
            positions[coin] = pos
        else:
            szi = float(pos["szi"])
            others.append({
                "coin": coin,
                "szi": szi,
                "usd": float(pos["positionValue"]) * (1 if szi >= 0 else -1),
            })

    now = int(time.time())
    record = {
        "ts": now,
        "ts_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "account_value": float(state["marginSummary"]["accountValue"]),
        "total_ntl_pos": float(state["marginSummary"]["totalNtlPos"]),
        "btc_mid": float(mids["BTC"]) if mids.get("BTC") else None,
        "eth_mid": float(mids["ETH"]) if mids.get("ETH") else None,
        "btc": position_block(positions.get("BTC")),
        "eth": position_block(positions.get("ETH")),
        "others": others,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(json.dumps(record, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
