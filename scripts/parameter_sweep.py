#!/usr/bin/env python3
"""Fast parameter sweep using cached settlements."""
import json, sqlite3, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"

with open(CACHE) as f:
    SETTLEMENTS = json.load(f)

# Parameters to sweep
BTC_MOVES = [3, 5, 7, 10, 15]
YES_LOWS = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]
YES_HIGHS = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
STOP_MULTS = [3.0, 4.0, 5.0, 0.0]  # 0 = no stop
USE_TP = [True, False]

SCALE_SIZE = 8
ENTRY_DELAY = 20
WINDOW_LEN = 300
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
    end_ts = ws + WINDOW_LEN
    s = datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid", (s, e))
    btc = [(r[0], float(r[1])) for r in cur.fetchall()]
    
    cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
    row = cur.fetchone()
    yes_prices = []
    if row:
        cur.execute("SELECT timestamp, midpoint FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid", (row[0], s, e))
        yes_prices = [(r[0], float(r[1]) if r[1] else 0) for r in cur.fetchall()]
    
    return btc, yes_prices

def simulate(btc_min, yes_low, yes_high, sl_mult, use_tp, windows_data, cost_model=DEFAULT_COST_MODEL):
    total_pnl = 0
    slippage_pnl = 0
    wins = 0
    losses = 0
    trades = 0
    
    for slug, ws, btc_ticks, yes_snaps, settlement in windows_data:
        if len(btc_ticks) < 5 or len(yes_snaps) < 3 or not settlement:
            continue
        
        btc_open = btc_ticks[0][1]
        entries = []
        first_btc_change = None
        snap_idx = 0
        yes_mid = 0.5
        
        for ts_str, btc_price in btc_ticks:
            if isinstance(ts_str, str):
                td = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            else:
                continue
            elapsed = td.timestamp() - ws
            if elapsed < 0 or elapsed >= WINDOW_LEN:
                continue
            btc_change = btc_price - btc_open
            
            while snap_idx < len(yes_snaps) and yes_snaps[snap_idx][0] <= ts_str:
                yes_mid = yes_snaps[snap_idx][1]
                snap_idx += 1
            if yes_mid <= 0.001 or yes_mid >= 0.999:
                continue
            
            # Entry
            if elapsed >= ENTRY_DELAY:
                can_enter = True
                if entries and len(entries) >= 2:
                    can_enter = False
                if entries and elapsed < entries[-1]["time"] + 20:
                    can_enter = False
                
                yes_sig = btc_change > btc_min and elapsed <= 180 and yes_mid <= yes_low
                no_sig = btc_change < -btc_min and elapsed <= 270 and yes_mid >= yes_high
                
                if can_enter and yes_sig:
                    entries.append({"side": "YES", "entry_yes": yes_mid, "price": yes_mid, "size": SCALE_SIZE, "time": elapsed, "ex": False})
                    if first_btc_change is None: first_btc_change = btc_change
                elif can_enter and no_sig:
                    entries.append({"side": "NO", "entry_yes": yes_mid, "price": 1 - yes_mid, "size": SCALE_SIZE, "time": elapsed, "ex": False})
                    if first_btc_change is None: first_btc_change = btc_change
            
            # Exits (skip last 15s)
            if elapsed < 285:
                for e in entries:
                    if e["ex"]: continue
                    side = e["side"]
                    if use_tp and side == "YES" and yes_mid >= 0.94:
                        e["pnl"], e["pnl_slip"] = trade_pnl(e["side"], e["entry_yes"], yes_mid, e["size"], cost_model); e["ex"] = True
                    elif use_tp and side == "NO" and yes_mid <= 0.06:
                        e["pnl"], e["pnl_slip"] = trade_pnl(e["side"], e["entry_yes"], yes_mid, e["size"], cost_model); e["ex"] = True
                    if not e["ex"] and sl_mult > 0 and first_btc_change is not None:
                        th = max(abs(first_btc_change) * sl_mult, 10)
                        if side == "YES" and first_btc_change > 0 and btc_change > th:
                            e["pnl"], e["pnl_slip"] = trade_pnl(e["side"], e["entry_yes"], yes_mid, e["size"], cost_model); e["ex"] = True
                        elif side == "NO" and first_btc_change < 0 and btc_change < -th:
                            e["pnl"], e["pnl_slip"] = trade_pnl(e["side"], e["entry_yes"], yes_mid, e["size"], cost_model); e["ex"] = True
        
        # Settlement
        for e in entries:
            if not e["ex"]:
                won = (settlement == "Up" and e["side"] == "YES") or (settlement == "Down" and e["side"] == "NO")
                exit_yes = 1.0 if won and e["side"] == "YES" else 0.0 if won else 0.0 if e["side"] == "YES" else 1.0
                e["pnl"], e["pnl_slip"] = trade_pnl(e["side"], e["entry_yes"], exit_yes, e["size"], cost_model)
            total_pnl += e.get("pnl", 0)
            slippage_pnl += e.get("pnl_slip", 0)
            trades += 1
            if e.get("pnl", 0) > 0: wins += 1
            elif e.get("pnl", 0) < 0: losses += 1
    
    wr = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
    return {
        "pnl": round(total_pnl, 2),
        "pnl_with_slippage": round(slippage_pnl, 2),
        "pnl_after_costs": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "trades": trades,
        "wr": wr,
    }

