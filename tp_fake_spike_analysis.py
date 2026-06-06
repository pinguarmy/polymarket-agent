#!/usr/bin/env python3
"""Deep analysis: Is TP triggered on real fills or thin-market fake spikes?"""

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

print("="*70)
print("DEEP ANALYSIS: Is TP triggered on real fills or fake spikes?")
print("="*70)

print(f"\nWindow: {slug}")
print(f"Settlement: {resolution} (Up)")
print(f"BTC open: {btc_ticks[0][1]:.2f}, close: {btc_ticks[-1][1]:.2f}, range: {max(t[1] for t in btc_ticks) - min(t[1] for t in btc_ticks):.2f}")

# The TP triggered at 00:03:46 with YES_mid=0.055
# Let's look at this exact snapshot
print("\n" + "="*70)
print("TP TRIGGER POINT ANALYSIS: 00:03:46Z")
print("="*70)

trigger_snap = None
for snap in price_snap:
    if snap[0] == "2026-05-03T00:03:46Z":
        trigger_snap = snap
        break

if trigger_snap:
    ts, mid, bid, ask, spr, ltp = trigger_snap
    print(f"\nSnapshot at {ts}:")
    print(f"  midpoint = {mid:.3f}")
    print(f"  best_bid = {bid:.3f} (highest price to BUY YES)")
    print(f"  best_ask = {ask:.3f} (lowest price to SELL YES)")
    print(f"  spread   = {spr:.3f}")
    print(f"  ltp      = {ltp:.3f}")
    
    # Calculate NO price
    no_mid = 1.0 - mid
    print(f"\n  Derived NO_mid = 1.0 - {mid:.3f} = {no_mid:.3f}")
    
    # Spread as percentage
    spread_pct = abs(spr) / mid * 100 if mid > 0 else 0
    print(f"  Spread %: {spread_pct:.1f}% of price")
    
    # Is this a thin market?
    print(f"\n  >>> THIN MARKET ANALYSIS:")
    print(f"  YES_mid = 0.055 means YES token is 5.5 cents, NO token is 94.5 cents")
    print(f"  This is EXTREMELY lopsided - one side nearly zero")
    print(f"  In a real market with $64 position, this spread would NOT hold")
    
    # What price would you actually get?
    print(f"\n  >>> EXECUTION REALITY CHECK:")
    print(f"  To exit BUY_NO (sell your NO tokens):")
    print(f"    - You sell YES (to go flat)")
    print(f"    - Best bid to sell YES = {bid:.3f}")
    print(f"    - If you sell 8 YES tokens at bid: 8 * {bid:.3f} = ${8*bid:.2f}")
    print(f"    - But midpoint says YES = {mid:.3f}, so implied NO = {no_mid:.3f}")
    print(f"    - TP condition: YES_mid <= 0.06? {mid:.3f} <= 0.06 = {'YES' if mid <= 0.06 else 'NO'}")
    print(f"    - Actual exit price (best_bid): {bid:.3f}")
    print(f"    - Is best_bid ({bid:.3f}) <= 0.06? {'YES' if bid <= 0.06 else 'NO'}")

print("\n" + "="*70)
print("TIMELINE: YES_mid <= 0.06 events (before last 15s)")
print("="*70)

window_len = 300
for snap in price_snap:
    ts, mid, bid, ask, spr, ltp = snap
    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    elapsed = ts_dt.timestamp() - window_start
    
    if mid <= 0.06:
        in_last_15s = elapsed >= 285
        print(f"\n{ts} (elapsed={elapsed:.0f}s) {'*** IN LAST 15s ***' if in_last_15s else ''}")
        print(f"  YES_mid={mid:.3f}, bid={bid:.3f}, ask={ask:.3f}, spread={spr:.3f}, ltp={ltp:.3f}")
        
        # The "true" price check
        # For BUY_NO exit, you need to sell YES
        # If bid < 0.06, you can sell YES for < 0.06
        # But the TP condition checks midpoint, not bid
        print(f"  TP would trigger: YES_mid={mid:.3f} <= 0.06? {'YES' if mid <= 0.06 else 'NO'}")
        print(f"  But actual fill price (best_bid): {bid:.3f}")
        
        # Last trade price analysis
        if ltp != mid:
            print(f"  *** WARNING: ltp={ltp:.3f} != midpoint={mid:.3f}")
            print(f"  This suggests ltp is from the OTHER token (YES vs NO pair)")

