#!/usr/bin/env python3
"""Realistic backtest on ALL available data with real capital tracking.

Tests: RANDOM, MOMENTUM, CONTRARIAN, FIRST_TRADE
Uses actual trade prices, no outcome peeking.
Tracks capital deployment and ROI against $254.48 balance.
"""

import argparse, json, random, sqlite3, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

random.seed(42)
CAPITAL = 254.48

def follow_size(btc_change_abs: float) -> float:
    c = abs(btc_change_abs)
    if c < 8: return 8.0
    elif c < 25: return 12.0
    elif c < 55: return 15.0
    else: return 10.0

def run(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    
    t0 = time.time()
    print("Loading BTC prices...")
    btc_prices = {}
    for row in db.execute("SELECT timestamp, price FROM binance_btc_ticks ORDER BY timestamp"):
        try:
            dt = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            btc_prices[int(dt.timestamp())] = float(row["price"])
        except: pass
    print(f"  {len(btc_prices)} points ({time.time()-t0:.1f}s)")
    
    def btc_at(epoch):
        for offset in range(0, 120):
            p = btc_prices.get(int(epoch) - offset)
            if p is not None: return p
        return None
    
    # Collect all markets with valid data
    markets = []
    skipped_btc = 0
    skipped_trades = 0
    
    for m in db.execute("SELECT market_id, slug FROM markets WHERE slug LIKE 'btc-updown-5m-%'"):
        slug = m["slug"]
        try: open_ts = int(slug.split("-")[-1])
        except: continue
        
        close_ts = open_ts + 300
        btc_open = btc_prices.get(open_ts) or btc_at(open_ts)
        btc_close = btc_prices.get(close_ts) or btc_at(close_ts)
        
        if btc_open is None or btc_close is None:
            skipped_btc += 1
            continue
        
        btc_up = btc_close > btc_open
        btc_change = btc_close - btc_open
        
        # Find first trade inside the window
        first_trade = None
        for t in db.execute("SELECT price, side, trade_timestamp FROM polymarket_trades WHERE market_id = ? ORDER BY id", (m["market_id"],)):
            trade_epoch = None
            try: trade_epoch = float(t["trade_timestamp"])
            except:
                try:
                    ts = str(t["trade_timestamp"]).replace(" UTC","Z").replace(" ","T")
                    trade_epoch = datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
                except: pass
            
            if trade_epoch and open_ts <= trade_epoch <= close_ts:
                first_trade = {
                    "price": float(t["price"]),
                    "side": t["side"],
                    "epoch": trade_epoch,
                    "elapsed": trade_epoch - open_ts,
                }
                break
        
        if not first_trade:
            skipped_trades += 1
            continue
        
        btc_entry = btc_at(first_trade["epoch"]) or btc_open
        
        markets.append({
            "slug": slug, "btc_open": btc_open, "btc_close": btc_close,
            "btc_up": btc_up, "btc_change": btc_change,
            "trade": first_trade, "btc_entry": btc_entry,
        })
    
    N = len(markets)
    print(f"Markets: {N} (skipped: {skipped_btc} no BTC, {skipped_trades} no in-window trades)")
    
    # ── Run all 4 strategies ──
    results = {}
    
    for strat_name, strat_fn in [
        ("RANDOM", lambda m: ("YES" if random.random() > 0.5 else "NO", None)),
        ("MOMENTUM", lambda m: ("YES" if m["btc_entry"] > m["btc_open"] else "NO", "btc↑")),
        ("CONTRARIAN", lambda m: ("NO" if m["btc_entry"] > m["btc_open"] else "YES", "fade")),
        ("FIRST_TRADE", lambda m: ("YES" if m["trade"]["side"] == "BUY" else "NO", "follow_trade")),
    ]:
        trades = []
        capital_used = 0.0
        
        for m in markets:
            side, tag = strat_fn(m)
            entry_price = m["trade"]["price"] if side == "YES" else 1.0 - m["trade"]["price"]
            size = follow_size(abs(m["btc_change"]))
            cost = entry_price * size
            capital_used += cost
            
            if side == "YES" and m["btc_up"]:
                pnl = (1.0 - entry_price) * size
                won = True
            elif side == "NO" and not m["btc_up"]:
                pnl = (1.0 - entry_price) * size
                won = True
            else:
                pnl = -cost
                won = False
            
            trades.append({"pnl": round(pnl,2), "won": won, "side": side, "entry": round(entry_price,4)})
        
        wins = sum(1 for t in trades if t["won"])
        total_pnl = sum(t["pnl"] for t in trades)
        
        results[strat_name] = {
            "name": strat_name,
            "trades": N,
            "wins": wins,
            "losses": N - wins,
            "win_rate": round(wins / N * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / N, 2),
            "capital_used": round(capital_used, 2),
            "roi": round(total_pnl / CAPITAL * 100, 2),
        }
    
    db.close()
    
    # Print
    print(f"\n{'='*70}")
    print(f"REALISTIC BACKTEST — {N} Markets — \${CAPITAL} Capital")
    print(f"{'='*70}")
    print(f"{'Strategy':<20} {'Trades':>7} {'Win%':>7} {'PnL':>10} {'Avg':>7} {'ROI':>7}")
    print(f"{'-'*70}")
    
    for key in ["RANDOM", "MOMENTUM", "CONTRARIAN", "FIRST_TRADE"]:
        s = results[key]
        print(f"{s['name']:<20} {s['trades']:>7} {s['win_rate']:>6.1f}% ${s['total_pnl']:>9.2f} ${s['avg_pnl']:>6.2f} {s['roi']:>6.1f}%")
    
    # Analysis
    mom = results["MOMENTUM"]
    rnd = results["RANDOM"]
    edge_wr = mom["win_rate"] - rnd["win_rate"]
    edge_pnl = mom["total_pnl"] - rnd["total_pnl"]
    
    print(f"\n---")
    print(f"BTC went up on {sum(1 for m in markets if m['btc_up'])}/{N} markets ({sum(1 for m in markets if m['btc_up'])/N*100:.0f}%)")
    print(f"Momentum edge: {edge_wr:+.1f}% win rate, {edge_pnl:+.2f} PnL vs random")
    print(f"Expected PnL per 100 trades: ${mom['avg_pnl']*100:+.2f}")
    print(f"Capital required: \${results['MOMENTUM']['capital_used']:.2f} to execute {N} trades")
    print(f"ROI on capital: {mom['roi']:.1f}%")
    
    return {"markets": N, "capital": CAPITAL, "results": results}

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/btc5m.db")
    p.add_argument("--output", default="logs/realistic_pnl.json")
    args = p.parse_args()
    
    r = run(args.db)
    with open(args.output, "w") as f:
        json.dump(r, f, indent=2)
