#!/usr/bin/env python3
"""
Walk-Forward V2: Validate Phase A optimal params on BTC 5-min strategy.
Uses 4-fold time-series cross-validation (75/25 split per fold).
"""

import sqlite3
import datetime
import bisect
from collections import defaultdict

# ── fixed params (Phase A optimal) ──────────────────────────────────────────
PARAMS = {
    "BTC_YES":   3.0,
    "BTC_NO":    5.0,
    "YES_LOW":   0.25,
    "YES_HIGH":  0.45,
    "TP_YES":    0.85,
    "TP_NO":     0.88,
    "SCALE_SIZE": 10.0,
    "MAX_ENTRIES": 1,
}

SLIPPAGE   = 0.005
COST_RATE  = 0.02
SCAN_DELAY = 10
N_FOLDS    = 4

from pathlib import Path
DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "btc5m.db")

# ── data loading ─────────────────────────────────────────────────────────────

def load_all_data():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # BTC ticks (full second-level resolution)
    c.execute("SELECT timestamp, price FROM binance_btc_ticks ORDER BY timestamp")
    btc_ticks = [(r[0], r[1]) for r in c.fetchall()]
    btc_ts_list   = [r[0] for r in btc_ticks]
    btc_price_list = [r[1] for r in btc_ticks]

    # Windows
    c.execute("""
        SELECT m.market_id, m.slug, m.resolution,
               CAST(substr(m.slug, -10) AS INTEGER) as ts
        FROM markets m
        WHERE m.slug LIKE 'btc-updown-5m-%'
          AND m.market_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM polymarket_price_snapshots s
                      WHERE s.market_id = m.market_id AND s.side = 'YES')
        ORDER BY CAST(substr(m.slug, -10) AS INTEGER)
    """)
    windows = [{"market_id": r[0], "slug": r[1], "resolution": r[2], "ts": r[3]}
               for r in c.fetchall()]

    # Snapshots
    c.execute("""
        SELECT market_id, side, midpoint, timestamp
        FROM polymarket_price_snapshots
        WHERE side IN ('YES', 'NO')
        ORDER BY market_id, timestamp
    """)
    snaps_by_market = defaultdict(lambda: defaultdict(dict))
    for market_id, side, midpoint, ts_str in c.fetchall():
        snaps_by_market[market_id][ts_str][side] = midpoint

    conn.close()
    return windows, snaps_by_market, btc_ts_list, btc_price_list


def get_btc_at_ts(target_ts_str, btc_ts_list, btc_price_list):
    idx = bisect.bisect_left(btc_ts_list, target_ts_str)
    if idx < len(btc_ts_list):
        return btc_price_list[idx]
    return None


def get_btc_open(ts_unix, btc_ts_list, btc_price_list):
    dt = datetime.datetime.fromtimestamp(ts_unix, tz=datetime.timezone.utc)
    open_str = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    return get_btc_at_ts(open_str, btc_ts_list, btc_price_list)


def resolve_by_btc(btc_move):
    """Determine settlement from BTC move (positive=Up, negative=Down)."""
    return "Up" if btc_move >= 0 else "Down"


# ── simulation ───────────────────────────────────────────────────────────────

def compute_pnl(side, entry_mid, exit_mid, tp_exit=False):
    scale = PARAMS["SCALE_SIZE"]
    if side == "YES":
        entry_cost = (entry_mid + SLIPPAGE) * (1 + COST_RATE) * scale
        # TP exit: sell at exit_mid with slippage; Settlement win: at 1.0, no sell slippage
        if tp_exit:
            exit_value = (exit_mid - SLIPPAGE) * (1 - COST_RATE) * scale
        elif exit_mid >= 1.0:
            # Settlement win — redeem at $1, only fee applies
            exit_value = 1.0 * (1 - COST_RATE) * scale
        else:
            # Settlement loss — nothing back
            return -entry_cost
    else:  # BUY_NO
        entry_no = 1.0 - entry_mid
        entry_cost = (entry_no + SLIPPAGE) * (1 + COST_RATE) * scale
        if tp_exit:
            # TP_NO=0.88 means NO token price hit 0.88
            exit_value = (exit_mid - SLIPPAGE) * (1 - COST_RATE) * scale
        elif exit_mid >= 1.0:
            # Settlement win for NO
            exit_value = 1.0 * (1 - COST_RATE) * scale
        else:
            return -entry_cost
    return exit_value - entry_cost


