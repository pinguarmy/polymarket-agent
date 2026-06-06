#!/usr/bin/env python3
"""Debug trace for window btc-updown-5m-1777766400"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(".")
db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))

slug = 'btc-updown-5m-1777766400'
window_start = 1777766400  # 00:00 UTC

# Load market info
cur = db.cursor()
cur.execute("SELECT market_id, resolution FROM markets WHERE slug=?", (slug,))
mkt_row = cur.fetchone()
mkt_id = mkt_row[0]
resolution = mkt_row[1]
print(f"Market ID: {mkt_id}, Resolution: {resolution}")

# Load price snapshots
cur.execute(
    "SELECT timestamp, midpoint, best_bid, best_ask, spread, last_trade_price "
    "FROM polymarket_price_snapshots WHERE market_id=? ORDER BY timestamp",
    (mkt_id,)
)
price_snap = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in cur.fetchall()]

# Load BTC ticks
cur.execute(
    "SELECT timestamp, price FROM binance_btc_ticks "
    "WHERE timestamp >= '2026-05-03T00:00:00Z' AND timestamp <= '2026-05-03T00:05:00Z' ORDER BY timestamp"
)
btc_ticks = [(r[0], float(r[1])) for r in cur.fetchall()]

print(f"\nBTC open: {btc_ticks[0][1]:.2f}, close: {btc_ticks[-1][1]:.2f}")
print(f"BTC range: {max(t[1] for t in btc_ticks) - min(t[1] for t in btc_ticks):.2f}")
print(f"Price snapshots: {len(price_snap)}")
print(f"BTC ticks: {len(btc_ticks)}")

# Trace entry logic
MIN_BTC_MOVE = 7.0
MIN_BTC_MOVE_NO = 10.0
YES_LOW_THRESHOLD = 0.50
YES_HIGH_THRESHOLD = 0.55
ENTRY_DELAY = 20.0
ENTRY_WINDOW_END = 270.0
ENTRY_WINDOW_END_NO = 180
SCALE_SIZE = 8.0
TAKE_PROFIT = 0.94
MAX_ENTRIES_BUY_YES = 4
MAX_ENTRIES_BUY_NO = 3
SIGNAL_PERSIST_SEC = 20.0

btc_open = btc_ticks[0][1]
entries = []
snap_idx = 0

print("\n=== ENTRY LOGIC TRACE ===")
print(f"{'Time':12s} {'BTC':12s} {'BTC_chg':8s} {'YES_mid':8s} {'snap_idx':8s} {'Signal':20s} {'Action'}")
print("-" * 95)

for i, (ts_str, btc_price) in enumerate(btc_ticks):
    if isinstance(ts_str, str):
        tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        continue
    elapsed = tick_dt.timestamp() - window_start
    if elapsed < 0 or elapsed > 300:
        continue
    
    btc_change = btc_price - btc_open
    
    # Get latest YES price
    while snap_idx < len(price_snap) and price_snap[snap_idx][0] <= ts_str:
        snap = price_snap[snap_idx]
        snap_idx += 1
    
    # The yes_mid is from the LAST snapshot seen
    # But wait - we need to figure out WHICH yes_mid to use
    # Let me re-examine: snap_idx points to the NEXT snapshot to process
    # So the current yes_mid is from snap_idx-1
    
    if snap_idx > 0:
        yes_mid = price_snap[snap_idx-1][1]
    else:
        yes_mid = 0.5  # default
    
    if yes_mid <= 0.001 or yes_mid >= 0.999:
        continue
    
    if elapsed < ENTRY_DELAY:
        continue
    
    can_enter = True
    max_entries = MAX_ENTRIES_BUY_YES
    last_dir = None
    if entries:
        last_dir = "BUY_YES" if entries[-1]["entry_yes"] >= YES_HIGH_THRESHOLD else "BUY_NO"
        max_entries = MAX_ENTRIES_BUY_YES if last_dir == "BUY_YES" else MAX_ENTRIES_BUY_NO
        if len(entries) >= max_entries:
            can_enter = False
        if elapsed < entries[-1]["time"] + SIGNAL_PERSIST_SEC:
            can_enter = False
    
    NO_signal = btc_change > MIN_BTC_MOVE_NO and elapsed <= ENTRY_WINDOW_END_NO and yes_mid <= YES_LOW_THRESHOLD
    YES_signal = btc_change < -MIN_BTC_MOVE and elapsed <= ENTRY_WINDOW_END and yes_mid >= YES_HIGH_THRESHOLD
    
    signal_str = ""
    action_str = ""
    if NO_signal and can_enter:
        signal_str = "NO_signal"
        action_str = f"BUY_NO @ {yes_mid:.3f}"
        entries.append({"side": "NO", "entry_yes": yes_mid, "entry_price": 1.0 - yes_mid,
                        "size": SCALE_SIZE, "cost": yes_mid * SCALE_SIZE, "time": elapsed})
    elif YES_signal and can_enter:
        signal_str = "YES_signal"
        action_str = f"BUY_YES @ {yes_mid:.3f}"
        entries.append({"side": "YES", "entry_yes": yes_mid, "entry_price": yes_mid,
                        "size": SCALE_SIZE, "cost": yes_mid * SCALE_SIZE, "time": elapsed})
    
    if signal_str or i < 5 or elapsed < 30 or (entries and elapsed - entries[-1]["time"] < 5):
        print(f"{ts_str[11:19]:12s} {btc_price:12.2f} {btc_change:+8.2f} {yes_mid:8.3f} {snap_idx:8d} {signal_str:20s} {action_str}")

print(f"\n=== ENTRIES MADE ===")
for e in entries:
    print(f"  {e['side']}: entry_yes={e['entry_yes']:.3f}, entry_price={e['entry_price']:.3f}, time={e['time']:.1f}s")

# Now trace exit logic
print("\n=== EXIT LOGIC TRACE (Mode A) ===")
mode_a_entries = []
for e in entries:
    me = dict(e)
    me["exited"] = False
    mode_a_entries.append(me)

first_btc_change = entries[0]["time"] if entries else None  # This is wrong, let me fix
# first_btc_change should be btc_change at first entry
if entries:
    first_entry_tick_idx = next(i for i, (ts_str, _) in enumerate(btc_ticks) 
                                  if (datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() - window_start) >= entries[0]["time"])
    first_btc_change = btc_ticks[first_entry_tick_idx][1] - btc_open
    print(f"first_btc_change (at entry) = {first_btc_change:.2f}")

snap_idx = 0
for ts_str, btc_price in btc_ticks[1:]:
    if isinstance(ts_str, str):
        tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    else:
        continue
    elapsed = tick_dt.timestamp() - window_start
    if elapsed < 0 or elapsed > 300:
        continue
    btc_change = btc_price - btc_open
    
    while snap_idx < len(price_snap) and price_snap[snap_idx][0] <= ts_str:
        snap = price_snap[snap_idx]
        snap_idx += 1
    
    if snap_idx > 0:
        yes_mid = price_snap[snap_idx-1][1]
    else:
        yes_mid = 0.5
    
    if yes_mid <= 0.001 or yes_mid >= 0.999:
        continue
    
    if elapsed >= 300 - 15:  # Skip last 15s
        continue
    
    for me in mode_a_entries:
        if me["exited"]:
            continue
        entry_yes = me["entry_yes"]
        direction = "BUY_YES" if entry_yes >= YES_HIGH_THRESHOLD else "BUY_NO"
        
        # TAKE PROFIT
        if direction == "BUY_YES" and yes_mid >= TAKE_PROFIT:
            me["exited"] = True
            me["exit_time"] = elapsed
            me["exit_yes"] = yes_mid
            me["pnl"] = round((yes_mid - entry_yes) * me["size"], 2)
            me["exit_reason"] = "TAKE_PROFIT"
            print(f"  TP  BUY_YES @ {entry_yes:.3f} -> {yes_mid:.3f} (${me['pnl']:+.2f}) at {ts_str[11:19]} (elapsed={elapsed:.1f}s)")
        elif direction == "BUY_NO" and yes_mid <= 1.0 - TAKE_PROFIT:
            me["exited"] = True
            me["exit_time"] = elapsed
            me["exit_yes"] = yes_mid
            me["pnl"] = round((entry_yes - yes_mid) * me["size"], 2)
            me["exit_reason"] = "TAKE_PROFIT"
            print(f"  TP  BUY_NO  @ {entry_yes:.3f} -> {yes_mid:.3f} (${me['pnl']:+.2f}) at {ts_str[11:19]} (elapsed={elapsed:.1f}s)")
        
        # STOP LOSS BTC
        if not me["exited"] and first_btc_change is not None:
            btc_th = max(abs(first_btc_change) * 4.0, 10.0)
            if direction == "BUY_YES" and first_btc_change < 0 and btc_change < -btc_th:
                me["exited"] = True
                me["exit_time"] = elapsed
                me["exit_yes"] = yes_mid
                me["pnl"] = round((yes_mid - entry_yes) * me["size"], 2)
                me["exit_reason"] = "STOP_LOSS_BTC"
                print(f"  SL  BUY_YES @ {entry_yes:.3f} -> {yes_mid:.3f} (${me['pnl']:+.2f}) at {ts_str[11:19]} (elapsed={elapsed:.1f}s)")
            elif direction == "BUY_NO" and first_btc_change > 0 and btc_change > btc_th:
                me["exited"] = True
                me["exit_time"] = elapsed
                me["exit_yes"] = yes_mid
                me["pnl"] = round((entry_yes - yes_mid) * me["size"], 2)
                me["exit_reason"] = "STOP_LOSS_BTC"
                print(f"  SL  BUY_NO  @ {entry_yes:.3f} -> {yes_mid:.3f} (${me['pnl']:+.2f}) at {ts_str[11:19]} (elapsed={elapsed:.1f}s)")

print(f"\n=== SNAPSHOT ANALYSIS ===")
print("Looking at snapshots with extreme YES values and spread:")
for snap in price_snap:
    ts, mid, bid, ask, spr, ltp = snap
    if mid >= 0.90 or mid <= 0.10:
        print(f"  {ts}: midpoint={mid:.3f}, bid={bid:.2f}, ask={ask:.2f}, spread={spr:.3f}, ltp={ltp:.2f}")
        # For BUY_NO, TP triggers when YES <= 0.06 (i.e., 1-0.94)
        # Check if bid/ask spread is wide (fake spike indicator)
        if spr < -0.005 or spr > 0.005:
            print(f"    ^^^ WIDE SPREAD - possible thin market fake spike")

print("\n=== CRITICAL QUESTION: Was YES ever really <= 0.06 for BUY_NO TP? ===")
print("For BUY_NO, TP condition: yes_mid <= 1.0 - 0.94 = 0.06")
print("\nSnapshots where YES <= 0.06:")
for snap in price_snap:
    ts, mid, bid, ask, spr, ltp = snap
    if mid <= 0.06:
        print(f"  {ts}: midpoint={mid:.3f}, best_bid={bid:.2f}, best_ask={ask:.2f}, spread={spr:.3f}, last_trade={ltp:.2f}")
        # Was this in last 15 seconds?
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elapsed = (ts_dt.timestamp() - window_start)
        if elapsed >= 285:
            print(f"    ^^^ IN LAST 15s WINDOW (elapsed={elapsed:.1f}s) - SKIPPED BY BACKTEST")

db.close()