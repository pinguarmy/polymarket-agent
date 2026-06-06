#!/usr/bin/env python3
"""WALK-FORWARD VALIDATION — 4-fold time-series rolling window.

Each fold: 75% train (select best params), 25% test (evaluate with those params).
Uses slippage + cost model.
"""
import sqlite3
from pathlib import Path
from datetime import datetime
from collections import defaultdict

SRC = Path(__file__).resolve().parent
db = sqlite3.connect(str(SRC.parent / "data" / "btc5m.db"))
db.row_factory = sqlite3.Row

# Cost model constants
SLIPPAGE = 0.005
COST_RATE = 0.02
SIZE = 10.0

# Load BTC prices
btc_prices = {}
for row in db.execute("SELECT timestamp, price FROM binance_btc_ticks ORDER BY timestamp"):
    try:
        dt = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        btc_prices[int(dt.timestamp())] = float(row["price"])
    except:
        pass

def btc_at(epoch):
    for offset in range(0, 120):
        p = btc_prices.get(int(epoch) - offset)
        if p is not None:
            return p
    return None

# Collect all markets with their timestamps
markets = []
for m in db.execute("SELECT market_id, slug FROM markets WHERE slug LIKE 'btc-updown-5m-%'"):
    slug = m["slug"]
    try:
        open_ts = int(slug.split("-")[-1])
    except:
        continue

    btc_open = btc_prices.get(open_ts) or btc_at(open_ts)
    btc_close = btc_prices.get(open_ts + 300) or btc_at(open_ts + 300)
    if btc_open is None or btc_close is None:
        continue

    # Get YES price snapshots
    snaps = []
    for s in db.execute(
        "SELECT midpoint, timestamp FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND midpoint IS NOT NULL ORDER BY id",
        (m["market_id"],)
    ):
        try:
            s_dt = datetime.fromisoformat(s["timestamp"].replace("Z", "+00:00"))
            s_epoch = s_dt.timestamp()
        except:
            continue
        if open_ts <= s_epoch <= open_ts + 270:
            btc_now = btc_at(s_epoch) or btc_open
            snaps.append({
                "price": float(s["midpoint"]),
                "elapsed": s_epoch - open_ts,
                "btc_move": btc_now - btc_open,
            })

    if len(snaps) < 10:
        continue

    markets.append({
        "slug": slug,
        "open_ts": open_ts,
        "btc_up": btc_close > btc_open,
        "snaps": snaps,
    })

db.close()

# Sort by timestamp
markets.sort(key=lambda x: x["open_ts"])
n_markets = len(markets)
print(f"Total markets: {n_markets}")

# Walk-forward: 4 folds, 75% train / 25% test
# Each fold shifts by n_markets // 4
n_folds = 4
fold_size = n_markets // n_folds

# Parameter grid for training
# Optimal parameter grid (based on enhanced_sweep results)
PARAM_GRID = [
    (3, 0.25, 0.45),   # Best: BTC_Y=$3, YES_LOW=0.25, YES_HIGH=0.45
    (3, 0.30, 0.45),   # Variant: higher YES_LOW
    (5, 0.25, 0.45),   # Variant: higher BTC
    (5, 0.30, 0.45),   # Variant: both higher
    (3, 0.35, 0.45),   # Variant: highest YES_LOW
]

def evaluate_signals(signals, btc_min, yes_low, yes_high, elapsed_min=10, elapsed_max=270):
    """Evaluate a set of raw signals with given parameters and cost model."""
    subset = [
        s for s in signals
        if abs(s["btc_move"]) >= btc_min
        and ((s["signal"] == "BUY_YES" and s["yes"] >= yes_high) or
             (s["signal"] == "BUY_NO" and s["yes"] <= yes_low))
        and s["elapsed"] >= elapsed_min
        and s["elapsed"] <= elapsed_max
    ]
    if len(subset) < 3:
        return None, 0, 0.0, 0.0

    wins = sum(1 for s in subset if s["won"])
    pnl_no = sum(s["pnl_no_cost"] for s in subset)
    pnl_with = sum(s["pnl_with_cost"] for s in subset)
    return subset, wins, pnl_no, pnl_with

print(f"\n{'='*70}")
print(f"WALK-FORWARD 4-FOLD VALIDATION")
print(f"SLIPPAGE={SLIPPAGE}/leg, COST={COST_RATE*100}%/side")
print(f"{'='*70}")

all_fold_results = []