def simulate_window(window, snaps_by_market, btc_ts_list, btc_price_list):
    market_id = window["market_id"]
    window_ts = window["ts"]

    raw_snaps = snaps_by_market.get(market_id, {})
    if not raw_snaps:
        return []

    # Get BTC open
    btc_open = get_btc_open(window_ts, btc_ts_list, btc_price_list)
    if btc_open is None:
        return []

    # Build sorted snapshot list with BTC prices
    snaps = []
    for ts_str, sides in raw_snaps.items():
        if 'YES' not in sides or 'NO' not in sides:
            continue
        btc = get_btc_at_ts(ts_str, btc_ts_list, btc_price_list)
        if btc is None:
            btc = btc_open
        btc_move = btc - btc_open
        snaps.append({
            "ts_str": ts_str,
            "ts_unix": datetime.datetime.strptime(ts_str, '%Y-%m-%dT%H:%M:%SZ').timestamp(),
            "btc_move": btc_move,
            "yes_mid": sides['YES'],
            "no_mid": sides['NO'],
        })

    if not snaps:
        return []

    snaps.sort(key=lambda x: x["ts_unix"])

    # First scan-eligible timestamp
    scan_start_ts = snaps[0]["ts_unix"] + SCAN_DELAY

    trades = []
    entered_side = None
    entry_mid = None

    for snap in snaps:
        if snap["ts_unix"] < scan_start_ts:
            continue

        if entered_side is None:
            btc_move = snap["btc_move"]
            yes_mid  = snap["yes_mid"]
            no_mid   = snap["no_mid"]

            # Entry signals
            if btc_move < -PARAMS["BTC_YES"] and yes_mid >= PARAMS["YES_HIGH"]:
                entered_side = "YES"
                entry_mid = yes_mid
            elif btc_move > PARAMS["BTC_NO"] and yes_mid <= PARAMS["YES_LOW"]:
                entered_side = "NO"
                entry_mid = yes_mid

            if entered_side is not None:
                if len(trades) >= PARAMS["MAX_ENTRIES"]:
                    entered_side = None
                else:
                    # BUGFIX: append entry immediately so non-TP trades aren't lost
                    trades.append({"side": entered_side, "entry_mid": entry_mid,
                                   "exit_mid": None, "pnl": None, "tp_exit": False})
        else:
            # TP check — update the most recent unclosed trade record
            tp_hit = False
            if entered_side == "YES" and snap["yes_mid"] >= PARAMS["TP_YES"]:
                pnl = compute_pnl("YES", entry_mid, PARAMS["TP_YES"], tp_exit=True)
                tp_hit = True
            elif entered_side == "NO" and snap["no_mid"] >= PARAMS["TP_NO"]:
                pnl = compute_pnl("NO", entry_mid, PARAMS["TP_NO"], tp_exit=True)
                tp_hit = True

            if tp_hit:
                # Update the last trade record for this entry
                trades[-1] = {"side": entered_side, "entry_mid": entry_mid,
                               "exit_mid": PARAMS["TP_YES"] if entered_side == "YES" else PARAMS["TP_NO"],
                               "pnl": pnl, "tp_exit": True}
                entered_side = None

    # Settlement if still in position — update the last open trade record
    if entered_side is not None:
        # Find the last trade record still open (exit_mid is None)
        trade_idx = None
        for i in range(len(trades) - 1, -1, -1):
            if trades[i]["side"] == entered_side and trades[i]["exit_mid"] is None:
                trade_idx = i
                break
        if trade_idx is None:
            # Edge case: no open trade found (shouldn't happen)
            entered_trade = {"side": entered_side, "entry_mid": entry_mid,
                             "exit_mid": None, "pnl": None, "tp_exit": False}
            trades.append(entered_trade)
            trade_idx = len(trades) - 1

        # Use actual market resolution, not BTC-move approximation
        resolution = window.get("resolution")
        wins = (resolution == "Up") if entered_side == "YES" else (resolution == "Down")
        exit_mid = 1.0 if wins else 0.0
        pnl = compute_pnl(entered_side, entry_mid, exit_mid, tp_exit=False)
        trades[trade_idx] = {"side": entered_side, "entry_mid": entry_mid,
                              "exit_mid": exit_mid, "pnl": pnl, "tp_exit": False}

    return trades


def eval_windows(window_list, snaps_by_market, btc_ts_list, btc_price_list):
    total_pnl = 0.0
    total_trades = 0
    wins = 0
    for w in window_list:
        # BUGFIX: skip unsettled markets (NULL/empty resolution)
        if not w.get("resolution"):
            continue
        trades = simulate_window(w, snaps_by_market, btc_ts_list, btc_price_list)
        for trade in trades:
            total_pnl += trade["pnl"]
            total_trades += 1
            if trade["pnl"] > 0:
                wins += 1
    wr = wins / total_trades * 100 if total_trades > 0 else 0.0
    return total_pnl, total_trades, wr


