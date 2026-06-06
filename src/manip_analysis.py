#!/usr/bin/env python3
"""Manipulation analysis using SNAPSHOT data (not trades)."""
import sqlite3
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

SRC = Path(__file__).resolve().parent
db = sqlite3.connect(str(SRC.parent / "data" / "btc5m.db"))
db.row_factory = sqlite3.Row

btc_prices = {}
for row in db.execute("SELECT timestamp, price FROM binance_btc_ticks ORDER BY timestamp"):
    try:
        dt = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        btc_prices[int(dt.timestamp())] = float(row["price"])
    except: pass

def btc_at(epoch):
    for offset in range(0, 120):
        p = btc_prices.get(int(epoch) - offset)
        if p is not None: return p
    return None

all_markets = []
total_snaps = 0

for m in db.execute("SELECT market_id, slug FROM markets WHERE slug LIKE 'btc-updown-5m-%'"):
    slug = m["slug"]
    try: open_ts = int(slug.split("-")[-1])
    except: continue
    
    btc_open = btc_prices.get(open_ts) or btc_at(open_ts)
    btc_close = btc_prices.get(open_ts+300) or btc_at(open_ts+300)
    if btc_open is None or btc_close is None: continue
    btc_up = btc_close > btc_open
    btc_change = (btc_close - btc_open) / btc_open * 100
    
    # Get YES snapshots in window
    snaps = []
    for s in db.execute("SELECT midpoint, timestamp FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND midpoint IS NOT NULL ORDER BY id", (m["market_id"],)):
        try:
            s_dt = datetime.fromisoformat(s["timestamp"].replace("Z","+00:00"))
            s_epoch = s_dt.timestamp()
        except: continue
        if open_ts <= s_epoch <= open_ts + 300:
            snaps.append({"price": float(s["midpoint"]), "elapsed": s_epoch - open_ts})
    
    if len(snaps) < 10: continue
    total_snaps += len(snaps)
    
    first_240 = [s for s in snaps if s["elapsed"] < 240]
    last_60 = [s for s in snaps if s["elapsed"] >= 240]
    if not first_240 or not last_60: continue
    
    avg_first = sum(s["price"] for s in first_240) / len(first_240)
    avg_last = sum(s["price"] for s in last_60) / len(last_60)
    late_flip = avg_last - avg_first
    
    wrong_count = sum(1 for s in snaps if (btc_up and s["price"] < 0.50) or (not btc_up and s["price"] > 0.50))
    wrong_pct = wrong_count / len(snaps) * 100
    
    all_markets.append({
        "slug": slug, "btc_up": btc_up, "btc_change": btc_change,
        "wrong_pct": wrong_pct, "late_flip": late_flip,
        "avg_first": avg_first, "avg_last": avg_last, "snaps": len(snaps),
    })

db.close()

print(f"Markets with ≥10 snapshots: {len(all_markets)} ({total_snaps} total snapshots)")

manipulated = [m for m in all_markets if m["wrong_pct"] > 60 and abs(m["late_flip"]) > 0.15]
normal = [m for m in all_markets if m["wrong_pct"] < 40]
print(f"Manipulated: {len(manipulated)} | Normal: {len(normal)}")

# Threshold sweep
print(f"\n{'='*60}")
print(f"THRESHOLD SWEEP — Follow the manipulation")
print(f"{'='*60}")

best = None
for wrong_min in [50, 55, 60, 65, 70, 75, 80]:
    for flip_min in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
        subset = [m for m in all_markets if m["wrong_pct"] >= wrong_min and abs(m["late_flip"]) >= flip_min]
        if len(subset) < 5: continue
        
        wins = 0
        for m in subset:
            # FOLLOW the manipulator: YES high + BTC ended UP = reversal happened → win
            if m["avg_first"] > 0.55 and m["btc_up"]: wins += 1
            elif m["avg_first"] < 0.45 and not m["btc_up"]: wins += 1
        
        wr = wins / len(subset) * 100
        if wr >= 60:
            print(f"  wrong≥{wrong_min}% flip≥{flip_min:.2f} → {len(subset)} mkts {wr:.0f}%")
            if best is None or (wr > best[0] and len(subset) > best[2]):
                best = (wr, wrong_min, flip_min, len(subset))

if best:
    print(f"\nBEST: wrong≥{best[1]}%, flip≥{best[2]:.2f} → {best[3]} mkts, {best[0]:.0f}% WR")
    subset = [m for m in all_markets if m["wrong_pct"] >= best[1] and abs(m["late_flip"]) >= best[2]]
    pnl = 0
    for m in subset:
        # FOLLOW: YES high (>0.55) → buy YES at avg_first; YES low (<0.45) → buy NO at 1-avg_first
        entry = 1.0 - m["avg_first"] if m["avg_first"] < 0.50 else m["avg_first"]
        if (m["avg_first"] > 0.55 and m["btc_up"]) or (m["avg_first"] < 0.45 and not m["btc_up"]):
            pnl += (1.0 - entry) * 10   # Reversal happened → win
        else:
            pnl += -entry * 10           # No reversal → lose
    print(f"Est PnL: \${pnl:+.0f} at \$10/trade")

# Top manipulated
manipulated.sort(key=lambda m: m["wrong_pct"] * abs(m["late_flip"]), reverse=True)
print(f"\nTOP 10 MANIPULATED:")
for m in manipulated[:10]:
    d = "↑" if m["btc_up"] else "↓"
    print(f"  BTC{d} wrong={m['wrong_pct']:.0f}% 1st4m={m['avg_first']:.3f} last1m={m['avg_last']:.3f} flip={m['late_flip']:+.3f} | {m['slug'][-25:]}")
