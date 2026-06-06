#!/usr/bin/env python3
"""Reconcile paper trades with actual Polymarket settlement results via Gamma API.

Usage:
  python3 scripts/reconcile_settlements.py              # check all unsettled
  python3 scripts/reconcile_settlements.py --all         # re-check all trades
  python3 scripts/reconcile_settlements.py --slug btc-updown-5m-1777759800
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPER_TRADES = PROJECT_ROOT / "logs" / "paper_trades.jsonl"
RECONCILED_LOG = PROJECT_ROOT / "logs" / "reconciled_trades.jsonl"

GAMMA_API = "https://gamma-api.polymarket.com"


def get_resolution(slug: str) -> dict:
    """Fetch market resolution from Gamma API."""
    url = f"{GAMMA_API}/markets/slug/{slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": "error", "code": e.code, "slug": slug}
    except Exception as e:
        return {"status": "error", "message": str(e), "slug": slug}

    if isinstance(data, list):
        data = data[0] if data else {}

    closed = data.get("closed", False)
    prices_raw = data.get("outcomePrices")
    outcomes_raw = data.get("outcomes")

    if not prices_raw or not outcomes_raw:
        return {
            "status": "no_prices",
            "closed": closed,
            "slug": slug,
        }

    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    prices = [float(p) for p in prices]

    if not closed:
        return {"status": "not_closed", "outcomes": outcomes, "outcomePrices": prices, "slug": slug}

    max_p = max(prices)
    min_p = min(prices)

    if max_p >= 0.99 and min_p <= 0.01:
        winner_idx = prices.index(max_p)
        return {
            "status": "resolved",
            "winner": outcomes[winner_idx],
            "outcomes": outcomes,
            "outcomePrices": prices,
            "closed": closed,
            "slug": slug,
        }

    return {
        "status": "ambiguous",
        "outcomes": outcomes,
        "outcomePrices": prices,
        "closed": closed,
        "slug": slug,
    }


def load_trades():
    """Load paper trades."""
    if not PAPER_TRADES.exists():
        return []
    trades = []
    with open(PAPER_TRADES) as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))
    return trades


def load_reconciled():
    """Load already reconciled trade slugs."""
    if not RECONCILED_LOG.exists():
        return set()
    reconciled = set()
    with open(RECONCILED_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                d = json.loads(line)
                reconciled.add(d.get("slug", "") + d.get("side", "") + str(d.get("entry_price", "")))
    return reconciled


def save_reconciled(entry):
    """Save a reconciled result."""
    with open(RECONCILED_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def calculate_pnl(trade, resolution):
    """Calculate actual PnL from trade + settlement result."""
    slug = trade.get("slug", "")
    side = trade.get("side", "")
    entry_price = trade.get("entry_price", trade.get("fill_price", 0))
    fill_price = trade.get("fill_price", entry_price)
    size = trade.get("size", 8)
    cost = entry_price * size  # this is what we paid

    # Determine fill price used for PnL
    actual_price = fill_price if fill_price and fill_price > 0 else entry_price

    winner = resolution.get("winner", "")
    if not winner:
        return None

    # Did our side win?
    if side == "YES":
        won = (winner == "Up")
    else:  # NO
        won = (winner == "Down")

    if won:
        pnl = (1.0 - actual_price) * size
    else:
        pnl = -actual_price * size

    return {
        "slug": slug,
        "side": side,
        "entry_price": actual_price,
        "size": size,
        "cost": cost,
        "winner": winner,
        "won": won,
        "pnl": round(pnl, 2),
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="Reconcile paper trades with settlement results")
    parser.add_argument("--all", action="store_true", help="Re-check all trades")
    parser.add_argument("--slug", type=str, help="Check a specific slug only")
    args = parser.parse_args()

    trades = load_trades()
    reconciled = set() if args.all else load_reconciled()

    buy_trades = [t for t in trades if t.get("action") == "BUY"]

    total_pnl = sum(t.get("pnl", 0) for t in trades if t.get("action") == "SELL")
    wins = 0
    losses = 0
    checked = 0

    print(f"{'Slug':25s} {'Side':8s} {'Entry':8s} {'Size':6s} {'Winner':8s} {'Result':8s} {'PnL':>8s}")
    print("-" * 75)

    for t in buy_trades:
        slug = t.get("slug", "")
        side = t.get("side", "")
        entry_price = t.get("entry_price", t.get("fill_price", 0))
        size = t.get("size", 8)

        # Skip if already reconciled (unless --all)
        key = slug + side + str(entry_price)
        if key in reconciled and not args.all:
            continue

        # Skip if already exited via stop
        matched_exit = any(
            x.get("slug") == slug
            and x.get("entry_price") == entry_price
            and x.get("action") == "SELL"
            for x in trades
        )
        if matched_exit:
            continue

        if args.slug and slug != args.slug:
            continue

        resolution = get_resolution(slug)
        if resolution["status"] != "resolved":
            if resolution["status"] == "not_closed":
                print(f"{slug[-20:]:25s} {side:8s} {entry_price:>7.3f} {'$'+str(size):6s} {'--':8s} {'OPEN':8s} {'--':>8s}")
            continue

        pnl_data = calculate_pnl(t, resolution)
        if not pnl_data:
            continue

        checked += 1
        pnl = pnl_data["pnl"]
        total_pnl += pnl
        result = "WIN ✅" if pnl > 0 else "LOSS ❌"
        if pnl > 0:
            wins += 1
        else:
            losses += 1

        print(f"{slug[-20:]:25s} {side:8s} {entry_price:>7.3f} {'$'+str(size):6s} {resolution['winner']:8s} {result:8s} ${pnl:>+6.2f}")

        save_reconciled(pnl_data)

    # Print stop-losses that were already recorded
    for t in trades:
        if t.get("action") == "SELL":
            pnl = t.get("pnl", 0)
            reason = t.get("exit_reason", "")
            slug = t.get("slug", "")
            side = t.get("side", "")
            entry_price = t.get("entry_price", 0)
            size = t.get("size", 8)
            result = "WIN ✅" if pnl > 0 else "LOSS ❌"
            print(f"{slug[-20:]:25s} {side:8s} {entry_price:>7.3f} {'$'+str(size):6s} {'--':8s} {result:8s} ${pnl:>+6.2f}  [{reason}]")

    print("-" * 75)
    total = wins + losses
    wr = f"{wins/total*100:.1f}%" if total > 0 else "--"
    print(f"Checked: {checked} new | Wins: {wins} | Losses: {losses} | Win Rate: {wr} | Total PnL: ${total_pnl:+.2f}")


if __name__ == "__main__":
    main()
