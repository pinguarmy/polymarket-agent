#!/usr/bin/env python3
"""
Independent backtest verification.
Replicates parameter sweep logic from scratch WITHOUT copying parameter_sweep.py.
Validates whether the optimal parameters have a real edge or suffer from data snooping.
"""
import json, sqlite3, random, math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT / "data" / "btc5m.db"
CACHE_PATH = PROJECT / "scripts" / "settlement_cache.json"

# Load settlement outcomes
with open(CACHE_PATH) as f:
    SETTLEMENTS = json.load(f)

WINDOW_LEN = 300
ENTRY_DELAY = 20
SCALE_SIZE = 8

# =============================================================================
# STEP 1: Load all windows with data
# =============================================================================
def load_all_windows():
    """Load BTC ticks and YES price snapshots for all cached windows."""
    db = sqlite3.connect(str(DB_PATH))
    cur = db.cursor()

    windows = []
    for slug, outcome in SETTLEMENTS.items():
        ws = int(slug.split('-')[-1])
        s = datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        e = datetime.fromtimestamp(ws + WINDOW_LEN, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # BTC ticks
        cur.execute(
            "SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (s, e)
        )
        btc_ticks = [(r[0], float(r[1])) for r in cur.fetchall()]

        # YES price snapshots
        cur.execute("SELECT market_id FROM markets WHERE slug=?", (slug,))
        mkt_row = cur.fetchone()
        yes_snaps = []
        if mkt_row:
            cur.execute(
                "SELECT timestamp, midpoint FROM polymarket_price_snapshots "
                "WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (mkt_row[0], s, e)
            )
            yes_snaps = [(r[0], float(r[1]) if r[1] else 0) for r in cur.fetchall()]

        if len(btc_ticks) >= 5 and len(yes_snaps) >= 3 and outcome in ("Up", "Down"):
            windows.append({
                "slug": slug,
                "ws": ws,
                "btc_ticks": btc_ticks,
                "yes_snaps": yes_snaps,
                "outcome": outcome,
            })

    db.close()
    return windows

# =============================================================================
# STEP 2: Simulate one parameter set on a list of windows
# =============================================================================
def simulate_windows(params, windows):
    """
    params: {btc_min, yes_low, yes_high, sl_mult, use_tp}
    windows: list of window dicts (already filtered)
    
    Returns: {pnl, wins, losses, trades}
    Entry rules (independent implementation):
      BUY DOWN (NO): BTC rise > btc_min, YES mid <= yes_low
      BUY UP (YES):   BTC fall > btc_min, YES mid >= yes_high
    
    Exit rules:
      TP: YES >= 0.94 (BUY_UP) or YES <= 0.06 (BUY_DOWN)
      SL: BTC reverses beyond |first_BTC_change| * sl_mult
      Settlement: any position still open at window end
    """
    btc_min  = params["btc_min"]
    yes_low  = params["yes_low"]
    yes_high = params["yes_high"]
    sl_mult  = params["sl_mult"]
    use_tp   = params["use_tp"]

    total_pnl = 0.0
    wins = 0
    losses = 0
    trades = 0

    for w in windows:
        btc_ticks  = w["btc_ticks"]
        yes_snaps  = w["yes_snaps"]
        settlement  = w["outcome"]
        ws          = w["ws"]

        if len(btc_ticks) < 5 or len(yes_snaps) < 3:
            continue

        btc_open = btc_ticks[0][1]
        entries  = []          # list of dicts
        first_btc_change = None
        snap_idx  = 0
        yes_mid   = 0.5

        # ----- Tick loop (entry + early exit) -----
        for ts_str, btc_price in btc_ticks:
            if not isinstance(ts_str, str):
                continue
            td = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            elapsed = td.timestamp() - ws
            if elapsed < 0 or elapsed >= WINDOW_LEN:
                continue

            btc_change = btc_price - btc_open

            # Advance snapshot pointer to current tick
            while snap_idx < len(yes_snaps) and yes_snaps[snap_idx][0] <= ts_str:
                yes_mid = yes_snaps[snap_idx][1]
                snap_idx += 1

            if yes_mid <= 0.001 or yes_mid >= 0.999:
                continue

            # ---- Entry (after ENTRY_DELAY, max 2 entries, 20s apart) ----
            if elapsed >= ENTRY_DELAY:
                can_enter = True
                if entries:
                    if len(entries) >= 2:
                        can_enter = False
                    elif elapsed < entries[-1]["entry_time"] + 20:
                        can_enter = False

                # BUY DOWN: BTC up > btc_min, YES low, within 180s
                down_sig = btc_change > btc_min and elapsed <= 180 and yes_mid <= yes_low
                # BUY UP: BTC down > btc_min, YES high, within 270s
                up_sig   = btc_change < -btc_min and elapsed <= 270 and yes_mid >= yes_high

                if can_enter and down_sig:
                    entries.append({
                        "side": "NO",
                        "entry_yes": yes_mid,
                        "price": 1.0 - yes_mid,
                        "size": SCALE_SIZE,
                        "entry_time": elapsed,
                    })
                    if first_btc_change is None:
                        first_btc_change = btc_change
                elif can_enter and up_sig:
                    entries.append({
                        "side": "YES",
                        "entry_yes": yes_mid,
                        "price": yes_mid,
                        "size": SCALE_SIZE,
                        "entry_time": elapsed,
                    })
                    if first_btc_change is None:
                        first_btc_change = btc_change

            # ---- Exit (skip last 15s) ----
            if elapsed < 285:
                for e in entries:
                    if e.get("ex"):
                        continue

                    direction = "BUY_YES" if e["side"] == "YES" else "BUY_NO"

                    # Take profit
                    if use_tp:
                        if direction == "BUY_YES" and yes_mid >= 0.94:
                            e["pnl"] = round((yes_mid - e["entry_yes"]) * e["size"], 2)
                            e["ex"] = True
                        elif direction == "BUY_NO" and yes_mid <= 0.06:
                            e["pnl"] = round((e["entry_yes"] - yes_mid) * e["size"], 2)
                            e["ex"] = True

                    # Stop loss
                    if not e.get("ex") and sl_mult > 0 and first_btc_change is not None:
                        threshold = max(abs(first_btc_change) * sl_mult, 10)
                        if direction == "BUY_YES" and first_btc_change < 0 and btc_change < -threshold:
                            e["pnl"] = round((yes_mid - e["entry_yes"]) * e["size"], 2)
                            e["ex"] = True
                        elif direction == "BUY_NO" and first_btc_change > 0 and btc_change > threshold:
                            e["pnl"] = round((e["entry_yes"] - yes_mid) * e["size"], 2)
                            e["ex"] = True

        # ---- Settlement for unclosed positions ----
        for e in entries:
            if not e.get("ex"):
                won = (settlement == "Up" and e["side"] == "YES") or \
                      (settlement == "Down" and e["side"] == "NO")
                if won:
                    e["pnl"] = round((1.0 - e["price"]) * e["size"], 2)
                else:
                    e["pnl"] = round(-e["price"] * e["size"], 2)

            total_pnl += e.get("pnl", 0)
            trades += 1
            if e.get("pnl", 0) > 0:
                wins += 1
            elif e.get("pnl", 0) < 0:
                losses += 1

    wr = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0.0
    return {
        "pnl": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "trades": trades,
        "wr": wr,
    }

# =============================================================================
# STEP 3: Full parameter sweep (same grid as original)
# =============================================================================
BTC_MOVES  = [3, 5, 7, 10, 15]
YES_LOWS   = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55]
YES_HIGHS  = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
STOP_MULTS = [0.0, 3.0, 4.0, 5.0]
USE_TPS    = [True, False]

