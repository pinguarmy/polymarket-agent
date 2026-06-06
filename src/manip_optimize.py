#!/usr/bin/env python3
"""Optimization: find best strategy parameters from manipulation data."""
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict
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

markets_data = []
for m in db.execute("SELECT market_id, slug FROM markets WHERE slug LIKE 'btc-updown-5m-%'"):
    slug = m["slug"]
    try: open_ts = int(slug.split("-")[-1])
    except: continue
    btc_open = btc_prices.get(open_ts) or btc_at(open_ts)
    btc_close = btc_prices.get(open_ts+300) or btc_at(open_ts+300)
    if btc_open is None or btc_close is None: continue
    btc_up = btc_close > btc_open
    btc_change_d = btc_close - btc_open
    
    snaps = []
    for s in db.execute("SELECT midpoint, timestamp FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND midpoint IS NOT NULL ORDER BY id", (m["market_id"],)):
        try:
            s_dt = datetime.fromisoformat(s["timestamp"].replace("Z","+00:00"))
            s_epoch = s_dt.timestamp()
        except: continue
        if open_ts <= s_epoch <= open_ts + 300:
            btc_now = btc_at(s_epoch) or btc_open
            snaps.append({"price": float(s["midpoint"]), "elapsed": s_epoch - open_ts,
                         "time_left": open_ts+300-s_epoch, "btc_move_d": btc_now - btc_open})
    if len(snaps) < 10: continue
    
    f240 = [s for s in snaps if s["elapsed"] < 240]
    l60 = [s for s in snaps if s["elapsed"] >= 240]
    if not f240 or not l60: continue
    avg_f = sum(s["price"] for s in f240)/len(f240)
    avg_l = sum(s["price"] for s in l60)/len(l60)
    flip = avg_l - avg_f
    wrong = sum(1 for s in snaps if (btc_up and s["price"]<0.50) or (not btc_up and s["price"]>0.50))/len(snaps)*100
    
    markets_data.append({"slug":slug, "btc_up":btc_up, "btc_change_d":btc_change_d,
        "wrong":wrong, "flip":flip, "avg_f":avg_f, "avg_l":avg_l,
        "manip": wrong>60 and abs(flip)>0.15, "snaps":snaps})

db.close()

manip = [m for m in markets_data if m["manip"]]
normal = [m for m in markets_data if not m["manip"]]
print(f"Markets: {len(markets_data)} | Manipulated: {len(manip)} | Normal: {len(normal)}")

# OPT 1: Entry window
print(f"\n--- OPT 1: Entry Window ---")
for (t0, t1), label in [((0,60),"0-60s"),((60,120),"1-2min"),((120,180),"2-3min"),((180,240),"3-4min")]:
    w=t=0; entries=[]
    for m in manip:
        for s in m["snaps"]:
            if t0<=s["elapsed"]<t1:
                up=s["btc_move_d"]>0
                wrong=(up and s["price"]<0.50) or (not up and s["price"]>0.50)
                if wrong:
                    t+=1
                    if up==m["btc_up"]: w+=1
                    entries.append(s["price"])
    if t: print(f"  {label}: {w}/{t} ({w/t*100:.0f}%) | avg entry={sum(entries)/len(entries):.3f}")

# OPT 2: BTC move
print(f"\n--- OPT 2: BTC Move Threshold ---")
for b in [5,10,15,20,30,50]:
    sub=[m for m in manip if abs(m["btc_change_d"])>=b]
    if sub:
        w=sum(1 for m in sub if (m["avg_f"]>0.55 and m["btc_up"]) or (m["avg_f"]<0.45 and not m["btc_up"]))
        print(f"  BTC≥\${b}: {len(sub)} mkts, {w}/{len(sub)} ({w/len(sub)*100:.0f}%)")

