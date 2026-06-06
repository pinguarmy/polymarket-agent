#!/usr/bin/env python3
"""STOP-LOSS PARAMETER COMPARISON BACKTEST (v3) — 使用真实止损逻辑

Compares:
  A组 (当前): STOP_LOSS_BTC_MULTIPLIER=3.0, STOP_LOSS_BTC_MIN=10.0, 无%止损
  B组 (建议): STOP_LOSS_BTC_MULTIPLIER=5.0, STOP_LOSS_BTC_MIN=30.0, STOP_LOSS_BTC_PCT=0.0006

止损逻辑（来自 realtime_trader.py）:
  BUY_YES:  first_btc_change<0 且 btc_change < -max(abs(first_btc_change)*mult, min_abs, pct*entry_btc)
  BUY_NO:   first_btc_change>0 且 btc_change >  max(abs(first_btc_change)*mult, min_abs, pct*entry_btc)
"""
import sqlite3, json, sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
from db import Database

FOLLOW_STRATEGY = {
    "BTC_UP_YES_OVERPRICED":   {"side": "YES"},
    "BTC_DOWN_YES_UNDERPRICED": {"side": "NO"},
}

def follow_size(btc_change_abs: float) -> float:
    c = abs(btc_change_abs)
    if c < 8:   return 8.0
    elif c < 25: return 12.0
    elif c < 55: return 15.0
    else:       return 10.0

def load_signals(signals_file: str) -> list:
    with open(signals_file) as f:
        data = json.load(f)
    return data["signals"]

def build_btc_window(db, open_ts: int) -> list:
    conn = db.get_connection()
    rows = conn.execute("""
        SELECT timestamp, price FROM binance_btc_ticks
        WHERE timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp
    """, (
        datetime.utcfromtimestamp(open_ts).isoformat() + "Z",
        datetime.utcfromtimestamp(open_ts + 300).isoformat() + "Z"
    )).fetchall()
    conn.close()
    result = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            epoch = int(dt.timestamp())
            elapsed = epoch - open_ts
            if 0 <= elapsed <= 300:
                result.append((elapsed, float(r["price"])))
        except:
            pass
    return sorted(result, key=lambda x: x[0])

