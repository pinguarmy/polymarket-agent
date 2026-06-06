#!/usr/bin/env python3
"""Trace one window trade-by-trade in both scripts."""
import json, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone

P = Path(".")
CACHE = P / "scripts" / "settlement_cache.json"
with open(CACHE) as f: S = json.load(f)

W=300; D=20; SZ=10

# My logic
slug = "btc-updown-5m-1777766100"
st = S.get(slug)
print(f"Slug: {slug} Settlement: {st}")

db = sqlite3.connect(str(P/"data"/"btc5m.db"))
cur = db.cursor()
ss = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1777766100))
ee = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1777766100+W))
cur.execute("SELECT timestamp,price FROM binance_btc_ticks WHERE timestamp>=? AND timestamp<=? ORDER BY rowid", (ss, ee))
bt = [(r[0], float(r[1])) for r in cur.fetchall()]
cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
row = cur.fetchone()
ys = []
if row:
    cur.execute("SELECT timestamp,midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp>=? AND timestamp<=? ORDER BY rowid", (row[0], ss, ee))
    ys = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
db.close()

print(f"BTC ticks: {len(bt)}, YES snaps: {len(ys)}, BTC open: {bt[0][1]:.2f}")
print()

op=bt[0][1]; en=[]; si=0; ym=0.5
for ts,bp in bt:
    td=datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
    el=td-1777766100
    if el<0 or el>=W: continue
    bc=bp-op
    while si<len(ys) and ys[si][0]<=ts: ym=ys[si][1]; si+=1
    if ym<=0.001 or ym>=0.999: continue
    
    # Entry check
    if el>=D and not en:  # MAX_ENTRIES=1 for simplicity
        if bc>5 and el<=180 and ym<=0.25:
            en.append({"s":"NO","ep":ym,"p":1-ym,"z":SZ,"t":round(el,1),"x":False,"bc":bc})
            print(f"  ENTRY NO @ t={el:.0f}s bc={bc:+.1f} YES={ym:.3f}")
        elif bc<-3 and el<=270 and ym>=0.45:
            en.append({"s":"YES","ep":ym,"p":ym,"z":SZ,"t":round(el,1),"x":False,"bc":bc})
            print(f"  ENTRY YES @ t={el:.0f}s bc={bc:+.1f} YES={ym:.3f}")
    
    # Exit check
    if el<285 and en and not en[-1]["x"]:
        e=en[-1]
        d=en[0]["s"]  # Use actual trade side
        if d=="YES" and ym>=0.85:
            pnl = (ym-e["ep"])*e["z"]
            print(f"  TP YES @ t={el:.0f}s ym={ym:.3f} entry={e['ep']:.3f} pnl=${pnl:+.2f}")
            e["x"]=True
        elif d=="NO" and ym<=0.12:
            pnl = (e["ep"]-ym)*e["z"]
            print(f"  TP NO @ t={el:.0f}s ym={ym:.3f} entry_yes={e['ep']:.3f} pnl=${pnl:+.2f}")
            e["x"]=True
        
        if not e.get("x"):
            th=max(abs(e["bc"])*3,10)
            if d=="YES" and e["bc"]<0 and bc<-th:
                pnl = (ym-e["ep"])*e["z"]
                print(f"  SL YES @ t={el:.0f}s bc={bc:+.1f} th={th} pnl=${pnl:+.2f}")
                e["x"]=True
            elif d=="NO" and e["bc"]>0 and bc>th:
                pnl = (e["ep"]-ym)*e["z"]
                print(f"  SL NO @ t={el:.0f}s bc={bc:+.1f} th={th} pnl=${pnl:+.2f}")
                e["x"]=True

if en and not en[0]["x"]:
    won=(st=="Up" and en[0]["s"]=="YES") or (st=="Down" and en[0]["s"]=="NO")
    pnl = (1.0-en[0]["p"])*en[0]["z"] if won else -en[0]["p"]*en[0]["z"]
    print(f"  SETTLEMENT: won={won} pnl=${pnl:+.2f}")
    en[0]["pn"] = pnl

if en:
    print(f"  FINAL PnL: ${en[0].get('pn',en[0].get('pnl',0)):+.2f}")
else:
    print("  No entry")