def main():
    db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))
    cur = db.cursor()
    
    # Pre-load all windows data
    start_ts = 1777766100
    end_ts = int(time.time())
    ws = (start_ts // 300) * 300
    windows_data = []
    
    while ws + 300 <= end_ts:
        slug = f"btc-updown-5m-{ws}"
        settlement = SETTLEMENTS.get(slug)
        btc, yes = load_data(slug, ws, cur)
        if len(btc) >= 5 and len(yes) >= 3 and settlement:
            windows_data.append((slug, ws, btc, yes, settlement))
        ws += 300
    
    print(f"Loaded {len(windows_data)} windows with data")
    print()
    
    results = []
    total = len(BTC_MOVES) * len(YES_LOWS) * len(YES_HIGHS) * len(STOP_MULTS) * len(USE_TP)
    done = 0
    
    for btc in BTC_MOVES:
        for yl in YES_LOWS:
            for yh in YES_HIGHS:
                if yh <= yl: continue
                for sl in STOP_MULTS:
                    for tp in USE_TP:
                        r = simulate(btc, yl, yh, sl, tp, windows_data)
                        r["p"] = {"b": btc, "l": yl, "h": yh, "s": sl, "t": tp}
                        results.append(r)
                        done += 1
    
    # Sort by PnL
    results.sort(key=lambda x: x["pnl"], reverse=True)
    
    hdr = f"{'Rank':5s} {'PnL':>8s} {'Win%':>6s} {'Tr':>4s} {'W':>3s} {'L':>3s} {'BTC':>4s} {'YL':>5s} {'YH':>5s} {'SL':>4s} {'TP':>4s}"
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(results[:25]):
        p = r["p"]
        print(f"{i+1:5d} ${r['pnl']:>+6.2f} {r['wr']:>5.1f}% {r['trades']:>3d} {r['wins']:>3d} {r['losses']:>3d} ${p['b']:>2d} {p['l']:>5.2f} {p['h']:>5.2f} {'no' if p['s']==0 else str(p['s'])+'x':>4s} {str(p['t']):>4s}")
    
    print()
    print("=== Top config ===")
    best = results[0]
    print(f"  BTC MIN: ${best['p']['b']} | YES LOW: {best['p']['l']} | YES HIGH: {best['p']['h']}")
    print(f"  STOP: {best['p']['s']}× | TP: {best['p']['t']}")
    print(f"  PnL: ${best['pnl']} | WR: {best['wr']}% | Trades: {best['trades']}")
    print(f"  With slippage: ${best['pnl_with_slippage']:+.2f}")
    print(f"  After costs: ${best['pnl_after_costs']:+.2f}")
    
    print()
    print("=== Impact analysis ===")
    for label, key in [("BTC threshold", "b"), ("YES low", "l"), ("YES high", "h"), ("Stop mult", "s"), ("TP on/off", "t")]:
        groups = defaultdict(list)
        for r in results:
            groups[f"{r['p'][key]}"].append(r["pnl"])
        print(f"  {label}:")
        for g, pnls in sorted(groups.items()):
            print(f"    {str(g):>6s}: avg PnL=${sum(pnls)/len(pnls):+.2f} ({len(pnls)} configs)")

    db.close()

if __name__ == "__main__":
    main()
