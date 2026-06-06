#!/usr/bin/env python3
"""Cross-check: my comparison vs Codex comparison."""
import json, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone

P = Path(".")
CACHE = P / "scripts" / "settlement_cache.json"
with open(CACHE) as f: S = json.load(f)

print(f"Settlements: {len(S)}")

W=300; D=20; SZ=10

def sim(by,bn,yl,yh,sy,sn,me,ws):
    pnl=0; w=0; l=0
    for slug,wst,bt,ys,st in ws:
        if len(bt)<5 or len(ys)<3 or not st: continue
        op=bt[0][1]; en=[]; si=0; ym=0.5
        for ts,bp in bt:
            td=datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()
            el=td-wst
            if el<0 or el>=W: continue
            bc=bp-op
            while si<len(ys) and ys[si][0]<=ts: ym=ys[si][1]; si+=1
            if ym<=0.001 or ym>=0.999: continue
            if el>=D:
                ok=True
                if en and len(en)>=me: ok=False
                if en and el<en[-1]["t"]+20: ok=False
                if ok and bc>bn and el<=180 and ym<=yl:
                    en.append({"s":"NO","ep":ym,"p":1-ym,"z":SZ,"t":el,"x":False,"bc":bc})
                elif ok and bc<-by and el<=270 and ym>=yh:
                    en.append({"s":"YES","ep":ym,"p":ym,"z":SZ,"t":el,"x":False,"bc":bc})
            if el<285:
                for e in en:
                    if e["x"]: continue
                    d=e["s"]  # Use actual trade side
                    if d=="YES" and ym>=0.85: e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                    elif d=="NO" and ym<=0.12: e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
                    if not e.get("x"):
                        s=sy if d=="YES" else sn
                        if s and e.get("bc") is not None:
                            th=max(abs(e["bc"])*s,10)
                            if d=="YES" and e["bc"]<0 and bc<-th: e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                            elif d=="NO" and e["bc"]>0 and bc>th: e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
        for e in en:
            if not e.get("x"):
                won=(st=="Up" and e["s"]=="YES") or (st=="Down" and e["s"]=="NO")
                e["pn"]=round((1.0-e["p"])*e["z"],2) if won else round(-e["p"]*e["z"],2)
            pnl+=e.get("pn",0)
            if e.get("pn",0)>0: w+=1
            elif e.get("pn",0)<0: l+=1
    tr=w+l
    return {"pnl":round(pnl,2),"wr":round(w/tr*100,1) if tr>0 else 0,"trades":tr}

db=sqlite3.connect(str(P/"data"/"btc5m.db"))
cur=db.cursor()
ws=1777766100; aw=[]
while ws+300<=int(time.time()):
    slug=f"btc-updown-5m-{ws}"; st=S.get(slug)
    ss=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(ws))
    ee=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime(ws+W))
    cur.execute("SELECT timestamp,price FROM binance_btc_ticks WHERE timestamp>=? AND timestamp<=? ORDER BY rowid",(ss,ee))
    bt=[(r[0],float(r[1])) for r in cur.fetchall()]
    cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?",(slug,))
    row=cur.fetchone()
    ys=[]
    if row:
        cur.execute("SELECT timestamp,midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp>=? AND timestamp<=? ORDER BY rowid",(row[0],ss,ee))
        ys=[(r[0],float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
    if len(bt)>=5 and len(ys)>=3 and st: aw.append((slug,ws,bt,ys,st))
    ws+=300
db.close()

print(f"Windows: {len(aw)}")
print()
for name,by,bn,yl,yh,sy,sn,me in [
    ("NEW MAX=2 (当前)",3,10,0.30,0.45,3,3,2),
    ("OLD MAX=1",3,5,0.25,0.45,4,3,1),
]:
    r=sim(by,bn,yl,yh,sy,sn,me,aw)
    print(f"{name:<25} ${r['pnl']:>+6.2f} | {r['wr']:>5.1f}% | {r['trades']:>4d}笔")
