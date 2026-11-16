#!/usr/bin/env python3
"""HL whale-aggregate shadow logger (measurement only).

Once per run this script:
  1. Downloads the public Hyperliquid leaderboard (static file on
     stats-data.hyperliquid.xyz, no auth, no API key) and takes the
     TOP_N addresses by account value, skipping known protocol vaults.
  2. Reads each address's BTC perp position from the public info API
     (clearinghouseState) and aggregates the cohort:
       - net signed notional (long USD total - short USD total)
       - long / short / flat address counts
       - gross long / gross short notionals
  3. Appends one JSON Lines record to docs/whale_shadow.jsonl.

It exists to measure whether the aggregate positioning of the largest
Hyperliquid accounts *leads* BTC price, in the same spirit as the
HL-8888 direction-signal shadow (hl8888_shadow.py), which it rides
along with. It is fully independent of bot.py: it never reads or
writes the paper-trading state and places no orders.

Persistence note: the HL-8888 Shadow Logger workflow only stages
docs/hl8888_shadow.jsonl in its own commit step, and workflow files
cannot be edited through the GitHub API (the Muse app has no
workflows permission). So when running inside GitHub Actions this
script commits docs/whale_shadow.jsonl itself (plus the HL-8888 log,
which by then has already been appended by hl8888_shadow.py) and
pushes with a rebase-and-retry loop. The workflow's later commit step
then finds the HL-8888 log already committed and exits as a no-op.
Outside GitHub Actions the git step is skipped entirely; the caller
is responsible for committing the appended record.

Record fields per line:
  ts               unix seconds (UTC) of the sample
  ts_iso           ISO-8601 UTC timestamp
  btc_mid          BTC mid price at sample time (USD, from allMids)
  selection        how the cohort was picked ("leaderboard_account_value")
  n_addresses      cohort size (TOP_N)
  n_with_position  addresses holding a non-zero BTC position
  long_count       addresses net long BTC
  short_count      addresses net short BTC
  flat_count       addresses with no BTC position
  gross_long_usd   sum of long BTC notionals (USD, positive)
  gross_short_usd  sum of short BTC notionals (USD, positive)
  net_usd          gross_long_usd - gross_short_usd
  net_btc          signed sum of BTC position sizes (coins)
  largest          {address, usd} of the biggest single BTC position
                   by absolute notional (null when nobody is positioned)
  positions        [{address, account_value, szi, usd, entry_px, upnl}]
                   for every cohort address (usd is signed; 0.0 = flat)
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "docs", "whale_shadow.jsonl")
TOP_N = 30
# Protocol-owned vaults are not directional traders; skip them so the
# cohort stays at TOP_N actual trading accounts.
EXCLUDE_ADDRESSES = {
    "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303",  # HLP vault
}
PAUSE_SEC = 0.15  # between info API calls; ~31 calls per run is trivial
USER_AGENT = "Mozilla/5.0 (compatible; whale-shadow-logger/1.0)"


class RateLimited(Exception):
    """Raised on HTTP 429 so the caller can stop instead of hammering."""


def _open(req, timeout):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(f"HTTP 429 from {req.full_url}") from exc
        raise


def fetch_leaderboard():
    req = urllib.request.Request(
        LEADERBOARD_URL, headers={"User-Agent": USER_AGENT})
    return json.loads(_open(req, timeout=60))


def post_info(payload):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
    )
    return json.loads(_open(req, timeout=30))


def pick_cohort(rows):
    ranked = sorted(rows, key=lambda r: float(r["accountValue"]),
                    reverse=True)
    cohort = []
    for row in ranked:
        addr = row["ethAddress"].lower()
        if addr in EXCLUDE_ADDRESSES:
            continue
        cohort.append({
            "address": row["ethAddress"],
            "account_value": float(row["accountValue"]),
        })
        if len(cohort) >= TOP_N:
            break
    return cohort


def btc_position(state):
    for entry in state.get("assetPositions", []):
        pos = entry["position"]
        if pos["coin"] == "BTC":
            szi = float(pos["szi"])
            return {
                "szi": szi,
                "usd": float(pos["positionValue"]) * (1 if szi >= 0 else -1),
                "entry_px": float(pos["entryPx"]) if pos.get("entryPx") else None,
                "upnl": float(pos.get("unrealizedPnl", 0.0)),
            }
    return {"szi": 0.0, "usd": 0.0, "entry_px": None, "upnl": 0.0}


def collect():
    rows = fetch_leaderboard()["leaderboardRows"]
    cohort = pick_cohort(rows)
    mids = post_info({"type": "allMids"})

    positions = []
    for member in cohort:
        state = post_info({"type": "clearinghouseState",
                           "user": member["address"]})
        block = btc_position(state)
        positions.append({
            "address": member["address"],
            "account_value": member["account_value"],
            **block,
        })
        time.sleep(PAUSE_SEC)

    gross_long = sum(p["usd"] for p in positions if p["usd"] > 0)
    gross_short = -sum(p["usd"] for p in positions if p["usd"] < 0)
    positioned = [p for p in positions if p["usd"] != 0]
    largest = None
    if positioned:
        top = max(positioned, key=lambda p: abs(p["usd"]))
        largest = {"address": top["address"], "usd": top["usd"]}

    now = int(time.time())
    return {
        "ts": now,
        "ts_iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "btc_mid": float(mids["BTC"]) if mids.get("BTC") else None,
        "selection": "leaderboard_account_value",
        "n_addresses": len(positions),
        "n_with_position": len(positioned),
        "long_count": sum(1 for p in positions if p["usd"] > 0),
        "short_count": sum(1 for p in positions if p["usd"] < 0),
        "flat_count": sum(1 for p in positions if p["usd"] == 0),
        "gross_long_usd": gross_long,
        "gross_short_usd": gross_short,
        "net_usd": gross_long - gross_short,
        "net_btc": sum(p["szi"] for p in positions),
        "largest": largest,
        "positions": positions,
    }


def append_record(record):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          timeout=120)


def self_commit():
    """Commit the whale log from inside GitHub Actions (see module doc).

    No-op outside Actions. Every failure is reported on stderr and
    swallowed: losing one hourly whale sample must never fail the run.
    The HL-8888 log is staged too because hl8888_shadow.py has already
    appended its sample by the time this runs; the workflow's own
    commit step then cleanly no-ops.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    repo_root = os.path.dirname(os.path.abspath(__file__))
    try:
        os.chdir(repo_root)
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email",
             "41898282+github-actions[bot]@users.noreply.github.com")
        _git("add", "docs/whale_shadow.jsonl", "docs/hl8888_shadow.jsonl")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return
        commit = _git("commit", "-m", "Whale shadow: append hourly sample")
        if commit.returncode != 0:
            print(f"whale_shadow: self-commit failed: {commit.stderr}",
                  file=sys.stderr)
            return
        for attempt in range(3):
            push = _git("push", "origin", "HEAD:main")
            if push.returncode == 0:
                return
            print(f"whale_shadow: push attempt {attempt + 1} failed, "
                  "rebasing", file=sys.stderr)
            _git("fetch", "origin", "main")
            rebase = _git("rebase", "origin/main")
            if rebase.returncode != 0:
                _git("rebase", "--abort")
                print(f"whale_shadow: rebase failed: {rebase.stderr}",
                      file=sys.stderr)
                return
            time.sleep(5)
        _git("push", "origin", "HEAD:main")
    except Exception as exc:  # noqa: BLE001 - must never break the caller
        print(f"whale_shadow: self-commit skipped ({exc})", file=sys.stderr)


def run():
    record = collect()
    append_record(record)
    print(json.dumps(record, ensure_ascii=False))
    self_commit()
    return record


def main():
    try:
        run()
    except RateLimited as exc:
        print(f"whale_shadow: rate limited, stopping: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