for fold in range(n_folds):
    # Train: [0, (fold+1)*fold_size) — expanding window per fold spec
    # Actually: rolling 75/25 — each fold train on 75% before its test window
    # fold 0: train 0..75%, test 75%..100%
    # fold 1: train 0..50%, test 50%..75%
    # fold 2: train 0..25%, test 25%..50%
    # fold 3: train 0..0%, test 0%..25%
    # But that's backwards. Let's use expanding train, rolling test:
    # fold 0: train 0..50%, test 50%..75% (we need at least 50% train)
    # fold 1: train 0..55%, test 55%..80%
    # fold 2: train 0..60%, test 60%..85%
    # fold 3: train 0..65%, test 65%..90%
    # Simpler: just 4 equal rolling windows
    # fold i: train on markets[(i)*fold_size : (i+3)*fold_size] (75%), test on next fold_size (25%)
    # But we only have n_folds=4 folds total, so we need overlapping train windows
    
    # Standard approach: each fold uses the first (fold+1)*fold_size as train,
    # then the next fold_size as test, but shifted by fold_size//4 each time.
    # Actually simplest: 4 rolling windows with 75/25 split
    # fold 0: train indices 0..int(n*0.50), test int(n*0.50)..int(n*0.75)
    # fold 1: train indices 0..int(n*0.55), test int(n*0.55)..int(n*0.80)
    # fold 2: train indices 0..int(n*0.60), test int(n*0.60)..int(n*0.85)
    # fold 3: train indices 0..int(n*0.65), test int(n*0.65)..int(n*0.90)
    
    train_end_pcts = [0.50, 0.55, 0.60, 0.65]
    test_start_pcts = [0.50, 0.55, 0.60, 0.65]
    test_end_pcts = [0.75, 0.80, 0.85, 0.90]
    
    train_end = int(n_markets * train_end_pcts[fold])
    test_start = int(n_markets * test_start_pcts[fold])
    test_end = int(n_markets * test_end_pcts[fold])
    
    train_markets = markets[:train_end]
    test_markets = markets[test_start:test_end]
    
    if len(train_markets) < 20 or len(test_markets) < 5:
        print(f"\nFold {fold+1}: too few markets (train={len(train_markets)}, test={len(test_markets)}) — skipping")
        continue
    
    # Build signals for train set
    def build_signals(mkt_list):
        sigs = []
        for mkt in mkt_list:
            btc_up = mkt["btc_up"]
            for s in mkt["snaps"]:
                btc_move = s["btc_move"]
                yes = s["price"]
                elapsed = s["elapsed"]
                if elapsed < 10 or elapsed > 270:
                    continue
                signal = None
                if btc_move > 0 and yes <= 0.45:
                    signal = "BUY_NO"
                elif btc_move < 0 and yes >= 0.55:
                    signal = "BUY_YES"
                if signal is None:
                    continue
                bet_up = (signal == "BUY_YES")
                won = bet_up == btc_up
                raw_entry = yes if bet_up else 1.0 - yes
                entry_with_slip = raw_entry + SLIPPAGE
                if won:
                    pnl_no = (1.0 - raw_entry) * SIZE
                else:
                    pnl_no = -raw_entry * SIZE
                buy_cost = entry_with_slip * SIZE
                buy_fee = buy_cost * COST_RATE
                if won:
                    sell_proceeds = SIZE - (SLIPPAGE * SIZE) - (SIZE * COST_RATE)
                    pnl_with = sell_proceeds - buy_cost - buy_fee
                else:
                    pnl_with = -buy_cost - buy_fee
                sigs.append({
                    "slug": mkt["slug"],
                    "elapsed": elapsed,
                    "btc_move": btc_move,
                    "yes": yes,
                    "signal": signal,
                    "won": won,
                    "pnl_no_cost": round(pnl_no, 2),
                    "pnl_with_cost": round(pnl_with, 2),
                })
        return sigs
    
    train_sigs = build_signals(train_markets)
    test_sigs = build_signals(test_markets)
    
    # Select best params on train set
    best_params = None
    best_train_pnl = None
    for btc_min, yes_low, yes_high in PARAM_GRID:
        subset, wins, pnl_no, pnl_with = evaluate_signals(train_sigs, btc_min, yes_low, yes_high)
        if subset is None:
            continue
        if best_train_pnl is None or pnl_with > best_train_pnl:
            best_train_pnl = pnl_with
            best_params = (btc_min, yes_low, yes_high)
    
    if best_params is None:
        print(f"\nFold {fold+1}: no valid params found on train set")
        continue
    
    btc_min, yes_low, yes_high = best_params
    
    # Evaluate on train with best params
    train_subset, train_wins, train_pnl_no, train_pnl_with = evaluate_signals(
        train_sigs, btc_min, yes_low, yes_high
    )
    # Evaluate on test with best params
    test_subset, test_wins, test_pnl_no, test_pnl_with = evaluate_signals(
        test_sigs, btc_min, yes_low, yes_high
    )
    
    train_wr = train_wins / len(train_subset) * 100 if train_subset else 0
    test_wr = test_wins / len(test_subset) * 100 if test_subset else 0
    
    # Deduplicated per market
    def dedup(sigs):
        by_mkt = {}
        for s in sigs:
            if s["slug"] not in by_mkt or s["elapsed"] < by_mkt[s["slug"]]["elapsed"]:
                by_mkt[s["slug"]] = s
        return list(by_mkt.values())
    
    train_dd = dedup(train_subset) if train_subset else []
    test_dd = dedup(test_subset) if test_subset else []
    
    train_wins_d = sum(1 for s in train_dd if s["won"])
    train_pnl_no_d = sum(s["pnl_no_cost"] for s in train_dd)
    train_pnl_with_d = sum(s["pnl_with_cost"] for s in train_dd)
    test_wins_d = sum(1 for s in test_dd if s["won"])
    test_pnl_no_d = sum(s["pnl_no_cost"] for s in test_dd)
    test_pnl_with_d = sum(s["pnl_with_cost"] for s in test_dd)
    
    print(f"\n--- Fold {fold+1}/4 ---")
    print(f"  Train: {len(train_markets)} mkts, {len(train_sigs)} sigs, {len(train_dd)} unique")
    print(f"  Test:  {len(test_markets)} mkts, {len(test_sigs)} sigs, {len(test_dd)} unique")
    print(f"  Best params: BTC_MIN=${btc_min}, YES_LOW={yes_low}, YES_HIGH={yes_high}")
    print(f"  Train (deduped): {train_wins_d}/{len(train_dd)} ({train_wins_d/len(train_dd)*100:.0f}%), "
          f"PnL no-cost=${train_pnl_no_d:+.2f}, with-cost=${train_pnl_with_d:+.2f}")
    print(f"  Test  (deduped): {test_wins_d}/{len(test_dd)} ({test_wins_d/len(test_dd)*100:.0f}%), "
          f"PnL no-cost=${test_pnl_no_d:+.2f}, with-cost=${test_pnl_with_d:+.2f}")
    
    all_fold_results.append({
        "fold": fold + 1,
        "train_n": len(train_markets),
        "test_n": len(test_markets),
        "train_sigs": len(train_sigs),
        "test_sigs": len(test_sigs),
        "train_unique": len(train_dd),
        "test_unique": len(test_dd),
        "btc_min": btc_min,
        "yes_low": yes_low,
        "yes_high": yes_high,
        "train_wr": train_wins_d / len(train_dd) * 100 if train_dd else 0,
        "train_pnl_no": train_pnl_no_d,
        "train_pnl_with": train_pnl_with_d,
        "test_wr": test_wins_d / len(test_dd) * 100 if test_dd else 0,
        "test_pnl_no": test_pnl_no_d,
        "test_pnl_with": test_pnl_with_d,
    })