def run_group(db, signals, mult, min_abs, pct, label):
    trades = []
    
    for sig in signals:
        direction = sig.get("direction", "")
        if direction not in FOLLOW_STRATEGY:
            continue
            
        strat = FOLLOW_STRATEGY[direction]
        side = strat["side"]
        slug = sig["market_slug"]
        yes_mid = sig["yes_mid"]
        elapsed = sig.get("seconds_elapsed", 0)
        
        if elapsed > 240:
            continue
            
        try:
            open_ts = int(slug.split("-")[-1])
        except:
            continue
            
        btc_window = build_btc_window(db, open_ts)
        if len(btc_window) < 2:
            continue
            
        # Find entry index and entry_btc
        entry_btc = None
        entry_idx = 0
        for idx, (el, p) in enumerate(btc_window):
            if el >= elapsed:
                entry_btc = p
                entry_idx = idx
                break
        if entry_btc is None:
            continue
            
        final_btc = btc_window[-1][1]
        btc_up = final_btc > entry_btc
        
        entry_price = yes_mid if side == "YES" else 1.0 - yes_mid
        btc_change_dollar = sig.get("btc_change_dollar", 0)
        size = follow_size(abs(btc_change_dollar))
        cost = entry_price * size
        
        trade_active = True
        exited = False
        exit_reason = None
        exit_pnl = 0.0
        
        # Find YES price at each tick (approximate from midpoint)
        # Use signal's yes_mid as starting point, interpolate based on BTC move
        current_yes = yes_mid
        
        # Get the tick after entry for first_btc_change (can't use entry tick itself — it's 0)
        next_idx = entry_idx + 1
        if next_idx < len(btc_window):
            _, next_btc = btc_window[next_idx]
            first_btc_change = next_btc - entry_btc
        else:
            first_btc_change = 0.0
        
        for idx in range(next_idx, len(btc_window)):
            el, current_btc = btc_window[idx]
            
            if not trade_active:
                break
            
            btc_change = current_btc - entry_btc
            
            # Estimate YES price from BTC movement
            # If BTC moved by X%, YES typically moves ~0.5-0.6X in same direction
            btc_move_pct = btc_change / entry_btc
            yes_estimate = yes_mid + btc_move_pct * 0.55
            yes_estimate = max(0.01, min(0.99, yes_estimate))
            current_yes = yes_estimate
            
            if side == "YES":
                # BUY YES stop logic (from realtime_trader.py)
                if first_btc_change is not None and first_btc_change < 0:
                    # BTC moved against us (down) at entry → continue check
                    threshold_mult = abs(first_btc_change) * mult
                    threshold_pct = entry_btc * pct
                    threshold = max(threshold_mult, min_abs, threshold_pct)
                    
                    # Stop if BTC continues to fall past threshold
                    if btc_change < -threshold:
                        pnl = (current_yes - entry_price) * size
                        exit_pnl = pnl
                        exited = True
                        exit_reason = "STOP_LOSS_BTC"
                        trade_active = False
                        break
                        
            else:  # BUY_NO
                if first_btc_change is not None and first_btc_change > 0:
                    threshold_mult = abs(first_btc_change) * mult
                    threshold_pct = entry_btc * pct
                    threshold = max(threshold_mult, min_abs, threshold_pct)
                    
                    # Stop if BTC continues to rise past threshold
                    if btc_change > threshold:
                        # NO price = 1 - YES
                        no_entry = entry_price
                        no_exit = 1.0 - current_yes
                        pnl = (no_entry - no_exit) * size
                        exit_pnl = pnl
                        exited = True
                        exit_reason = "STOP_LOSS_BTC"
                        trade_active = False
                        break
        
        # If not stopped out, hold to expiration
        if not exited:
            if side == "YES" and btc_up:
                exit_pnl = (1.0 - entry_price) * size
                exit_reason = "HOLD_WIN"
            elif side == "YES" and not btc_up:
                exit_pnl = -cost
                exit_reason = "HOLD_LOSS"
            elif side == "NO" and not btc_up:
                exit_pnl = (1.0 - entry_price) * size
                exit_reason = "HOLD_WIN"
            else:
                exit_pnl = -cost
                exit_reason = "HOLD_LOSS"
        
        won = exit_pnl > 0
        trades.append({
            "direction": direction,
            "side": side,
            "size": size,
            "entry_price": round(entry_price, 4),
            "cost": round(cost, 2),
            "btc_change_dollar": btc_change_dollar,
            "exit_reason": exit_reason,
            "pnl": round(exit_pnl, 2),
            "won": won,
        })
    
    total = len(trades)
    wins = sum(1 for t in trades if t["won"])
    pnl = sum(t["pnl"] for t in trades)
    wr = round(wins / max(total, 1) * 100, 1)
    avg_pnl = round(pnl / max(total, 1), 2)
    
    # Max drawdown
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    
    # Stop-loss hit rate
    stop_hits = sum(1 for t in trades if t["exit_reason"] == "STOP_LOSS_BTC")
    stop_wins = sum(1 for t in trades if t["exit_reason"] == "STOP_LOSS_BTC" and t["won"])
    
    return {
        "label": label,
        "mult": mult,
        "min_abs": min_abs,
        "pct": pct,
        "total_trades": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": wr,
        "total_pnl": round(pnl, 2),
        "avg_pnl": avg_pnl,
        "max_drawdown": round(max_dd, 2),
        "stop_hits": stop_hits,
        "stop_win_rate": round(stop_wins / max(stop_hits, 1) * 100, 1) if stop_hits else 0,
        "trades": trades,
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/btc5m.db")
    parser.add_argument("--input", default="logs/manipulation_analysis.json")
    args = parser.parse_args()
    
    db = Database(args.db)
    signals = load_signals(args.input)
    
    print(f"Loaded {len(signals)} signals\n")
    
    print("Running Group A (3×止损, $10 min, 无%)...")
    group_a = run_group(db, signals, mult=3.0, min_abs=10.0, pct=0.0, label="A组 (当前)")
    
    print("Running Group B (5×止损, $30 min, 0.06%)...")
    group_b = run_group(db, signals, mult=5.0, min_abs=30.0, pct=0.0006, label="B组 (建议)")
    
    print("\n" + "=" * 70)
    print("止损参数对比回测结果 — BTC 5分钟 FOLLOW 策略")
    print("=" * 70)
    print(f"\n{'指标':<20} {'A组 (3×, $10)':<20} {'B组 (5×, $30, 0.06%)':<20}")
    print("-" * 70)
    print(f"{'交易次数':<20} {group_a['total_trades']:<20} {group_b['total_trades']:<20}")
    print(f"{'胜率 (WR)':<20} {group_a['win_rate']:<20} {group_b['win_rate']:<20}")
    print(f"{'总盈亏 ($)':<20} ${group_a['total_pnl']:<19} ${group_b['total_pnl']:<19}")
    print(f"{'平均盈亏 ($)':<20} ${group_a['avg_pnl']:<19} ${group_b['avg_pnl']:<19}")
    print(f"{'最大回撤 ($)':<20} ${group_a['max_drawdown']:<19} ${group_b['max_drawdown']:<19}")
    print(f"{'止损触发次数':<20} {group_a['stop_hits']:<20} {group_b['stop_hits']:<20}")
    print(f"{'止损胜率':<20} {group_a['stop_win_rate']:<20} {group_b['stop_win_rate']:<20}")
    
    # Avg win / avg loss
    a_wins = [t["pnl"] for t in group_a["trades"] if t["won"]]
    a_losses = [abs(t["pnl"]) for t in group_a["trades"] if not t["won"]]
    b_wins = [t["pnl"] for t in group_b["trades"] if t["won"]]
    b_losses = [abs(t["pnl"]) for t in group_b["trades"] if not t["won"]]
    a_avg_win = sum(a_wins)/len(a_wins) if a_wins else 0
    a_avg_loss = sum(a_losses)/len(a_losses) if a_losses else 0
    b_avg_win = sum(b_wins)/len(b_wins) if b_wins else 0
    b_avg_loss = sum(b_losses)/len(b_losses) if b_losses else 0
    print(f"{'平均盈利 ($)':<20} ${a_avg_win:<19.2f} ${b_avg_win:<19.2f}")
    print(f"{'平均亏损 ($)':<20} ${a_avg_loss:<19.2f} ${b_avg_loss:<19.2f}")
    a_ratio = a_avg_win/a_avg_loss if a_avg_loss else 0
    b_ratio = b_avg_win/b_avg_loss if b_avg_loss else 0
    print(f"{'盈亏比':<20} {a_ratio:<20.2f} {b_ratio:<20.2f}")
    
    # Exit reason breakdown
    print("\n--- 退出原因分布 ---")
    for g, name in [(group_a, "A组"), (group_b, "B组")]:
        reasons = defaultdict(int)
        for t in g["trades"]:
            reasons[t["exit_reason"]] += 1
        print(f"\n{name}:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {r}: {c} ({c/len(g['trades'])*100:.1f}%)")
    
    print("\n" + "=" * 70)
    pnl_diff = group_b["total_pnl"] - group_a["total_pnl"]
    
    if group_b["total_pnl"] > group_a["total_pnl"]:
        print(f"✅ B组总盈亏更优 (+${pnl_diff:.2f})，将修改代码！")
        modify = True
    elif group_b["total_pnl"] == group_a["total_pnl"]:
        print(f"⚖️  两组相同，按用户要求修改代码！")
        modify = True
    else:
        print(f"⚠️  A组总盈亏更优 (${-pnl_diff:.2f})，但按用户要求修改代码！")
        modify = True
    
    result = {"group_a": group_a, "group_b": group_b, "modify": modify}
    with open("logs/stoploss_comparison.json", "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n已保存: logs/stoploss_comparison.json")
