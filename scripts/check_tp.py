#!/usr/bin/env python3
"""Check TP exits: are they real or fake?"""
import json, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"
with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

BTC_MIN = 3; BN = 5; YL = 0.25; YH = 0.45
WINDOW_LEN = 300; ENTRY_DELAY = 20

db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))
cur = db.cursor()
ws = 1777766100
tp_details = []
windows_total = 0

while ws + 300 <= int(time.time()):
    slug = f"btc-updown-5m-{ws}"
    st = SETTLEMENTS.get(slug)
    if not st: ws += 300; continue
    
    windows_total += 1
    s = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws))
    e = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws + WINDOW_LEN))
    
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid", (s, e))
    btc = [(r[0], float(r[1])) for r in cur.fetchall()]
    
    cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
    row = cur.fetchone()
    if not row: ws += 300; continue
    
    cur.execute("SELECT timestamp, midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid", (row[0], s, e))
    yes = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
    
    if len(btc) < 5 or len(yes) < 3: ws += 300; continue
    
    op = btc[0][1]; ent = []; si = 0; ym = 0.5; fbc = None
    for ts, bp in btc:
        td = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        el = td - ws
        if el < 0 or el >= WINDOW_LEN: continue
        bc = bp - op
        while si < len(yes) and yes[si][0] <= ts: ym = yes[si][1]; si += 1
        if ym <= 0.001 or ym >= 0.999: continue
        if el >= ENTRY_DELAY:
            ok = True
            if ent and len(ent) >= 2: ok = False
            if ent and el < ent[-1]["t"] + 20: ok = False
            if ok and bc > BN and el <= 180 and ym <= YL:
                ent.append({"s":"NO","ep":ym,"p":1-ym,"z":8,"t":el,"x":False}); fbc = fbc or bc
            elif ok and bc < -BTC_MIN and el <= 270 and ym >= YH:
                ent.append({"s":"YES","ep":ym,"p":ym,"z":8,"t":el,"x":False}); fbc = fbc or bc
        if el < 285:
            for e in ent:
                if e["x"]: continue
                d = e["s"]  # Use actual trade side
                if d == "YES" and ym >= 0.94:
                    tp_details.append({"slug":slug[-10:],"direction":d,"entry_el":round(e["t"],1),"tp_el":round(el,1),"entry_yes":e["ep"],"tp_yes":ym,"settlement":st})
                    e["x"] = True
                elif d == "NO" and ym <= 0.06:
                    tp_details.append({"slug":slug[-10:],"direction":d,"entry_el":round(e["t"],1),"tp_el":round(el,1),"entry_yes":e["ep"],"tp_yes":ym,"settlement":st})
                    e["x"] = True
    ws += 300
db.close()

print(f"Windows: {windows_total}")
print(f"Total TP exits: {len(tp_details)}")
print()

risky = [t for t in tp_details if t["tp_el"] >= 270]
safe = [t for t in tp_details if t["tp_el"] < 270]
print(f"Fake risk (last 30s): {len(risky)}")
print(f"Likely real (< 270s): {len(safe)}")
print()

if risky:
    for t in risky[:5]:
        print(f"  FAKE? {t['slug']} {t['direction']} entry={t['entry_yes']:.3f} -> TP@{t['tp_yes']:.3f} at {t['tp_el']}s  settled={t['settlement']}")
print()

# Settlement outcomes for TP-exited trades
won = sum(1 for t in tp_details if (t["direction"] == "YES" and t["settlement"] == "Up") or (t["direction"] == "NO" and t["settlement"] == "Down"))
lost = len(tp_details) - won
print(f"Would have WON at settlement: {won}/{len(tp_details)} ({won/len(tp_details)*100:.0f}%)")
print(f"Would have LOST at settlement: {lost}/{len(tp_details)} ({lost/len(tp_details)*100:.0f}%)")
print(f"TP SAVED from losing settlements: {lost} trades")
print()
print(f"KEY: if TP is mostly fake, TP exits would SETTLE the OPPOSITE direction")
print(f"     (price spiked then reversed). If {lost}/{len(tp_details)} settled opposite,")
print(f"     that confirms TP is catching fake spikes.")
print(f"     Ratio: {lost/max(len(tp_details),1)*100:.0f}% fake spike exits")