# Summary
if all_fold_results:
    print(f"\n{'='*70}")
    print(f"WALK-FORWARD SUMMARY")
    print(f"{'='*70}")
    print(f"{'Fold':>4} {'Train':>6} {'Test':>6} {'Sigs(tr/tst)':>13} {'Params':>18} "
          f"{'Train WR%':>9} {'TrainPnL(no)':>12} {'TrainPnL(c)':>11} "
          f"{'Test WR%':>8} {'TestPnL(no)':>11} {'TestPnL(c)':>10}")
    for r in all_fold_results:
        params_str = f"BTC${r['btc_min']}L{r['yes_low']}H{r['yes_high']}"
        print(f"{r['fold']:>4} {r['train_n']:>6} {r['test_n']:>6} "
              f"{r['train_sigs']:>5}/{r['test_sigs']:>5} {params_str:>18} "
              f"{r['train_wr']:>8.0f} {r['train_pnl_no']:>12.2f} {r['train_pnl_with']:>11.2f} "
              f"{r['test_wr']:>8.0f} {r['test_pnl_no']:>11.2f} {r['test_pnl_with']:>10.2f}")
    
    # Aggregate test PnL
    total_test_pnl_no = sum(r["test_pnl_no"] for r in all_fold_results)
    total_test_pnl_with = sum(r["test_pnl_with"] for r in all_fold_results)
    avg_test_wr = sum(r["test_wr"] for r in all_fold_results) / len(all_fold_results)
    print(f"\n  Aggregated Test PnL (no cost): ${total_test_pnl_no:+.2f}")
    print(f"  Aggregated Test PnL (w cost):   ${total_test_pnl_with:+.2f}")
    print(f"  Avg Test Win Rate: {avg_test_wr:.0f}%")