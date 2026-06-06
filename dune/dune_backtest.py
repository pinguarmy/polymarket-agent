#!/usr/bin/env python3
"""Dune Historical Backtest — runs the FOLLOW strategy against all BTC 5-min markets.

Uses binance_btc_ticks for BTC direction and polymarket_price_snapshots for entry prices.
Falls back to polymarket_trades when snapshots aren't available.

Usage:
  python3 dune/dune_backtest.py --db data/btc5m.db --output logs/dune_pnl.json
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def follow_size(btc_change_abs: float) -> float:
    c = abs(btc_change_abs)
    if c < 8:
        return 8.0
    elif c < 25:
        return 12.0
    elif c < 55:
        return 15.0
    else:
        return 10.0


def run_dune_backtest(db_path: str) -> dict:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    
    markets = db.execute("""
        SELECT m.market_id, m.slug, m.condition_id, m.open_time,
               m.yes_token_id, m.no_token_id
        FROM markets m
        WHERE m.slug LIKE 'btc-updown-5m-%'
        ORDER BY m.open_time
    """).fetchall()
    
    print(f"Found {len(markets)} BTC 5-min markets")
    
    trades = []
    skipped = {"no_btc": 0, "no_price": 0, "no_trades": 0, "late": 0}
    
    for m in markets:
        slug = m["slug"]
        
        try:
            open_ts = int(slug.split("-")[-1])
        except (ValueError, IndexError):
            continue
        
        close_ts = open_ts + 300
        open_iso = datetime.fromtimestamp(open_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        close_iso = datetime.fromtimestamp(close_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # BTC direction
        btc_open = db.execute(
            "SELECT price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
            (open_iso, close_iso)
        ).fetchone()
        
        btc_close = db.execute(
            "SELECT price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (open_iso, close_iso)
        ).fetchone()
        
        if not btc_open or not btc_close:
            skipped["no_btc"] += 1
            continue
        
        btc_open_price = float(btc_open["price"])
        btc_close_price = float(btc_close["price"])
        btc_change = btc_close_price - btc_open_price
        btc_up = btc_change > 0
        
        # Which side to trade
        if btc_up:
            side = "YES"
            token_id = m["yes_token_id"]
        else:
            side = "NO"
            token_id = m["no_token_id"]
        
        # Entry price: prefer price snapshots, fall back to trades
        entry_price = None
        entry_elapsed = 0
        snap_used = False
        
        # Try price snapshots first (midpoint)
        snap = db.execute("""
            SELECT p.midpoint, p.best_bid, p.best_ask
            FROM polymarket_price_snapshots p
            WHERE p.market_id = ? AND p.token_id = ? AND p.side = ?
            ORDER BY p.id ASC
            LIMIT 1
        """, (m["market_id"], token_id, side)).fetchone()
        
        if snap and snap["midpoint"]:
            entry_price = float(snap["midpoint"])
            if side == "NO":
                entry_price = 1.0 - entry_price
            snap_used = True
        
        # Fallback: use earliest trade price in window
        if entry_price is None:
            # Trade timestamps may be Unix ints or ISO strings — handle both
            trade_row = db.execute("""
                SELECT t.price, t.trade_timestamp
                FROM polymarket_trades t
                WHERE t.market_id = ?
                ORDER BY t.id ASC
                LIMIT 1
            """, (m["market_id"],)).fetchone()
            
            if trade_row:
                trade_ts = trade_row["trade_timestamp"]
                # Check if trade is within the market window
                in_window = False
                try:
                    # Try Unix timestamp
                    trade_epoch = float(trade_ts)
                    if open_ts <= trade_epoch <= close_ts:
                        in_window = True
                        entry_elapsed = trade_epoch - open_ts
                except (ValueError, TypeError):
                    # Try ISO format
                    try:
                        ts_str = str(trade_ts)
                        if "UTC" in ts_str:
                            ts_str = ts_str.replace(" UTC", "Z").replace(" ", "T")
                        trade_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        trade_epoch = trade_dt.timestamp()
                        if open_ts <= trade_epoch <= close_ts:
                            in_window = True
                            entry_elapsed = trade_epoch - open_ts
                    except (ValueError, TypeError):
                        pass
                
                if in_window:
                    entry_price = float(trade_row["price"])
                    if side == "NO":
                        entry_price = 1.0 - entry_price
                else:
                    skipped["no_price"] += 1
                    continue
            else:
                skipped["no_price"] += 1
                continue
        
        if entry_elapsed > 240:
            # Only apply late cutoff to snapshot entries, not trade fallbacks
            if entry_price is not None and snap_used:
                skipped["late"] += 1
                continue
        
        # Size and PnL
        size = follow_size(abs(btc_change))
        cost = entry_price * size
        
        if side == "YES" and btc_up:
            pnl = (1.0 - entry_price) * size
            won = True
        elif side == "NO" and not btc_up:
            pnl = (1.0 - entry_price) * size
            won = True
        else:
            pnl = -cost
            won = False
        
        trades.append({
            "slug": slug,
            "direction": f"BTC_{'UP' if btc_up else 'DOWN'}_FOLLOW",
            "category": "follow",
            "side": side,
            "entry_price": round(entry_price, 4),
            "size": size,
            "cost": round(cost, 2),
            "btc_change": round(btc_change, 2),
            "btc_up": btc_up,
            "pnl": round(pnl, 2),
            "won": won,
            "entry_elapsed": round(entry_elapsed, 0),
            "reason": f"{'BTC up' if btc_up else 'BTC down'} → buy {side}",
        })
    
    db.close()
    
    total = len(trades)
    wins = sum(1 for t in trades if t["won"])
    losses = total - wins
    total_pnl = sum(t["pnl"] for t in trades)
    
    return {
        "strategy": "dune-follow-v1",
        "total_markets": len(markets),
        "total_trades": total,
        "total_wins": wins,
        "total_losses": losses,
        "win_rate": round(wins / max(total, 1) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / max(total, 1), 2),
        "skipped": skipped,
        "trades": trades,
    }


def print_report(r: dict):
    print("=" * 60)
    print("DUNE HISTORICAL BACKTEST — FOLLOW Strategy")
    print("=" * 60)
    print(f"Markets scanned:  {r['total_markets']}")
    print(f"Trades executed:  {r['total_trades']}")
    print(f"Wins:             {r['total_wins']}")
    print(f"Losses:           {r['total_losses']}")
    print(f"Win rate:         {r['win_rate']}%")
    print(f"Total PnL:        ${r['total_pnl']:+.2f}")
    print(f"Avg PnL/trade:    ${r['avg_pnl']:+.2f}")
    print()
    sk = r.get("skipped", {})
    print(f"Skipped: no_btc={sk.get('no_btc',0)}, no_price={sk.get('no_price',0)}, late={sk.get('late',0)}")
    
    if r["trades"]:
        sizes = defaultdict(int)
        for t in r["trades"]:
            sizes[t["size"]] += 1
        print(f"\nSize distribution:")
        for sz in sorted(sizes):
            print(f"  ${sz:.0f}: {sizes[sz]} trades ({sizes[sz]/len(r['trades'])*100:.0f}%)")
        
        print(f"\nLast 8 trades:")
        for t in r["trades"][-8:]:
            mark = "✓" if t["won"] else "✗"
            print(f"  {mark} {t['slug'][-25:]} | {t['side']} | entry=${t['entry_price']:.3f} size={t['size']:.0f} pnl=${t['pnl']:+.2f} btc={t['btc_change']:+.0f}")
    
    print(f"\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/btc5m.db")
    parser.add_argument("--output", default="logs/dune_pnl.json")
    args = parser.parse_args()
    
    result = run_dune_backtest(args.db)
    
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    
    print_report(result)
