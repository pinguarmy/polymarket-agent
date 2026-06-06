#!/usr/bin/env python3
"""Single run with final params."""
import json, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone

P = Path(__file__).resolve().parent.parent
CACHE = P / "scripts" / "settlement_cache.json"
with open(CACHE) as f: S = json.load(f)

W=300; D=20
BY=3; BN=5; YL=0.25; YH=0.45; SY=4; SN=3; TP=1
TY=0.85; TN=0.88; SZ=10

db = sqlite3.connect(str(P / "data" / "btc5m.db"))
cur = db.cursor()
ws = 1777766100; aw = []
while ws + 300 <= int(time.time()):
    slug = f"btc-updown-5m-{ws}"
    st = S.get(slug)
    ss = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws))
    ee = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws+W))
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid", (ss, ee))
    bt = [(r[0], float(r[1])) for r in cur.fetchall()]
    cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
    row = cur.fetchone()
    ys = []
    if row:
        cur.execute("SELECT timestamp, midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid", (row[0], ss, ee))
        ys = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
    if len(bt) >= 5 and len(ys) >= 3 and st: aw.append((slug, ws, bt, ys, st))
    ws += 300
db.close()

pnl=0; w=0; l=0; tp_n=0; sl_n=0; st_n=0; tp_p=0; sl_p=0; st_p=0
for slug, ws, bt, ys, st in aw:
    op=bt[0][1]; en=[]; si=0; ym=0.5; epn=0
    for ts,bp in bt:
        td=datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
        el=td-ws
        if el<0 or el>=W: continue
        bc=bp-op
        while si<len(ys) and ys[si][0]<=ts: ym=ys[si][1]; si+=1
        if ym<=0.001 or ym>=0.999: continue
        if el>=D and not en:
            if bc>BN and el<=180 and ym<=YL: en.append({"s":"NO","ep":ym,"p":1-ym,"z":SZ,"t":el,"x":False})
            elif bc<-BY and el<=270 and ym>=YH: en.append({"s":"YES","ep":ym,"p":ym,"z":SZ,"t":el,"x":False})
        if el<285 and en and not en[0]["x"]:
            e=en[0]; d=e["s"]  # Use actual trade side
            th=TY if d=="YES" else TN
            if d=="YES" and ym>=th: e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True; epn=1
            elif d=="NO" and ym<=1.0-th: e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True; epn=1
            if not e.get("x"):
                s=SY if d=="YES" else SN
                if e.get("ep") is not None:
                    th2=max(e["ep"]*s, 10)
                    if d=="YES" and e["ep"]>0.5 and bc<-th2: e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True; epn=2
                    elif d=="NO" and e["ep"]<0.5 and bc>th2: e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True; epn=2
    if en and not en[0].get("x"):
        won=(st=="Up" and en[0]["s"]=="YES") or (st=="Down" and en[0]["s"]=="NO")
        en[0]["pn"]=round((1.0-en[0]["p"])*en[0]["z"],2) if won else round(-en[0]["p"]*en[0]["z"],2)
        epn=3
    if en:
        p=en[0].get("pn",0); pnl+=p
        if p>0: w+=1
        elif p<0: l+=1
        if epn==1: tp_n+=1; tp_p+=p
        elif epn==2: sl_n+=1; sl_p+=p
        elif epn==3: st_n+=1; st_p+=p

tr=w+l
print(f"Windows: {len(aw)}")
print(f"Trades: {tr}")
print(f"PnL: ${pnl:+.2f}")
print(f"Win%: {w}/{tr} ({w/tr*100:.1f}%)")
print(f"Avg/trade: ${pnl/tr:.2f}")
print(f"Avg/window: ${pnl/len(aw):.2f}")
print()
print(f"Exit breakdown:")
print(f"  TP: {tp_n} trades, ${tp_p:+.2f}")
print(f"  SL: {sl_n} trades, ${sl_p:+.2f}")
print(f"  Settlement: {st_n} trades, ${st_p:+.2f}")
