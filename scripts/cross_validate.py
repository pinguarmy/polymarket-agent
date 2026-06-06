#!/usr/bin/env python3
"""Cross-validation: train on 50% windows, test on remaining 50%. Check if optimal params survive."""
import json, sqlite3, time, random
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / 'scripts' / 'settlement_cache.json'
with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

SCALE_SIZE = 8; ENTRY_DELAY = 20; WINDOW_LEN = 300
DEFAULT_COST_MODEL = {"slippage": 0.005, "cost_per_leg": 0.02}

def trade_pnl(side, entry_yes, exit_yes, size, cost_model=DEFAULT_COST_MODEL):
    slippage = float(cost_model.get("slippage", 0.0))
    cost_per_leg = float(cost_model.get("cost_per_leg", 0.0))
    entry_price = entry_yes if side == "YES" else 1.0 - entry_yes
    exit_price = exit_yes if side == "YES" else 1.0 - exit_yes
    pnl_with_slippage = ((exit_price - slippage) - (entry_price + slippage)) * size
    pnl_after_costs = pnl_with_slippage - (2 * cost_per_leg * size)
    return round(pnl_after_costs, 2), round(pnl_with_slippage, 2)

def load_data(slug, ws, cur):
    s = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ws))
    e = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(ws + WINDOW_LEN))
    cur.execute('SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid', (s, e))
    btc = [(r[0], float(r[1])) for r in cur.fetchall()]
    cur.execute('SELECT m.market_id FROM markets m WHERE m.slug=?', (slug,))
    row = cur.fetchone()
    yes = []
    if row:
        cur.execute("SELECT timestamp, midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid", (row[0], s, e))
        yes = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]
    return btc, yes

def simulate(btc_min, yes_low, yes_high, sl_mult, use_tp, windows, cost_model=DEFAULT_COST_MODEL):
    total_pnl = 0; slippage_pnl = 0; wins = 0; losses = 0
    for slug, ws, btc_t, yes_s, st in windows:
        if len(btc_t) < 5 or len(yes_s) < 3 or not st: continue
        open_p = btc_t[0][1]; entries = []; si = 0; ym = 0.5; fbc = None
        for ts, bp in btc_t:
            if not isinstance(ts, str): continue
            from datetime import timezone as tz, datetime as dt
            td = dt.fromisoformat(ts.replace('Z','+00:00')).timestamp()
            el = td - ws
            if el < 0 or el >= WINDOW_LEN: continue
            bc = bp - open_p
            while si < len(yes_s) and yes_s[si][0] <= ts: ym = yes_s[si][1]; si += 1
            if ym <= 0.001 or ym >= 0.999: continue
            if el >= ENTRY_DELAY:
                ok = True
                if entries and len(entries) >= 2: ok = False
                if entries and el < entries[-1]['t'] + 20: ok = False
                if ok and bc > btc_min and el <= 180 and ym <= yes_low:
                    entries.append({'s':'NO','ep':ym,'p':1-ym,'z':SCALE_SIZE,'t':el,'x':False,'bc':bc}); fbc = fbc or bc
                elif ok and bc < -btc_min and el <= 270 and ym >= yes_high:
                    entries.append({'s':'YES','ep':ym,'p':ym,'z':SCALE_SIZE,'t':el,'x':False,'bc':bc}); fbc = fbc or bc
            if el < 285:
                for e in entries:
                    if e['x']: continue
                    d = 'YES' if e['ep'] >= 0.55 else 'NO'
                    if use_tp and d == 'YES' and ym >= 0.94:
                        e['pn'], e['pn_slip'] = trade_pnl(e['s'], e['ep'], ym, e['z'], cost_model); e['x'] = True
                    elif use_tp and d == 'NO' and ym <= 0.06:
                        e['pn'], e['pn_slip'] = trade_pnl(e['s'], e['ep'], ym, e['z'], cost_model); e['x'] = True
                    if not e['x'] and sl_mult and fbc is not None:
                        th = max(abs(fbc)*sl_mult, 10)
                        if d == 'YES' and fbc < 0 and bc < -th:
                            e['pn'], e['pn_slip'] = trade_pnl(e['s'], e['ep'], ym, e['z'], cost_model); e['x'] = True
                        elif d == 'NO' and fbc > 0 and bc > th:
                            e['pn'], e['pn_slip'] = trade_pnl(e['s'], e['ep'], ym, e['z'], cost_model); e['x'] = True
        for e in entries:
            if not e['x']:
                won = (st == 'Up' and e['s'] == 'YES') or (st == 'Down' and e['s'] == 'NO')
                exit_yes = 1.0 if won and e['s'] == 'YES' else 0.0 if won else 0.0 if e['s'] == 'YES' else 1.0
                e['pn'], e['pn_slip'] = trade_pnl(e['s'], e['ep'], exit_yes, e['z'], cost_model)
            total_pnl += e.get('pn',0)
            slippage_pnl += e.get('pn_slip',0)
            if e.get('pn',0) > 0: wins += 1
            elif e.get('pn',0) < 0: losses += 1
    return {
        'pnl': round(total_pnl,2),
        'pnl_with_slippage': round(slippage_pnl,2),
        'pnl_after_costs': round(total_pnl,2),
        'wr': round(wins/(wins+losses)*100,1) if wins+losses>0 else 0,
        'trades': wins+losses,
    }