# OPT 3: Mispricing
print(f"\n--- OPT 3: Mispricing Magnitude ---")
for mp in [0.05,0.10,0.15,0.20,0.25]:
    sub=[m for m in manip if (m["btc_up"] and 0.50-m["avg_f"]>=mp) or (not m["btc_up"] and m["avg_f"]-0.50>=mp)]
    if sub:
        w=sum(1 for m in sub if (m["avg_f"]>0.55 and m["btc_up"]) or (m["avg_f"]<0.45 and not m["btc_up"]))
        print(f"  Mispricing≥{mp:.2f}: {len(sub)} mkts, {w}/{len(sub)} ({w/len(sub)*100:.0f}%)")

# OPT 4: Duration streaks
print(f"\n--- OPT 4: Duration Streaks ---")
for ms in [30,60,90,120,180]:
    q=w=0
    for m in manip:
        streaks=[]; cur=0
        for s in sorted(m["snaps"],key=lambda x:x["elapsed"]):
            if s["elapsed"]>=240: break
            up=s["btc_move_d"]>0.01
            if abs(s["btc_move_d"])<3: continue
            if (up and s["price"]<0.50) or (not up and s["price"]>0.50): cur+=5
            else:
                if cur>0: streaks.append(cur); cur=0
        if cur>0: streaks.append(cur)
        if streaks and max(streaks)>=ms:
            q+=1
            if (m["avg_f"]>0.55 and m["btc_up"]) or (m["avg_f"]<0.45 and not m["btc_up"]): w+=1
    if q: print(f"  Streak≥{ms}s: {q} mkts, {w}/{q} ({w/q*100:.0f}%)")

# OPT 5: Risk/Reward
print(f"\n--- OPT 5: Risk/Reward per Market ---")
for m in manip[:5]:
    # FOLLOW: YES high → buy YES at avg_f; YES low → buy NO at 1-avg_f
    entry=1.0-m["avg_f"] if m["avg_f"]<0.50 else m["avg_f"]
    reward=1.0-entry
    rr=reward/entry if entry>0 else 999
    d="BUY YES" if m["avg_f"]>0.50 else "BUY NO"
    print(f"  {m['slug'][-25:]}: entry={entry:.3f} | win=${reward:.3f} | RR={rr:.1f}:1 | {d}")

# Combined best thresholds
print(f"\n{'='*60}")
print(f"COMBINED BEST THRESHOLDS")
print(f"{'='*60}")
best=None
for wm in [50,55,60,65,70]:
    for fm in [0.05,0.10,0.15,0.20]:
        for bm in [5,10,15,20,30]:
            for dm in [30,60,90]:
                sub=[]
                for m in manip:
                    if m["wrong"]<wm or abs(m["flip"])<fm or abs(m["btc_change_d"])<bm: continue
                    streaks=[]; cur=0
                    for s in sorted(m["snaps"],key=lambda x:x["elapsed"]):
                        if s["elapsed"]>=240: break
                        up=s["btc_move_d"]>0.01
                        if abs(s["btc_move_d"])<3: continue
                        if (up and s["price"]<0.50) or (not up and s["price"]>0.50): cur+=5
                        else:
                            if cur>0: streaks.append(cur); cur=0
                    if cur>0: streaks.append(cur)
                    if not streaks or max(streaks)<dm: continue
                    sub.append(m)
                if len(sub)<5: continue
                w=sum(1 for m in sub if (m["avg_f"]>0.55 and m["btc_up"]) or (m["avg_f"]<0.45 and not m["btc_up"]))
                wr=w/len(sub)*100
                if wr>=90:
                    print(f"  wrong≥{wm}% flip≥{fm:.2f} btc≥\${bm} streak≥{dm}s → {len(sub)} mkts {wr:.0f}%")
                    if best is None or (wr>best[0] and len(sub)>best[2]):
                        best=(wr,wm,fm,bm,dm,len(sub))

if best:
    print(f"\n  BEST: wrong≥{best[1]}% flip≥{best[2]:.2f} btc≥\${best[3]} streak≥{best[4]}s → {best[5]} mkts {best[0]:.0f}%")
