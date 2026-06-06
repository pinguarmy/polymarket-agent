#!/usr/bin/env python3
"""Final parameter tune: TP + Scale Size, separated YES/NO."""
import json, sqlite3, time, random
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"
with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

WINDOW_LEN = 300; ENTRY_DELAY = 20
DEFAULT_COST_MODEL = {"slippage": 0.005, "cost_per_leg": 0.02}

# Locked params from previous sweeps
BTC_YES = 3; BTC_NO = 5
YL = 0.25; YH = 0.45
SL_YES = 4; SL_NO = 3
MAX_E = 1

def trade_pnl(side, entry_yes, exit_yes, size, cost_model=DEFAULT_COST_MODEL):
    slippage = float(cost_model.get("slippage", 0.0))
    cost_per_leg = float(cost_model.get("cost_per_leg", 0.0))
    entry_price = entry_yes if side == "YES" else 1.0 - entry_yes
    exit_price = exit_yes if side == "YES" else 1.0 - exit_yes
    pnl_with_slippage = ((exit_price - slippage) - (entry_price + slippage)) * size
    pnl_after_costs = pnl_with_slippage - (2 * cost_per_leg * size)
    return round(pnl_after_costs, 2), round(pnl_with_slippage, 2)

def sim(tp_y, tp_n, sz_y, sz_n, wins, cost_model=DEFAULT_COST_MODEL):
    pnl = 0; slippage_pnl = 0; w = 0; l = 0
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
            if el >= ENTRY_DELAY and not ent:
                if bc > BTC_NO and el <= 180 and ym <= YL:
                    ent.append({"s":"NO","ep":ym,"p":1-ym,"z":sz_n,"t":el,"x":False,"bc":bc})
                elif bc < -BTC_YES and el <= 270 and ym >= YH:
                    ent.append({"s":"YES","ep":ym,"p":ym,"z":sz_y,"t":el,"x":False,"bc":bc})
            if el < 285 and ent and not ent[0]["x"]:
                e = ent[0]; d = e["s"]  # Use actual trade side
                th = tp_y if d=="YES" else tp_n
                if d == "YES" and ym >= th:
                    e["pn"], e["pn_slip"] = trade_pnl(e["s"], e["ep"], ym, e["z"], cost_model); e["x"] = True
                elif d == "NO" and ym <= 1.0 - th:
                    e["pn"], e["pn_slip"] = trade_pnl(e["s"], e["ep"], ym, e["z"], cost_model); e["x"] = True
                if not e.get("x"):
                    s = SL_YES if d=="YES" else SL_NO
                    if e.get("bc") is not None:
                        th2 = max(abs(e["bc"])*s, 10)
                        if d == "YES" and e["bc"] < 0 and bc < -th2:
                            e["pn"], e["pn_slip"] = trade_pnl(e["s"], e["ep"], ym, e["z"], cost_model); e["x"] = True
                        elif d == "NO" and e["bc"] > 0 and bc > th2:
                            e["pn"], e["pn_slip"] = trade_pnl(e["s"], e["ep"], ym, e["z"], cost_model); e["x"] = True
        if ent and not ent[0].get("x"):
            won = (st=="Up" and ent[0]["s"]=="YES") or (st=="Down" and ent[0]["s"]=="NO")
            exit_yes = 1.0 if won and ent[0]["s"]=="YES" else 0.0 if won else 0.0 if ent[0]["s"]=="YES" else 1.0
            ent[0]["pn"], ent[0]["pn_slip"] = trade_pnl(ent[0]["s"], ent[0]["ep"], exit_yes, ent[0]["z"], cost_model)
        if ent:
            pnl += ent[0].get("pn",0)
            slippage_pnl += ent[0].get("pn_slip",0)
            if ent[0].get("pn",0) > 0: w += 1
            elif ent[0].get("pn",0) < 0: l += 1
    tr = w + l
    return {
        "pnl": round(pnl,2),
        "pnl_with_slippage": round(slippage_pnl,2),
        "pnl_after_costs": round(pnl,2),
        "wr": round(w/tr*100,1) if tr>0 else 0,
        "trades": tr,
    }

# Load data
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
    if len(btc) >= 5 and len(yes) >= 3 and st:
        all_w.append((slug, ws, btc, yes, st))
    ws += 300
db.close()
print(f"Windows: {len(all_w)}")

# Sweep
TPS = [0.85, 0.88, 0.90, 0.92, 0.94, 0.96, 0.98]
SZS = [5, 8, 10, 15, 20]
results = []
for tpy in TPS:
    for tpn in TPS:
        for szy in SZS:
            for szn in SZS:
                r = sim(tpy, tpn, szy, szn, all_w)
                results.append((r["pnl"], tpy, tpn, szy, szn, r["wr"], r["trades"]))

results.sort(key=lambda x: -x[0])
print(f"Configs: {len(results)}")
print()
print(f"{'Rank':>5} {'PnL':>8} {'Win%':>6} {'Tr':>4} TP_Y TP_N SZ_Y SZ_N")
print("-"*55)
for i in range(min(20, len(results))):
    r = results[i]
    print(f"{i+1:5d} ${r[0]:>+6.2f} {r[5]:>5.1f}% {r[6]:>4d} {r[1]:.2f} {r[2]:.2f}  ${r[3]:>2d} ${r[4]:>2d}")

best = results[0]
print()
print("=== BEST ===")
print(f"  TP YES={best[1]} | TP NO={best[2]}")
print(f"  Size YES=${best[3]} | Size NO=${best[4]}")
print(f"  PnL=${best[0]} | WR={best[5]}% | {best[6]} trades")
best_detail = sim(best[1], best[2], best[3], best[4], all_w)
print(f"  With slippage: ${best_detail['pnl_with_slippage']:+.2f}")
print(f"  After costs: ${best_detail['pnl_after_costs']:+.2f}")

# Baseline (TP=0.94, Size=$8 both)
base = sim(0.94, 0.94, 8, 8, all_w)
print(f"\nvs BASELINE (TP=0.94, Size=$8 both)")
print(f"  PnL=${base['pnl']} | WR={base['wr']}% | {base['trades']} trades")
print(f"  With slippage: ${base['pnl_with_slippage']:+.2f}")
print(f"  After costs: ${base['pnl_after_costs']:+.2f}")
print(f"  Improvement: ${best[0]-base['pnl']:+.2f}")

# Cross-validation
mid = len(all_w)//2; train = all_w[:mid]; test = all_w[mid:]
tr = sim(best[1], best[2], best[3], best[4], train)
te = sim(best[1], best[2], best[3], best[4], test)
print(f"\n=== Cross-validation ===")
print(f"  Train: ${tr['pnl']} | {tr['wr']}% | {tr['trades']}")
print(f"  Test:  ${te['pnl']} | {te['wr']}% | {te['trades']}")
print(f"  Test With slippage: ${te['pnl_with_slippage']:+.2f}")
print(f"  Test After costs: ${te['pnl_after_costs']:+.2f}")
random.seed(42); rands=[]
for _ in range(100):
    tpy=random.choice(TPS); tpn=random.choice(TPS)
    szy=random.choice(SZS); szn=random.choice(SZS)
    r2=sim(tpy,tpn,szy,szn,test)
    rands.append(r2["pnl"])
print(f"  Random: avg=${sum(rands)/len(rands):.2f} | best=${max(rands):.2f}")
print(f"  {'✅ Edge survives' if te['pnl']>sum(rands)/len(rands) else '❌ Edge dies'}")
