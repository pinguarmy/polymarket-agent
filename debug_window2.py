#!/usr/bin/env python3
"""Detailed tick-by-tick trace of Mode A exit logic for window 1777766400"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(".")
db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))

slug = 'btc-updown-5m-1777766400'
window_start = 1777766400

cur = db.cursor()
cur.execute("SELECT market_id, resolution FROM markets WHERE slug=?", (slug,))
mkt_id, resolution = cur.fetchone()

cur.execute(
    "SELECT timestamp, midpoint, best_bid, best_ask, spread, last_trade_price "
    "FROM polymarket_price_snapshots WHERE market_id=? ORDER BY timestamp",
    (mkt_id,)
)
price_snap = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in cur.fetchall()]

cur.execute(
    "SELECT timestamp, price FROM binance_btc_ticks "
    "WHERE timestamp >= '2026-05-03T00:00:00Z' AND timestamp <= '2026-05-03T00:05:00Z' ORDER BY timestamp"
)
btc_ticks = [(r[0], float(r[1])) for r in cur.fetchall()]

# Parameters
MIN_BTC_MOVE = 7.0
MIN_BTC_MOVE_NO = 10.0
YES_LOW_THRESHOLD = 0.50
YES_HIGH_THRESHOLD = 0.55
ENTRY_DELAY = 20.0
ENTRY_WINDOW_END = 270.0
ENTRY_WINDOW_END_NO = 180
SCALE_SIZE = 8.0
STOP_LOSS_BTC_MULTIPLIER = 4.0
STOP_LOSS_BTC_MIN = 10.0
TAKE_PROFIT = 0.94
MAX_ENTRIES_BUY_YES = 4
MAX_ENTRIES_BUY_NO = 3
SIGNAL_PERSIST_SEC = 20.0
window_len = 300

btc_open = btc_ticks[0][1]

# Replicate exact entry logic from simulate_window
entries = []
snap_idx = 0
first_btc_change = None

for i, (ts_str, btc_price) in enumerate(btc_ticks):
    if isinstance(ts_str, str):
        tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        continue
    elapsed = tick_dt.timestamp() - window_start
    if elapsed < 0 or elapsed > window_len:
        continue
    
    btc_change = btc_price - btc_open
    
    while snap_idx < len(price_snap) and price_snap[snap_idx][0] <= ts_str:
        snap_idx += 1
    
    if snap_idx > 0:
        yes_mid = price_snap[snap_idx-1][1]
    else:
        yes_mid = 0.5
    
    if yes_mid <= 0.001 or yes_mid >= 0.999:
        continue
    
    if elapsed >= ENTRY_DELAY:
        can_enter = True
        max_entries = MAX_ENTRIES_BUY_YES
        if entries:
            last_dir = "BUY_YES" if entries[-1]["entry_yes"] >= YES_HIGH_THRESHOLD else "BUY_NO"
            max_entries = MAX_ENTRIES_BUY_YES if last_dir == "BUY_YES" else MAX_ENTRIES_BUY_NO
            if len(entries) >= max_entries:
                can_enter = False
            if elapsed < entries[-1]["time"] + SIGNAL_PERSIST_SEC:
                can_enter = False
        
        NO_signal = btc_change > MIN_BTC_MOVE_NO and elapsed <= ENTRY_WINDOW_END_NO and yes_mid <= YES_LOW_THRESHOLD
        YES_signal = btc_change < -MIN_BTC_MOVE and elapsed <= ENTRY_WINDOW_END and yes_mid >= YES_HIGH_THRESHOLD
        
        if can_enter and NO_signal:
            entries.append({"side": "NO", "entry_yes": yes_mid, "entry_price": 1.0 - yes_mid,
                            "size": SCALE_SIZE, "cost": yes_mid * SCALE_SIZE, "time": elapsed})
            if first_btc_change is None:
                first_btc_change = btc_change
        elif can_enter and YES_signal:
            entries.append({"side": "YES", "entry_yes": yes_mid, "entry_price": yes_mid,
                            "size": SCALE_SIZE, "cost": yes_mid * SCALE_SIZE, "time": elapsed})
            if first_btc_change is None:
                first_btc_change = btc_change

print(f"Entries: {len(entries)}")
for e in entries:
    print(f"  {e['side']}: entry_yes={e['entry_yes']:.3f}, time={e['time']:.1f}s")

# Mode A exit logic - exact replica
mode_a_entries = []
for e in entries:
    me = dict(e)
    me["exited"] = False
    mode_a_entries.append(me)

snap_idx = 0
print("\n=== TICK-BY-TICK EXIT LOGIC (Mode A) ===")
print(f"Take Profit thresholds: BUY_YES -> yes_mid >= {TAKE_PROFIT}, BUY_NO -> yes_mid <= {1.0-TAKE_PROFIT}")
print(f"first_btc_change = {first_btc_change:.2f}")
btc_th = max(abs(first_btc_change) * STOP_LOSS_BTC_MULTIPLIER, STOP_LOSS_BTC_MIN)
print(f"BTC stop loss threshold: {btc_th:.2f}")
print()

for ts_str, btc_price in btc_ticks[1:]:
    if isinstance(ts_str, str):
        tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        continue
    elapsed = tick_dt.timestamp() - window_start
    if elapsed < 0 or elapsed > window_len:
        continue
    btc_change = btc_price - btc_open
    
    # Get latest snapshot
    while snap_idx < len(price_snap) and price_snap[snap_idx][0] <= ts_str:
        snap_idx += 1
    
    if snap_idx > 0:
        yes_mid = price_snap[snap_idx-1][1]
    else:
        yes_mid = 0.5
    
    if yes_mid <= 0.001 or yes_mid >= 0.999:
        continue
    
    if elapsed >= window_len - 15:  # Skip last 15s fake spikes
        continue
    
    # Check each entry
    for me in mode_a_entries:
        if me["exited"]:
            continue
        entry_yes = me["entry_yes"]
        direction = "BUY_YES" if entry_yes >= YES_HIGH_THRESHOLD else "BUY_NO"
        
        # TAKE PROFIT
        tp_triggered = False
        if direction == "BUY_YES" and yes_mid >= TAKE_PROFIT:
            tp_triggered = True
            me["exited"] = True
            me["exit_time"] = elapsed
            me["exit_yes"] = yes_mid
            me["pnl"] = round((yes_mid - entry_yes) * me["size"], 2)
            me["exit_reason"] = "TAKE_PROFIT"
        elif direction == "BUY_NO" and yes_mid <= 1.0 - TAKE_PROFIT:
            tp_triggered = True
            me["exited"] = True
            me["exit_time"] = elapsed
            me["exit_yes"] = yes_mid
            me["pnl"] = round((entry_yes - yes_mid) * me["size"], 2)
            me["exit_reason"] = "TAKE_PROFIT"
        
        if tp_triggered:
            print(f">>> TP TRIGGERED! {direction} entry_yes={entry_yes:.3f}, exit_yes={yes_mid:.3f}, pnl=${me['pnl']:+.2f}")
            print(f"    at tick {ts_str} (elapsed={elapsed:.1f}s)")
            print(f"    snapshot used: {price_snap[snap_idx-1][0]}, yes_mid={yes_mid:.3f}")
            # Check spread at this moment
            snap = price_snap[snap_idx-1]
            print(f"    full snap: midpoint={snap[1]:.3f}, bid={snap[2]:.2f}, ask={snap[3]:.2f}, spread={snap[4]:.3f}")
        
        # STOP LOSS BTC
        if not me["exited"] and first_btc_change is not None:
            if direction == "BUY_YES" and first_btc_change < 0 and btc_change < -btc_th:
                me["exited"] = True
                me["exit_time"] = elapsed
                me["exit_yes"] = yes_mid
                me["pnl"] = round((yes_mid - entry_yes) * me["size"], 2)
                me["exit_reason"] = "STOP_LOSS_BTC"
                print(f">>> SL BTC TRIGGERED! {direction} entry_yes={entry_yes:.3f}, exit_yes={yes_mid:.3f}")
            elif direction == "BUY_NO" and first_btc_change > 0 and btc_change > btc_th:
                me["exited"] = True
                me["exit_time"] = elapsed
                me["exit_yes"] = yes_mid
                me["pnl"] = round((entry_yes - yes_mid) * me["size"], 2)
                me["exit_reason"] = "STOP_LOSS_BTC"
                print(f">>> SL BTC TRIGGERED! {direction} entry_yes={entry_yes:.3f}, exit_yes={yes_mid:.3f}")

print(f"\n=== FINAL MODE A ENTRIES ===")
for me in mode_a_entries:
    print(f"  {me['side']}: exited={me['exited']}, reason={me.get('exit_reason','N/A')}, pnl=${me.get('pnl',0):+.2f}, exit_yes={me.get('exit_yes','N/A')}")

# Mode B
print(f"\n=== MODE B (Naked to Settlement) ===")
settlement = resolution
mode_b_pnl = 0
for e in entries:
    won = (settlement == "Up" and e["side"] == "YES") or (settlement == "Down" and e["side"] == "NO")
    if won:
        pnl = round((1.0 - e["entry_price"]) * e["size"], 2)
        result = "WIN"
    else:
        pnl = round(-e["entry_price"] * e["size"], 2)
        result = "LOSS"
    mode_b_pnl += pnl
    print(f"  {e['side']}: entry_price={e['entry_price']:.3f}, settlement={settlement}, {result}, pnl=${pnl:+.2f}")
print(f"Mode B total PnL: ${mode_b_pnl:+.2f}")

# Critical analysis
print("\n" + "="*70)
print("CRITICAL ANALYSIS")
print("="*70)
print(f"\nThis window: Settlement=Up, BTC went UP from 78686.85 to 78716.79")
print(f"Strategy entry: BUY_NO (betting on Down, expecting BTC manipulation)")
print(f"  - 3 NO entries at YES_mid = 0.385, 0.275, 0.325")
print(f"  - Direction was WRONG: BTC went UP, settlement was Up, so NO loses")
print(f"\nMode B: All 3 NO entries lose -> ${mode_b_pnl:+.2f}")
print(f"\nMode A TP: All 3 NO entries took profit at YES_mid=0.055 -> +$6.56")
print(f"  But: Settlement was UP, so if held to settlement, all would LOSE")
print(f"\nThe TP triggered at elapsed=227s (00:03:47) with YES_mid=0.055")
print(f"This is BEFORE the last 15s (285s threshold)")
print(f"But is 0.055 a REAL price or a thin-market fake?")

# Check the spread on the snapshot where TP triggered
for snap in price_snap:
    if snap[0] == "2026-05-03T00:03:46Z" or snap[0] == "2026-05-03T00:03:45Z":
        print(f"\nSnapshot at {snap[0]}: midpoint={snap[1]:.3f}, bid={snap[2]:.2f}, ask={snap[3]:.2f}")
        print(f"  For BUY_NO TP: need yes_mid <= 0.06, we have {snap[1]:.3f} -> {'PASS' if snap[1] <= 0.06 else 'FAIL'}")
        print(f"  But bid/ask spread: {abs(snap[3]-snap[2]):.2f} ({abs(snap[4]):.3f})")
        print(f"  To BUY_NO you trade at best_ask (the NO side): best_ask = {snap[3]:.2f}")
        print(f"  That's the price to sell NO (bet on Down), meaning buy YES would cost {snap[2]:.2f}")
        print(f"  Wait - let me recalculate...")
        print(f"  If YES_mid=0.055, that means YES (Up) token costs 0.055?")
        print(f"  No wait - the 'YES' in the snapshot IS the Up token")
        print(f"  So YES_mid=0.055 means Up token costs 0.055 (extremely cheap)")
        print(f"  Which means NO (Down) token costs 1-0.055=0.945")
        print(f"  So if you BOUGHT NO at entry_yes=0.385 (cost 0.615 per token)")
        print(f"  And YES (Up) is now 0.055, then NO (Down) is 0.945")
        print(f"  Your NO position is worth 0.945, profit = (0.945-0.615)*8 = ${(0.945-0.615)*8:.2f}")
        print(f"  But wait - last_trade_price=0.06 suggests actual trades were at 0.06")
        print(f"  And bid/ask spread of 0.01 means real price could be anywhere in that range")

db.close()