# Load all windows
db = sqlite3.connect(str(PROJECT / 'data' / 'btc5m.db'))
cur = db.cursor()
all_w = []
ws = 1777766100
while ws + 300 <= int(time.time()):
    slug = f'btc-updown-5m-{ws}'
    st = SETTLEMENTS.get(slug)
    b, y = load_data(slug, ws, cur)
    if len(b) >= 5 and len(y) >= 3 and st:
        all_w.append((slug, ws, b, y, st))
    ws += 300
db.close()

print(f'Total windows: {len(all_w)}')
print()

# 5-fold cross validation (rolling windows, chronological order)
BTCS = [3, 5, 7]; LOWS = [0.30, 0.35, 0.40]; HIGHS = [0.45, 0.50, 0.55]
SLS = [0, 3, 4]; TPS = [True, False]

# 3-fold: train on first 2/3, test on last 1/3
folds = [
    (all_w[:len(all_w)//3], all_w[len(all_w)//3:2*len(all_w)//3]),  # fold 1
    (all_w[:2*len(all_w)//3], all_w[2*len(all_w)//3:]),             # fold 2
]

print("=== 3-fold Cross Validation ===")
for fi, (train, test) in enumerate(folds):
    best = None
    for btc in BTCS:
        for yl in LOWS:
            for yh in [h for h in HIGHS if h > yl]:
                for sl in SLS:
                    for tp in TPS:
                        r = simulate(btc, yl, yh, sl or None, tp, train)
                        if best is None or r['pnl'] > best[0]:
                            best = (r['pnl'], btc, yl, yh, sl, tp, r['wr'], r['trades'])
    
    if best:
        tr = simulate(best[1], best[2], best[3], best[4] or None, best[5], test)
        # 100 random configs on test set
        rands = []
        for _ in range(100):
            btc = random.choice(BTCS)
            yl = random.choice(LOWS)
            yh = random.choice([h for h in HIGHS if h > yl])
            sl = random.choice(SLS)
            tp = random.choice(TPS)
            rands.append(simulate(btc, yl, yh, sl or None, tp, test)['pnl'])
        
        rand_avg = sum(rands)/len(rands)
        rand_best = max(rands)
        survives = tr['pnl'] > rand_avg
        
        print(f"  Fold {fi+1}: train={len(train)}, test={len(test)}")
        print(f"    Best train: BTC=${best[1]} LOW={best[2]} HIGH={best[3]} SL={best[4]} TP={'Y' if best[5] else 'N'}")
        print(f"    Train: ${best[0]} WR={best[6]}% {best[7]}trades")
        print(f"    Test:  ${tr['pnl']} WR={tr['wr']}% {tr['trades']}trades")
        print(f"    With slippage: ${tr['pnl_with_slippage']:+.2f}")
        print(f"    After costs: ${tr['pnl_after_costs']:+.2f}")
        print(f"    Rand:  avg=${rand_avg:.2f} best=${rand_best:.2f}")
        print(f"    {'✅ EDGE SURVIVES' if survives else '❌ EDGE DIES'}")

# Also run ALL historical data with best known params
print()
print("=== Best param on ALL data ===")
r = simulate(3, 0.30, 0.45, 4, True, all_w)
print(f"  BTC=$3 YES_LOW=0.30 YES_HIGH=0.45 SL=4x TP=True")
print(f"  PnL: ${r['pnl']} | WR: {r['wr']}% | {r['trades']} trades")
print(f"  With slippage: ${r['pnl_with_slippage']:+.2f}")
print(f"  After costs: ${r['pnl_after_costs']:+.2f}")
