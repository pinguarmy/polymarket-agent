"""BTC Paper Backtest v3 — differentiated sizing + flow filter + time cutoff."""
import json, sys
from pathlib import Path
from datetime import datetime

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
from db import Database

# ── Strategy Rules ───────────────────────────────────────────────

FLAT_STRATEGY = {
    "BTC_UP_YES_FLAT":     {"side": "YES", "reason": "BTC up but YES flat — market lagging, buy YES before catch-up"},
    "BTC_DOWN_NO_FLAT":    {"side": "NO",  "reason": "BTC down but NO flat — market lagging, buy NO before catch-up"},
}
FLAT_SIZE = 7.0

FOLLOW_STRATEGY = {
    "BTC_UP_YES_OVERPRICED":   {"side": "YES", "reason": "YES high because BTC actually up — follow momentum"},
    "BTC_DOWN_YES_UNDERPRICED": {"side": "NO",  "reason": "YES low because BTC actually down — follow momentum"},
}

ALL_STRATEGIES = {**FLAT_STRATEGY, **FOLLOW_STRATEGY}

# ── Differentiated Sizing ────────────────────────────────────────

def follow_size(btc_change_abs: float) -> float:
    """Scale FOLLOW bet size by BTC move magnitude.
    
    Small moves are noisier, large moves have more conviction.
    Diminishing returns on extreme moves (risk of reversal).
    """
    c = abs(btc_change_abs)
    if c < 8:
        return 8.0   # noise — small bet
    elif c < 25:
        return 12.0  # moderate conviction
    elif c < 55:
        return 15.0  # strong conviction
    else:
        return 10.0  # extreme — caution, possible reversal

# ── Flow Filter ──────────────────────────────────────────────────

def flow_confirms(db, market_id: int, token_id: str, side: str, btc_up: bool) -> bool:
    """Check if Polymarket trade flow confirms the signal direction.
    
    If BTC is going UP and we want to BUY YES:
      → recent YES trades should be mostly BUYS (flow confirms)
      → if recent YES trades are mostly SELLS, flow contradicts → filter out
    """
    conn = db.get_connection()
    
    # Get recent trades for this market token
    trades = conn.execute("""
        SELECT side, price, size FROM polymarket_trades
        WHERE market_id = ? AND side = ?
        ORDER BY id DESC LIMIT 10
    """, (market_id, side)).fetchall()
    conn.close()
    
    if len(trades) < 3:
        return True  # Not enough data, pass through (don't filter)
    
    # Determine trade flow direction
    # For YES token: higher price = buying pressure, lower = selling
    # For NO token: lower price = buying NO
    buy_pressure = 0
    sell_pressure = 0
    
    for t in trades:
        if t["size"] and t["price"]:
            vol = float(t["size"]) * float(t["price"])
            # Compare price to midpoint heuristic
            if float(t["price"]) > 0.5:  # above 0.5 = buying pressure
                buy_pressure += vol
            else:
                sell_pressure += vol
    
    # Determine if flow confirms our direction
    if btc_up and side == "YES":
        # BTC up → we want YES → flow should show buying pressure on YES
        return buy_pressure > sell_pressure * 0.7
    elif not btc_up and side == "NO":
        # BTC down → we want NO → flow should show selling pressure on YES (buying NO)
        return sell_pressure > buy_pressure * 0.7
    else:
        return True  # FLAT signals pass through

# ── Backtest ─────────────────────────────────────────────────────

