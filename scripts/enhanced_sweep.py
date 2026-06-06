#!/usr/bin/env python3
"""Enhanced parameter sweep with separated YES/NO params and MAX_ENTRIES."""
import json, sqlite3, time, random
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(".")
CACHE = PROJECT / "scripts" / "settlement_cache.json"
with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

WINDOW_LEN = 300; ENTRY_DELAY = 20; SCALE_SIZE = 8

def sim(btc_yes, btc_no, yl, yh, sl_yes, sl_no, max_entries, wins):
    tp_pnl = 0; tp_w = 0; tp_l = 0
    for slug, ws, btc_t, yes_s, st in wins:
        if len(btc_t) < 5 or len(yes_s) < 3 or not st: continue
        op = btc_t[0][1]; ent = []; si = 0; ym = 0.5
        for ts, bp in btc_t:
            td = datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
            el = td - ws
            if el < 0 or el >= WINDOW_LEN: continue
            bc = bp - op
            while si < len(yes_s) and yes_s[si][0] <= ts: ym = yes_s[si][1]; si += 1
            if ym <= 0.001 or ym >= 0.999: continue
            if el >= ENTRY_DELAY:
                ok = True
                if ent and len(ent) >= max_entries: ok = False
                if ent and el < ent[-1]["t"] + 20: ok = False
                if ok and bc > btc_no and el <= 180 and ym <= yl:
                    ent.append({"s":"NO","ep":ym,"p":1-ym,"z":SCALE_SIZE,"t":el,"x":False,"bc":bc})
                elif ok and bc < -btc_yes and el <= 270 and ym >= yh:
                    ent.append({"s":"YES","ep":ym,"p":ym,"z":SCALE_SIZE,"t":el,"x":False,"bc":bc})
            if el < 285:
                for e in ent:
                    if e["x"]: continue
                    d = e["s"]  # Use actual trade side, not threshold guess
                    if d == "YES" and ym >= 0.94:
                        e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                    elif d == "NO" and ym <= 0.06:
                        e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
                    if not e.get("x"):
                        s = sl_yes if d=="YES" else sl_no
                        if s and e.get("bc") is not None:
                            th = max(abs(e["bc"])*s, 10)
                            if d == "YES" and e["bc"] < 0 and bc < -th:
                                e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                            elif d == "NO" and e["bc"] > 0 and bc > th:
                                e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
        for e in ent:
            if not e.get("x"):
                won=(st=="Up" and e["s"]=="YES") or (st=="Down" and e["s"]=="NO")
                e["pn"]=round((1.0-e["p"])*e["z"],2) if won else round(-e["p"]*e["z"],2)
            tp_pnl+=e.get("pn",0)
            if e.get("pn",0)>0: tp_w+=1
            elif e.get("pn",0)<0: tp_l+=1
    tr=tp_w+tp_l
    return {"pnl":round(tp_pnl,2),"wr":round(tp_w/tr*100,1) if tr>0 else 0,"trades":tr}

db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))
cur = db.cursor()
ws = 1777766100; all_w = []
while ws + 300 <= int(time.time()):
    slug = f"btc-updown-5m-{ws}"
    st = SETTLEMENTS.get(slug)
    s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws))
    e = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws+WINDOW_LEN))
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid", (s, e))
    btc = [(r[0], float(r[1])) for r in cur.fetchall()]
    cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
    row = cur.fetchone()
    yes = []
    if row:
        cur.execute("SELECT timestamp, midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid", (row[0], s, e))
        yes = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
    if len(btc) >= 5 and len(yes) >= 3 and st: all_w.append((slug, ws, btc, yes, st))
    ws += 300
db.close()
print(f"Windows: {len(all_w)}")

BTCS=[3,5,7,10]; LOWS=[0.25,0.30,0.35,0.40]; HIGHS=[0.45,0.50,0.55]
SLS=[0,3,4,5]; MAX_ENTRIES=[1,2,3]

results=[]
for btc_y in BTCS:
    for btc_n in BTCS:
        for yl in LOWS:
            for yh in [h for h in HIGHS if h>yl]:
                for sly in SLS:
                    for sln in SLS:
                        for max_e in MAX_ENTRIES:
                            r=sim(btc_y,btc_n,yl,yh,sly or None,sln or None,max_e,all_w)
                            results.append((r["pnl"],btc_y,btc_n,yl,yh,sly,sln,max_e,r["wr"],r["trades"]))

results.sort(key=lambda x:-x[0])
print(f"Configs: {len(results)}")
print(f"\n{'Rank':>5} {'PnL':>8} {'Win%':>6} {'Tr':>4} BTC_Y BTC_N   YL   YH SL_Y SL_N MAX_E")
print("-"*70)
for i in range(min(20,len(results))):
    r=results[i]
    print(f"{i+1:5d} ${r[0]:>+6.2f} {r[8]:>5.1f}% {r[9]:>4d}  ${r[1]}   ${r[2]}  {r[3]:.2f} {r[4]:.2f}  {r[5]}x  {r[6]}x   {r[7]}")

best=results[0]
print(f"\n=== BEST ===")
print(f"  BTC YES=${best[1]} | BTC NO=${best[2]}")
print(f"  YES_LOW={best[3]} | YES_HIGH={best[4]}")
print(f"  SL YES={best[5]}x | SL NO={best[6]}x")
print(f"  MAX_ENTRIES={best[7]}")
print(f"  PnL=${best[0]} | WR={best[8]}% | {best[9]} trades")

old=sim(3,3,0.30,0.45,4,4,2,all_w)
print(f"\nvs OLD shared (BTC=$3, YES_LOW=0.30, YES_HIGH=0.45, SL=4x both, MAX_ENTRIES=2)")
print(f"  PnL=${old['pnl']} | WR={old['wr']}% | {old['trades']} trades")
print(f"  Improvement: ${best[0]-old['pnl']:+.2f}")

mid=len(all_w)//2; train=all_w[:mid]; test=all_w[mid:]
tr=sim(best[1],best[2],best[3],best[4],best[5] or None,best[6] or None,best[7],train)
te=sim(best[1],best[2],best[3],best[4],best[5] or None,best[6] or None,best[7],test)
print(f"\n=== Cross-validation ===")
print(f"  Train: ${tr['pnl']} | {tr['wr']}% | {tr['trades']}")
print(f"  Test:  ${te['pnl']} | {te['wr']}% | {te['trades']}")
random.seed(42); rands=[]
for _ in range(100):
    by=random.choice(BTCS); bn=random.choice(BTCS)
    yl=random.choice(LOWS); yh=random.choice([h for h in HIGHS if h>yl])
    sy=random.choice(SLS); sn=random.choice(SLS); me=random.choice(MAX_ENTRIES)
    r2=sim(by,bn,yl,yh,sy or None,sn or None,me,test)
    rands.append(r2["pnl"])
print(f"  Random: avg=${sum(rands)/len(rands):.2f} | best=${max(rands):.2f}")
print(f"  {'✅ Edge survives' if te['pnl']>sum(rands)/len(rands) else '❌ Edge dies'}")