def iter_params():
    for b in BTC_MOVES:
        for yl in YES_LOWS:
            for yh in YES_HIGHS:
                if yh <= yl:
                    continue
                for sl in STOP_MULTS:
                    for tp in USE_TPS:
                        yield {"btc_min": b, "yes_low": yl, "yes_high": yh,
                               "sl_mult": sl, "use_tp": tp}

# =============================================================================
# STEP 4: Main
# =============================================================================
def main():
    print("=" * 65)
    print("INDEPENDENT BACKTEST VERIFICATION")
    print("=" * 65)

    # Load data
    windows = load_all_windows()
    print(f"\nLoaded {len(windows)} windows with complete data")

    # Train/test split: first 75 = train, last 75 = test
    # (align with original: they used "first 75" / "last 75")
    train_windows = windows[:75]
    test_windows  = windows[75:]
    print(f"Train: {len(train_windows)} windows")
    print(f"Test:  {len(test_windows)} windows")

    # ---- Optimal params from original ----
    optimal = {
        "btc_min": 5, "yes_low": 0.30, "yes_high": 0.45,
        "sl_mult": 3.0, "use_tp": True,
    }

    print("\n" + "=" * 65)
    print("OPTIMAL PARAMETER RESULTS")
    print("=" * 65)
    train_res = simulate_windows(optimal, train_windows)
    test_res  = simulate_windows(optimal, test_windows)

    print(f"  Train: PnL=${train_res['pnl']:+.2f}, WR={train_res['wr']}%, "
          f"Trades={train_res['trades']}, W={train_res['wins']}, L={train_res['losses']}")
    print(f"  Test:  PnL=${test_res['pnl']:+.2f}, WR={test_res['wr']}%, "
          f"Trades={test_res['trades']}, W={test_res['wins']}, L={test_res['losses']}")

    # ---- Random parameter baseline ----
    print("\n" + "=" * 65)
    print("RANDOM PARAMETER BASELINE (100 sets, test set)")
    print("=" * 65)
    random.seed(42)
    random_results = []
    for _ in range(100):
        rp = {
            "btc_min": random.choice(BTC_MOVES),
            "yes_low": random.choice(YES_LOWS),
            "yes_high": random.choice(YES_HIGHS),
            "sl_mult": random.choice(STOP_MULTS),
            "use_tp": random.choice(USE_TPS),
        }
        # Skip invalid combos
        if rp["yes_high"] <= rp["yes_low"]:
            continue
        rr = simulate_windows(rp, test_windows)
        random_results.append(rr["pnl"])

    rand_avg  = sum(random_results) / len(random_results)
    rand_best = max(random_results)
    rand_worst = min(random_results)

    print(f"  Random avg PnL:  ${rand_avg:+.2f}")
    print(f"  Random best PnL:  ${rand_best:+.2f}")
    print(f"  Random worst PnL: ${rand_worst:+.2f}")

    # ---- Compare optimal vs random ----
    print("\n" + "=" * 65)
    print("OVERFITTING ANALYSIS")
    print("=" * 65)
    better_than_avg   = sum(1 for x in random_results if x < test_res["pnl"]) / len(random_results) * 100
    better_than_best  = 1 if test_res["pnl"] > rand_best else 0

    print(f"  Optimal PnL:              ${test_res['pnl']:+.2f}")
    print(f"  Random avg PnL:           ${rand_avg:+.2f}")
    print(f"  Random best PnL:          ${rand_best:+.2f}")
    print(f"  Pct of random params worse than optimal: {better_than_avg:.1f}%")
    print(f"  Optimal beats best random: {'YES' if better_than_best else 'NO'}")

    # ---- Top 5 params on test set (full sweep) ----
    print("\n" + "=" * 65)
    print("FULL PARAMETER SWEEP (test set, top 10)")
    print("=" * 65)
    all_results = []
    for p in iter_params():
        r = simulate_windows(p, test_windows)
        all_results.append((p, r))

    all_results.sort(key=lambda x: x[1]["pnl"], reverse=True)

    hdr = f"{'Rank':>4} {'PnL':>8} {'Win%':>6} {'Tr':>4} {'W':>3} {'L':>3}  {'BTC':>4} {'YL':>5} {'YH':>5} {'SL':>4} {'TP':>5}"
    print(hdr)
    print("-" * 65)
    for i, (p, r) in enumerate(all_results[:10]):
        star = " <-- OPTIMAL" if p == optimal else ""
        print(f"{i+1:4d} ${r['pnl']:>+6.2f} {r['wr']:>5.1f}% {r['trades']:>3d} {r['wins']:>3d} {r['losses']:>3d}  "
              f"${p['btc_min']:>2d} {p['yes_low']:>5.2f} {p['yes_high']:>5.2f}  "
              f"{'no' if p['sl_mult']==0 else str(int(p['sl_mult']))+'x':>4}  {str(p['use_tp']):>5}{star}")

    # ---- Train vs Test correlation for top 20 ----
    print("\n" + "=" * 65)
    print("TRAIN-TEST CONSISTENCY (top 20 on train)")
    print("=" * 65)
    train_results = []
    for p in iter_params():
        r = simulate_windows(p, train_windows)
        train_results.append((p, r))
    train_results.sort(key=lambda x: x[1]["pnl"], reverse=True)

    print(f"{'Rank':>4} {'TrainPnL':>9} {'TestPnL':>9} {'Diff':>8}")
    print("-" * 35)
    for i, (p, tr) in enumerate(train_results[:20]):
        te = simulate_windows(p, test_windows)
        diff = te["pnl"] - tr["pnl"]
        star = " *" if p == optimal else ""
        print(f"{i+1:4d} ${tr['pnl']:>+7.2f} ${te['pnl']:>+7.2f} ${diff:>+7.2f}{star}")

    # ---- Conclusion ----
    print("\n" + "=" * 65)
    print("CONCLUSION")
    print("=" * 65)
    opt_test = test_res["pnl"]
    edge_over_random = opt_test - rand_avg
    edge_over_best_random = opt_test - rand_best

    if opt_test > 0 and edge_over_random > 10 and not better_than_best:
        verdict = "LIKELY REAL EDGE (but see note below)"
    elif opt_test > 0 and better_than_best:
        verdict = "POSSIBLE OVERFITTING - optimal beats all 100 random sets"
    elif opt_test <= 0:
        verdict = "NO EDGE - negative or zero PnL on test set"
    else:
        verdict = "INCONCLUSIVE"

    print(f"  Optimal test PnL:              ${opt_test:+.2f}")
    print(f"  Edge over random average:     ${edge_over_random:+.2f}")
    print(f"  Edge over random best:         ${edge_over_best_random:+.2f}")
    print(f"  Verdict: {verdict}")
    print()
    print("  NOTE: Even with cross-validation, 1200 parameter combinations")
    print("  tested on 149 windows is a high ratio. The 'optimal' params")
    print("  may still be partly fitting noise. Treat with caution.")

if __name__ == "__main__":
    main()