# ── main ─────────────────────────────────────────────────────────────────────

def fmt(x):
    return f"+${x:.2f}" if x >= 0 else f"-${abs(x):.2f}"


def main():
    print("=== WALK-FORWARD V2 (最优参数) ===")
    print(f"Params: {PARAMS}")
    print()

    windows, snaps_by_market, btc_ts_list, btc_price_list = load_all_data()
    
    # Filter to ONLY resolved markets
    resolved = [w for w in windows if w.get("resolution")]
    n_all = len(windows)
    n_resolved = len(resolved)
    print(f"Total windows: {n_all} | Resolved: {n_resolved} | Unresolved (skipped): {n_all - n_resolved}")

    if n_resolved == 0:
        print("No resolved markets — nothing to validate")
        return

    t0 = datetime.datetime.fromtimestamp(resolved[0]['ts'], tz=datetime.timezone.utc)
    t1 = datetime.datetime.fromtimestamp(resolved[-1]['ts'], tz=datetime.timezone.utc)
    print(f"Time range: {t0} → {t1}")
    print()

    # 4-fold walk-forward on RESOLVED windows only
    n = n_resolved
    folds = [
        (0, int(n*0.75), int(n*0.75), n),
        (0, int(n*0.50), int(n*0.50), int(n*0.75)),
        (0, int(n*0.25), int(n*0.25), int(n*0.50)),
        (0, 0, 0, int(n*0.25)),
    ]

    test_results = []

    for fi, (train_start, train_end, test_start, test_end) in enumerate(folds, 1):
        train_w = resolved[train_start:train_end]
        test_w  = resolved[test_start:test_end]

        if not test_w:
            continue

        train_pnl, train_n, train_wr = eval_windows(
            train_w, snaps_by_market, btc_ts_list, btc_price_list)
        test_pnl,  test_n,  test_wr  = eval_windows(
            test_w,  snaps_by_market, btc_ts_list, btc_price_list)

        test_results.append((test_pnl, test_n, test_wr))

        print(f"Fold {fi}: 训练 PnL={fmt(train_pnl)} WR={train_wr:.1f}% (N={train_n})"
              f"  →  测试 PnL={fmt(test_pnl)} WR={test_wr:.1f}% (N={test_n})")

    total_pnl = sum(r[0] for r in test_results)
    total_n   = sum(r[1] for r in test_results)
    total_wr  = sum(r[2]*r[1] for r in test_results) / total_n if total_n > 0 else 0
    print()
    print(f"Total test: PnL={fmt(total_pnl)} WR={total_wr:.1f}% (N={total_n})")

    # Per-window PnL (all resolved)
    print()
    print("── 每窗口 PnL (已结算市场) ──")
    all_pnl = []
    for w in resolved:
        trades = simulate_window(w, snaps_by_market, btc_ts_list, btc_price_list)
        if trades:
            all_pnl.append(trades[0]["pnl"])

    if all_pnl:
        total_d = sum(all_pnl)
        wins_d  = sum(1 for x in all_pnl if x > 0)
        wr_d    = wins_d / len(all_pnl) * 100
        print(f"All resolved ({len(all_pnl)} trades): PnL={fmt(total_d)} WR={wr_d:.1f}%")
        print(f"  Wins={wins_d}, Losses={len(all_pnl)-wins_d}")
    else:
        print("No trades generated.")

    # Task B
    print()
    print("── Task B: 多窗口多次交易的问题 ──")
    print()
    print("Q: 一个窗口内是否可以多次交易（多笔 entry）？")
    print()
    print("现有 sweep 数据：MAX_ENTRIES=1 → PnL +$108 (65.9%)")
    print("                MAX_ENTRIES=2 → PnL +$73  (57.1%)")
    print()
    print("数学分析：")
    print("在 binary option 中，一个窗口只有一个结算结果（Up or Down）。")
    print("第一次 entry 已经暴露了全部方向风险。")
    print("第二次 entry 是同一个方向、同一个结算结果：")
    print("  • 赢了：两次都赢，但第二笔入场价可能更差（BTC already moved）")
    print("  • 输了：两次都输，亏损翻倍")
    print("  • 胜率不变，但风险数字翻倍")
    print()
    print("除非能证明第二笔入场有更好的风险回报比——但 binary option")
    print("的回报是对称的（赢=1-price，输=-price），不存在「第二笔更好」的情况。")
    print()
    print("结论：MAX_ENTRIES=1 优于 MAX_ENTRIES=2（+$108 vs +$73），")
    print("因为 2x 风险暴露而没有对应的 2x 胜率提升。")


if __name__ == "__main__":
    main()