print("\n" + "="*70)
print("CRITICAL BUG: Data Model Issue - Which token is which?")
print("="*70)

print("""
The snapshots have TWO rows per timestamp - one for YES, one for NO.
The code does NOT distinguish which token each row represents.

Looking at the raw data pattern:
  00:03:46Z: midpoint=0.945, bid=0.95, ask=0.94  (one token)
  00:03:46Z: midpoint=0.055, bid=0.06, ask=0.05  (other token)

These are clearly complementary pairs:
  If row1 is YES (Up): YES=0.945, NO=0.055
  If row1 is NO (Down): NO=0.945, YES=0.055

The question is: which row does the code pick?

If code picks midpoint=0.945:
  - For BUY_NO, TP condition (YES_mid <= 0.06) = FALSE (0.945 > 0.06)
  - TP would NOT trigger

If code picks midpoint=0.055:
  - For BUY_NO, TP condition (YES_mid <= 0.06) = TRUE (0.055 <= 0.06)
  - TP WOULD trigger

The code appears to be picking the LOWER midpoint value (0.055).
This means it's accidentally using the WRONG token's price!

If midpoint=0.055 is the YES price:
  - YES (Up) is at 0.055, meaning Up is almost certain
  - Settlement=Up confirms this

But if the code treats 0.055 as the price where it can exit BUY_NO:
  - It thinks it's selling YES cheap (0.055) 
  - But actually it bought YES cheap and should sell YES high for profit
  - The logic is BACKWARDS
""")

print("\n" + "="*70)
print("VERDICT: Backtest Cheating Analysis")
print("="*70)

print("""
FINDING 1: TP Triggered on Thin Market with Extreme Spread
----------------------------------------------------------
- YES_mid=0.055 with bid=0.06/ask=0.05 represents a ~18% spread
- This is NOT a price where you can reliably fill $64 orders
- The "price" is an AMM quote, not a real market-clearing price

FINDING 2: Database Row Ordering Dependency (BUG)
---------------------------------------------------
- The code picks whichever row comes first in ORDER BY timestamp
- This is arbitrary and depends on database internals
- Two rows per timestamp (YES/NO pair) but no token type column
- If it picks YES_mid=0.055 instead of YES_mid=0.945, it triggers TP
- If it picked the other row, TP would NOT trigger

FINDING 3: The TP Only Triggers Because Direction Was Wrong
------------------------------------------------------------
- Strategy bet BUY_NO (on Down)
- BTC went UP, settlement was Up, so direction was WRONG
- Mode B (hold to settlement) = -$16.12
- But TP "saved" the trade by exiting early at fake price
- This is the core problem: TP is exiting a WRONG bet at a fake price

FINDING 4: TP Threshold is Too Loose
-------------------------------------
- TAKE_PROFIT = 0.94 means YES >= 0.94 or YES <= 0.06 triggers TP
- 0.94/0.06 threshold allows extremely one-sided prices
- These extreme prices only exist in thin markets
- Real execution would get worse prices (slippage)

FINDING 5: "Skip last 15s" Doesn't Save It
------------------------------------------
- The TP at 00:03:46 (elapsed=226s) is BEFORE the 285s cutoff
- So the fake spike filter doesn't apply
- But the fake spike IS in thin market conditions

CONCLUSION: YES, the backtest IS CHEATING
==========================================
The TP triggers on thin-market prices that don't reflect real fill prices.
The code has a bug where it may be reading the wrong token's price.
The TP is used to exit WRONG bets at fake good prices.
Mode B (honest) loses, Mode A (with TP) wins - that's the red flag.
""")

db.close()