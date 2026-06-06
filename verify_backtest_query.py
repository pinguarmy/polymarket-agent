#!/usr/bin/env python3
"""Verify exactly what the backtest code's query returns"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(".")
db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))

slug = 'btc-updown-5m-1777766400'
window_start = 1777766400
start_str = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
end_str = datetime.fromtimestamp(window_start + 300, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

cur = db.cursor()
cur.execute("SELECT market_id FROM markets WHERE slug=?", (slug,))
mkt_id = cur.fetchone()[0]

# This is the EXACT query from the backtest code
cur.execute(
    "SELECT timestamp, midpoint, best_bid, best_ask, spread, last_trade_price "
    "FROM polymarket_price_snapshots WHERE market_id=? AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
    (mkt_id, start_str, end_str),
)
price_snap_exact = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in cur.fetchall()]

print("="*70)
print("EXACT BACKTEST CODE QUERY RESULTS")
print("="*70)
print(f"Query: ORDER BY timestamp (no secondary sort)")
print(f"Number of rows returned: {len(price_snap_exact)}")
print()

# The key question: which row comes first for each timestamp?
# Let's look at timestamps with both YES and NO rows
print("Rows at 00:03:46Z (TP trigger point):")
for row in price_snap_exact:
    if "00:03:46" in row[0]:
        print(f"  timestamp={row[0]}, yes_mid={row[1]:.3f}, bid={row[2]:.2f}, ask={row[3]:.2f}")

# Simulate the exact while loop from the code
print("\n" + "="*70)
print("SIMULATING EXACT BACKTEST CODE LOGIC")
print("="*70)

# Entry phase
btc_open = 78686.85
snap_idx = 0
entries = []
window_len = 300

# Load BTC ticks
cur.execute(
    "SELECT timestamp, price FROM binance_btc_ticks "
    "WHERE timestamp >= '2026-05-03T00:00:00Z' AND timestamp <= '2026-05-03T00:05:00Z' ORDER BY timestamp"
)
btc_ticks = [(r[0], float(r[1])) for r in cur.fetchall()]

MIN_BTC_MOVE = 7.0
MIN_BTC_MOVE_NO = 10.0
YES_LOW_THRESHOLD = 0.50
YES_HIGH_THRESHOLD = 0.55
ENTRY_DELAY = 20.0
ENTRY_WINDOW_END = 270.0
ENTRY_WINDOW_END_NO = 180
TAKE_PROFIT = 0.94
SIGNAL_PERSIST_SEC = 20.0
MAX_ENTRIES_BUY_YES = 4
MAX_ENTRIES_BUY_NO = 3

first_btc_change = None

# Entry loop
for i, (ts_str, btc_price) in enumerate(btc_ticks):
    if isinstance(ts_str, str):
        tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        continue
    elapsed = tick_dt.timestamp() - window_start
    if elapsed < 0 or elapsed > window_len:
        continue

    btc_change = btc_price - btc_open

    while snap_idx < len(price_snap_exact) and price_snap_exact[snap_idx][0] <= ts_str:
        snap = price_snap_exact[snap_idx]
        yes_mid = snap[1]  # EXACTLY as in the code
        snap_idx += 1

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
                            "size": 8.0, "time": elapsed})
            if first_btc_change is None:
                first_btc_change = btc_change
        elif can_enter and YES_signal:
            entries.append({"side": "YES", "entry_yes": yes_mid, "entry_price": yes_mid,
                            "size": 8.0, "time": elapsed})
            if first_btc_change is None:
                first_btc_change = btc_change

print(f"Entries made: {len(entries)}")
for e in entries:
    print(f"  {e['side']}: entry_yes={e['entry_yes']:.3f} at elapsed={e['time']:.1f}s")

# Exit phase - EXACTLY as in code
print("\n=== EXIT LOOP ===")
mode_a_entries = []
for e in entries:
    me = dict(e)
    me["exited"] = False
    mode_a_entries.append(me)

snap_idx = 0
for ts_str, btc_price in btc_ticks[1:]:
    if isinstance(ts_str, str):
        tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        continue
    elapsed = tick_dt.timestamp() - window_start
    if elapsed < 0 or elapsed > window_len:
        continue
    btc_change = btc_price - btc_open

    while snap_idx < len(price_snap_exact) and price_snap_exact[snap_idx][0] <= ts_str:
        snap = price_snap_exact[snap_idx]
        yes_mid = snap[1]  # EXACTLY as in the code
        snap_idx += 1

    if yes_mid <= 0.001 or yes_mid >= 0.999:
        continue

    if elapsed >= window_len - 15:  # Skip last 15s fake spikes
        continue

    for me in mode_a_entries:
        if me["exited"]:
            continue
        entry_yes = me["entry_yes"]
        direction = "BUY_YES" if entry_yes >= YES_HIGH_THRESHOLD else "BUY_NO"

        # TAKE PROFIT
        if direction == "BUY_YES" and yes_mid >= TAKE_PROFIT:
            me["exited"] = True
            print(f"TP BUY_YES at {ts_str}: entry_yes={entry_yes:.3f}, exit_yes={yes_mid:.3f}")
        elif direction == "BUY_NO" and yes_mid <= 1.0 - TAKE_PROFIT:
            me["exited"] = True
            print(f"TP BUY_NO at {ts_str}: entry_yes={entry_yes:.3f}, yes_mid={yes_mid:.3f}, threshold={1.0-TAKE_PROFIT:.3f}")

print(f"\nFinal: {len([e for e in mode_a_entries if e['exited']])}/{len(mode_a_entries)} exited")

# Key question: what was yes_mid at the TP trigger tick?
print("\n" + "="*70)
print("KEY: What was yes_mid at TP trigger tick 00:03:47?")
print("="*70)
print("The TP triggered at tick 00:03:47 but the snapshot is 00:03:46")
print("Let's trace snap_idx for tick 00:03:47...")

# Re-trace with detailed logging
snap_idx = 0
for ts_str, btc_price in btc_ticks:
    if ts_str == "2026-05-03T00:03:47Z":
        print(f"\nFound target tick: {ts_str}")
        # First, process all snapshots <= this tick
        print(f"Before while loop: snap_idx={snap_idx}")
        while snap_idx < len(price_snap_exact) and price_snap_exact[snap_idx][0] <= ts_str:
            snap = price_snap_exact[snap_idx]
            yes_mid = snap[1]
            print(f"  Processing snap[{snap_idx}]: {snap[0]}, yes_mid={yes_mid:.3f}")
            snap_idx += 1
        print(f"After while loop: snap_idx={snap_idx}, yes_mid from last snap = {yes_mid:.3f}")
        break

db.close()