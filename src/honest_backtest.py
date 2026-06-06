#!/usr/bin/env python3
"""HONEST BACKTEST — no outcome peeking. Phase B: slippage + cost model."""
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

SRC = Path(__file__).resolve().parent
db = sqlite3.connect(str(SRC.parent / "data" / "btc5m.db"))
db.row_factory = sqlite3.Row

# Constants
SLIPPAGE = 0.005   # per leg: buy at price + slippage
COST_RATE = 0.02   # 2% of trade value per side (buy + sell)
SIZE = 10.0

# Optimal parameters from prior analysis
BTC_MIN = 3            # BTC_Y: BTC moves $3 → enter
BTC_MIN_NO = 5         # BTC_N: BTC moves $5 → enter (separate threshold)
YES_LOW = 0.25         # BUY_NO when YES ≤ 0.25 (was 0.45)
YES_HIGH = 0.45        # BUY_YES when YES ≥ 0.45 (was 0.55)

# Load BTC prices
btc_prices = {}
for row in db.execute("SELECT timestamp, price FROM binance_btc_ticks ORDER BY timestamp"):
    try:
        dt = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        btc_prices[int(dt.timestamp())] = float(row["price"])
    except: pass

def btc_at(epoch):
    for offset in range(0, 120):
        p = btc_prices.get(int(epoch) - offset)
        if p is not None: return p
    return None

all_signals = []
markets_processed = 0

for m in db.execute("SELECT market_id, slug FROM markets WHERE slug LIKE 'btc-updown-5m-%'"):
    slug = m["slug"]
    try:
        open_ts = int(slug.split("-")[-1])
    except:
        continue

    btc_open = btc_prices.get(open_ts) or btc_at(open_ts)
    btc_close = btc_prices.get(open_ts+300) or btc_at(open_ts+300)
    if btc_open is None or btc_close is None:
        continue
    btc_up = btc_close > btc_open

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
    markets_processed += 1

    for s in snaps:
        btc_move = s["btc_move"]
        yes = s["price"]
        elapsed = s["elapsed"]

        if elapsed < 10 or elapsed > 270:
            continue

        # Asymmetric thresholds: YES needs $3, NO needs $5
        if abs(btc_move) < BTC_MIN:
            continue

        signal = None
        if btc_move > 0 and abs(btc_move) >= BTC_MIN_NO and yes <= YES_LOW:
            signal = "BUY_NO"
        elif btc_move < 0 and yes >= YES_HIGH:
            signal = "BUY_YES"
        if signal is None:
            continue

        bet_up = (signal == "BUY_YES")
        won = bet_up == btc_up

        # Entry price (before slippage)
        raw_entry = yes if bet_up else 1.0 - yes

        # With-slippage entry: buy at price + slippage
        entry_with_slip = raw_entry + SLIPPAGE

        # --- NO-COST PnL (baseline from original script) ---
        if won:
            pnl_no_cost = (1.0 - raw_entry) * SIZE
        else:
            pnl_no_cost = -raw_entry * SIZE

        # --- WITH-COST PnL ---
        # Buy: pay entry_with_slip * SIZE, plus 2% fee on that amount
        buy_cost = entry_with_slip * SIZE
        buy_fee = buy_cost * COST_RATE

        if won:
            # Win: settlement pays 1.0 per share, but we lose slippage on the sell too
            # Effective sell price = 1.0 - SLIPPAGE (slippage on the settlement side)
            # Fee on settlement = SIZE * COST_RATE
            sell_proceeds = SIZE - (SLIPPAGE * SIZE) - (SIZE * COST_RATE)
            net_pnl = sell_proceeds - buy_cost - buy_fee
        else:
            # Loss: we get 0 from settlement, lose the buy cost + fees
            net_pnl = -buy_cost - buy_fee

        all_signals.append({
            "slug": slug,
            "elapsed": elapsed,
            "btc_move": btc_move,
            "yes": yes,
            "signal": signal,
            "won": won,
            "pnl_no_cost": round(pnl_no_cost, 2),
            "pnl_with_cost": round(net_pnl, 2),
            "btc_up": btc_up,
        })

db.close()

print(f"Markets processed: {markets_processed}")
print(f"Total signals: {len(all_signals)}")

if not all_signals:
    print("No signals — check parameters")
    exit()

# =====================================================================
# SUMMARY: NO-COST vs WITH-COST
# =====================================================================
wins = sum(1 for s in all_signals if s["won"])
total_pnl_no = sum(s["pnl_no_cost"] for s in all_signals)
total_pnl_with = sum(s["pnl_with_cost"] for s in all_signals)

win_rate = wins / len(all_signals) * 100

print(f"\n{'='*60}")
print(f"PARAMETERS: BTC_MIN=${BTC_MIN}, YES_LOW={YES_LOW}, YES_HIGH={YES_HIGH}")
print(f"SLIPPAGE={SLIPPAGE}/leg, COST_RATE={COST_RATE*100}%/side")
print(f"{'='*60}")

print(f"\n--- ALL SIGNALS (no deduplication) ---")
print(f"  Signals:    {len(all_signals)}")
print(f"  Win rate:  {wins}/{len(all_signals)} = {win_rate:.1f}%")
print(f"  PnL (no cost):    ${total_pnl_no:+.2f}")
print(f"  PnL (with cost):  ${total_pnl_with:+.2f}")
print(f"  Cost impact:      ${total_pnl_no - total_pnl_with:+.2f}")

# Deduplicated stats (first signal per market)
by_market = {}
for s in all_signals:
    if s["slug"] not in by_market or s["elapsed"] < by_market[s["slug"]]["elapsed"]:
        by_market[s["slug"]] = s

deduped = list(by_market.values())
wins_d = sum(1 for s in deduped if s["won"])
pnl_no_d = sum(s["pnl_no_cost"] for s in deduped)
pnl_with_d = sum(s["pnl_with_cost"] for s in deduped)
wr_d = wins_d / len(deduped) * 100

print(f"\n--- DEDUPLICATED (first signal per market) ---")
print(f"  Unique markets: {len(deduped)}")
print(f"  Win rate:  {wins_d}/{len(deduped)} = {wr_d:.1f}%")
print(f"  PnL (no cost):    ${pnl_no_d:+.2f}")
print(f"  PnL (with cost):  ${pnl_with_d:+.2f}")
print(f"  Cost impact:      ${pnl_no_d - pnl_with_d:+.2f}")

# =====================================================================
# BREAKDOWN BY SIGNAL TYPE
# =====================================================================
print(f"\n{'='*60}")
print(f"BREAKDOWN BY SIGNAL TYPE")
print(f"{'='*60}")

for sig_type in ["BUY_NO", "BUY_YES"]:
    subset = [s for s in all_signals if s["signal"] == sig_type]
    if not subset:
        continue
    w = sum(1 for s in subset if s["won"])
    p_no = sum(s["pnl_no_cost"] for s in subset)
    p_with = sum(s["pnl_with_cost"] for s in subset)
    wr = w / len(subset) * 100
    print(f"  {sig_type}: {len(subset)} signals, {w}/{len(subset)} ({wr:.1f}%), "
          f"PnL no-cost=${p_no:+.2f}, with-cost=${p_with:+.2f}")