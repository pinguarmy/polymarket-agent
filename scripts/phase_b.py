#!/usr/bin/env python3
"""Phase B: Honest walk-forward + cost model validation.

Uses the exact same simulation logic as enhanced_sweep.py (SL, TP, entry logic).
Adds slippage + cost model. 4-fold time-series cross-validation.
"""
import json, sqlite3, time, random, sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"
with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

WINDOW_LEN = 300; ENTRY_DELAY = 20

def load_all_windows():
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
    return all_w

def simulate(btc_yes, btc_no, yl, yh, sl_yes, sl_no, max_e, wins, slip=0.0, cost=0.0):
    pnl = 0.0; pnl_raw = 0.0; w = 0; l = 0
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
                if ent and len(ent) >= max_e: ok = False
                if ent and el < ent[-1]["t"] + 20: ok = False
                if ok and bc > btc_no and el <= 180 and ym <= yl:
                    ent.append({"s":"NO","ep":ym,"p":1-ym,"z":10,"t":el,"x":False,"bc":bc})
                elif ok and bc < -btc_yes and el <= 270 and ym >= yh:
                    ent.append({"s":"YES","ep":ym,"p":ym,"z":10,"t":el,"x":False,"bc":bc})
            if el < 285:
                for e in ent:
                    if e["x"]: continue
                    d = e["s"]  # Use actual trade side, not threshold guess
                    th_y = 0.85; th_n = 0.88
                    if d == "YES" and ym >= th_y: e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                    elif d == "NO" and ym <= 1.0-th_n: e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
                    if not e.get("x"):
                        s = sl_yes if d=="YES" else sl_no
                        if s and e.get("bc") is not None:
                            th = max(abs(e["bc"])*s, 10)
                            if d == "YES" and e["bc"] < 0 and bc < -th: e["pn"]=round((ym-e["ep"])*e["z"],2); e["x"]=True
                            elif d == "NO" and e["bc"] > 0 and bc > th: e["pn"]=round((e["ep"]-ym)*e["z"],2); e["x"]=True
        for e in ent:
            raw_pnl = 0.0
            if not e.get("x"):
                won=(st=="Up" and e["s"]=="YES") or (st=="Down" and e["s"]=="NO")
                raw_pnl = round((1.0-e["p"])*e["z"],2) if won else round(-e["p"]*e["z"],2)
                e["pn"] = raw_pnl
            else:
                raw_pnl = e["pn"]
            # Apply costs to the ENTRY only (slippage + cost on buy)
            entry_cost = e["p"] * e["z"]
            entry_with_slip_cost = entry_cost + slip*e["z"] + cost*e["z"]
            # Exit costs only if it was NOT a settlement loss (no exit cost for lost positions)
            if e.get("x") or (st=="Up" and e["s"]=="YES") or (st=="Down" and e["s"]=="NO"):
                exit_adj = slip*e["z"] + cost*e["z"]
            else:
                exit_adj = 0.0  # No exit cost for losing settlement
            pnl_raw += raw_pnl
            cost_total = entry_cost - (e["p"]*e["z"] - entry_with_slip_cost) + exit_adj
            pnl += raw_pnl - slip*e["z"] - cost*e["z"] - exit_adj
            
            if raw_pnl > 0: w += 1
            elif raw_pnl < 0: l += 1
    return w, l, round(pnl_raw,2), round(pnl,2)

print("=" * 70)
print("PHASE B — Walk-Forward + Cost Model Validation")
print("=" * 70)
print()

all_w = load_all_windows()
print(f"Total windows: {len(all_w)}")
print()

# ── Step 1: Cost model impact on optimal params ──
print("-" * 70)
print("B1/B2: Cost Model Impact")
print("-" * 70)

# Optimal params from enhanced_sweep
BY=3; BN=5; YL=0.25; YH=0.45; SY=4; SN=3; ME=1

for slip, cost, label in [
    (0, 0, "No costs"),
    (0.005, 0.02, "Slippage 0.005 + Cost 0.02 (moderate)"),
    (0.01, 0.03, "Slippage 0.01 + Cost 0.03 (conservative)")
]:
    wi, lo, raw_p, adj_p = simulate(BY, BN, YL, YH, SY, SN, ME, all_w, slip, cost)
    tr = wi + lo
    print(f"  {label}:")
    print(f"    Raw PnL: ${raw_p:+.2f} | Adj PnL: ${adj_p:+.2f} | WR: {wi}/{tr} ({wi/tr*100:.1f}%)")
print()