def run_backtest(db_path: str, signals_file: str) -> dict:
    db = Database(db_path)
    
    with open(signals_file) as f:
        data = json.load(f)
    
    signals = data["signals"]
    trades = []
    stats = {"flat": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
             "follow": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}}
    
    flow_filtered = 0  # count filtered signals
    
    for sig in signals:
        direction = sig["direction"]
        slug = sig["market_slug"]
        
        strat = ALL_STRATEGIES.get(direction)
        if not strat:
            continue
        
        side = strat["side"]
        cat = "flat" if direction in FLAT_STRATEGY else "follow"
        
        # ── Time cutoff: skip signals in last 60s of window ──
        seconds_elapsed = sig.get("seconds_elapsed", 0)
        if seconds_elapsed > 240:  # 60s before close
            continue
        
        # ── Entry price ──
        yes_mid = sig["yes_mid"]
        if side == "YES":
            entry_price = yes_mid
        else:
            entry_price = 1.0 - yes_mid
        
        # ── BTC outcome ──
        conn = db.get_connection()
        ts = int(slug.split("-")[-1])
        
        open_row = conn.execute(
            "SELECT price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
            (f"{datetime.utcfromtimestamp(ts).isoformat()}Z", 
             f"{datetime.utcfromtimestamp(ts + 300).isoformat()}Z")
        ).fetchone()
        
        close_row = conn.execute(
            "SELECT price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
            (f"{datetime.utcfromtimestamp(ts).isoformat()}Z",
             f"{datetime.utcfromtimestamp(ts + 300).isoformat()}Z")
        ).fetchone()
        
        btc_up = close_row and open_row and close_row["price"] > open_row["price"]
        
        # ── Flow filter (FOLLOW only) ──
        if cat == "follow":
            # Resolve market_id and token_id from slug
            market_row = conn.execute(
                "SELECT market_id, yes_token_id, no_token_id FROM markets WHERE slug = ?",
                (slug,)
            ).fetchone()
            
            if market_row:
                market_id = market_row["market_id"]
                token_id = market_row["yes_token_id"] if side == "YES" else market_row["no_token_id"]
                
                if not flow_confirms(db, market_id, token_id, side, btc_up):
                    flow_filtered += 1
                    conn.close()
                    continue
        
        conn.close()
        
        # ── Size: differentiated for FOLLOW, fixed for FLAT ──
        if cat == "flat":
            size = FLAT_SIZE
        else:
            btc_change = abs(sig.get("btc_change_dollar", 0))
            size = follow_size(btc_change)
        
        cost = entry_price * size
        
        # ── PnL ──
        if side == "YES" and btc_up:
            pnl = (1.0 - entry_price) * size
            won = True
        elif side == "YES" and not btc_up:
            pnl = -cost
            won = False
        elif side == "NO" and not btc_up:
            pnl = (1.0 - entry_price) * size
            won = True
        else:
            pnl = -cost
            won = False
        
        trades.append({
            "slug": slug[-25:],
            "direction": direction,
            "category": cat,
            "side": side,
            "size": size,
            "entry_price": round(entry_price, 4),
            "cost": round(cost, 2),
            "btc_up": btc_up,
            "pnl": round(pnl, 2),
            "won": won,
            "reason": strat["reason"],
            "btc_change": sig["btc_change_dollar"],
            "yes_mid": yes_mid,
            "elapsed": seconds_elapsed,
        })
        
        stats[cat]["trades"] += 1
        stats[cat]["pnl"] += pnl
        if won:
            stats[cat]["wins"] += 1
        else:
            stats[cat]["losses"] += 1
    
    # Compute summary stats
    total_trades = len(trades)
    total_wins = sum(1 for t in trades if t["won"])
    total_losses = total_trades - total_wins
    total_pnl = sum(t["pnl"] for t in trades)
    
    for cat in stats:
        s = stats[cat]
        s["win_rate"] = round(s["wins"] / max(s["trades"], 1) * 100, 1)
        s["avg_pnl"] = round(s["pnl"] / max(s["trades"], 1), 2)
    
    return {
        "strategy": "btc-5min-v3",
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate": round(total_wins / max(total_trades, 1) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / max(total_trades, 1), 2),
        "flat_stats": stats["flat"],
        "follow_stats": stats["follow"],
        "trades": trades,
        "flow_filtered": flow_filtered,
    }

def print_report(r: dict):
    print("=" * 60)
    print("BTC PAPER BACKTEST v3 — Differentiated Sizing + Flow Filter")
    print("=" * 60)
    print(f"Total trades:  {r['total_trades']}")
    print(f"Wins:          {r['total_wins']}")
    print(f"Losses:        {r['total_losses']}")
    print(f"Win rate:      {r['win_rate']}%")
    print(f"Total PnL:     ${r['total_pnl']:+.2f}")
    print(f"Avg PnL/trade: ${r['avg_pnl']:+.2f}")
    if r.get("flow_filtered"):
        print(f"Flow filtered: {r['flow_filtered']} signals rejected")
    print()
    
    for cat, size_val in [("flat", FLAT_SIZE), ("follow", "dynamic")]:
        s = r[f"{cat}_stats"]
        size_label = f"${size_val:.0f}/trade" if isinstance(size_val, (int, float)) else size_val
        label = f"{cat.upper()} ({'manipulation' if cat=='flat' else 'momentum'} — {size_label})"
        print(f"  {label}:")
        print(f"    Trades: {s['trades']} | Wins: {s['wins']} | Losses: {s['losses']} | "
              f"Win rate: {s['win_rate']}% | PnL: ${s['pnl']:+.2f} | Avg: ${s['avg_pnl']:+.2f}")
    
    # Size distribution
    if r["trades"]:
        sizes = [t["size"] for t in r["trades"] if t["category"] == "follow"]
        if sizes:
            from collections import Counter
            dist = Counter(sizes)
            print(f"\n  FOLLOW size distribution:")
            for sz in sorted(dist):
                count = dist[sz]
                pct = count / len(sizes) * 100
                print(f"    ${sz:.0f}: {count} trades ({pct:.0f}%)")
    
    print(f"\nRecent trades:")
    for t in r["trades"][-8:]:
        mark = "✓" if t["won"] else "✗"
        print(f"  {mark} [{t['category']}] {t['direction']:30s} {t['side']} entry=${t['entry_price']:.3f} size={t['size']:.0f} pnl=${t['pnl']:+.2f} btc={t.get('btc_change', 0):+.0f}")
    
    print(f"\nSaved: logs/btc_paper_pnl_v3.json")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/btc5m.db")
    parser.add_argument("--input", default="logs/manipulation_analysis.json")
    parser.add_argument("--output", default="logs/btc_paper_pnl_v3.json")
    args = parser.parse_args()
    
    result = run_backtest(args.db, args.input)
    
    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    
    print_report(result)