# ── Step 2: Walk-forward 4-fold ──
print("-" * 70)
print("B3: Walk-Forward 4-Fold Validation")
print("-" * 70)
print(f"  Params fixed at: BTC_Y={BY} BTC_N={BN} YL={YL} YH={YH} SL_Y={SY} SL_N={SN} MAX=1")
print()

n = len(all_w)
fold_w = n // 4

SLIP = 0.005; COST = 0.02

for fold in range(4):
    train = all_w[: (fold + 1) * fold_w]
    test_start = (fold + 1) * fold_w
    test_end = min((fold + 2) * fold_w, n)
    test = all_w[test_start:test_end]
    
    if len(test) < 5:
        print(f"  Fold {fold+1}: test too small ({len(test)}), skipping")
        continue
    
    # Train: find best params on THIS fold's training data
    BTCS = [3, 5, 7, 10]; LOWS = [0.25, 0.30, 0.35]; HIGHS = [0.45, 0.50]
    SLS = [3, 4]; MES = [1]
    best = None
    for by in BTCS:
        for bn in BTCS:
            for yl in LOWS:
                for yh in [h for h in HIGHS if h > yl]:
                    for sy in SLS:
                        for sn in SLS:
                            for me in MES:
                                wi, lo, rp, ap = simulate(by, bn, yl, yh, sy, sn, me, train, SLIP, COST)
                                if best is None or ap > best[0]:
                                    best = (ap, by, bn, yl, yh, sy, sn, me, rp, wi+lo, wi/(wi+lo)*100 if wi+lo>0 else 0)
    # Evaluate on test set (just use optimal params, no retraining)
    if best:
        by, bn, yl, yh, sy, sn, me = best[1], best[2], best[3], best[4], best[5], best[6], best[7]
        t_wi, t_lo, t_rp, t_ap = simulate(by, bn, yl, yh, sy, sn, me, test, SLIP, COST)
        tr = t_wi + t_lo
        
        # Fixed params on test
        f_wi, f_lo, f_rp, f_ap = simulate(BY, BN, YL, YH, SY, SN, ME, test, SLIP, COST)
        f_tr = f_wi + f_lo
        
        print(f"  Fold {fold+1}: train={len(train)} test={len(test)}")
        print(f"    Best train params: BTC_Y=${by} BTC_N=${bn} YL={yl} YH={yh} SL_Y={sy}x SL_N={sn}x")
        print(f"    Train: raw=${best[8]:+.2f} adj=${ap:+.2f} WR={best[10]:.1f}% {best[9]}trades")
        print(f"    Test (tuned): raw=${t_rp:+.2f} adj=${t_ap:+.2f} WR={t_wi}/{tr} ({t_wi/tr*100:.1f}%)")
        print(f"    Test (fixed): raw=${f_rp:+.2f} adj=${f_ap:+.2f} WR={f_wi}/{f_tr} ({f_wi/f_tr*100:.1f}%)")
        
        # Random baseline
        random.seed(42 + fold)
        rands = []
        for _ in range(100):
            rby = random.choice(BTCS); rbn = random.choice(BTCS)
            ryl = random.choice(LOWS); ryh = random.choice([h for h in HIGHS if h > ryl])
            rsy = random.choice(SLS); rsn = random.choice(SLS)
            _, _, _, rap = simulate(rby, rbn, ryl, ryh, rsy, rsn, 1, test, SLIP, COST)
            rands.append(rap)
        rand_avg = sum(rands)/len(rands)
        print(f"    Random baseline: avg=${rand_avg:+.2f} {'✅ Edge' if t_ap > rand_avg else '❌ No edge'}")
        print()

# ── Step 3: All-data final report ──
print("-" * 70)
print("FINAL: All Data with Cost Model")
print("-" * 70)
wi, lo, rp, ap = simulate(BY, BN, YL, YH, SY, SN, ME, all_w, SLIP, COST)
tr = wi + lo
print(f"  Params: BTC_Y=${BY} BTC_N=${BN} YL={YL} YH={YH} SL_Y={SY}x SL_N={SN}x MAX={ME}")
print(f"  Raw PnL (no costs):  ${rp:+.2f}")
print(f"  Adj PnL (with costs): ${ap:+.2f}")
print(f"  Trades: {tr} | Wins: {wi} | Losses: {lo}")
print(f"  Win rate: {wi/tr*100:.1f}%")
print(f"  Avg raw trade: ${rp/tr:.2f} | Avg adj trade: ${ap/tr:.2f}")
print()
print(f"  Cost impact: ${rp - ap:+.2f} ({((rp-ap)/rp*100 if rp != 0 else 0):.0f}% of raw PnL)")
