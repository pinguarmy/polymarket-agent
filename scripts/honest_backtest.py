#!/usr/bin/env python3
"""Honest backtest replay — runs both with and without exit rules.

Mode A: Full strategy (TAKE_PROFIT + STOP_LOSS + SETTLEMENT)
Mode B: Naked (entry only, hold to SETTLEMENT)
"""

import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# Optimal params (from enhanced_sweep 265-window result)
MIN_BTC_MOVE = 3.0       # BTC_Y: $3 for BUY YES
MIN_BTC_MOVE_NO = 5.0    # BTC_N: $5 for BUY NO
YES_LOW_THRESHOLD = 0.25
YES_HIGH_THRESHOLD = 0.45
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


def load_btc_ticks(db, start_ts, end_ts):
    start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.cursor()
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp", (start_str, end_str))
    return [(r[0], float(r[1])) for r in cur.fetchall()]


def load_price_snapshots(db, slug, start_ts, end_ts):
    cur = db.cursor()
    cur.execute("SELECT market_id FROM markets WHERE slug=?", (slug,))
    row = cur.fetchone()
    if not row:
        return []
    mkt_id = row[0]
    start_str = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur.execute(
        "SELECT timestamp, midpoint, best_bid, best_ask, spread, last_trade_price "
        "FROM polymarket_price_snapshots WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? ORDER BY rowid",
        (mkt_id, start_str, end_str),
    )
    return [(r[0], float(r[1]) if r[1] else 0, float(r[2]) if r[2] else 0, float(r[3]) if r[3] else 0,
             float(r[4]) if r[4] else 0, float(r[5]) if r[5] else 0) for r in cur.fetchall()]


def get_settlement(db, slug):
    cur = db.cursor()
    cur.execute("SELECT resolution FROM markets WHERE slug=?", (slug,))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    import urllib.request
    url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list):
            data = data[0] if data else {}
        closed = data.get("closed", False)
        prices_raw = data.get("outcomePrices")
        outcomes_raw = data.get("outcomes")
        if prices_raw and outcomes_raw:
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            prices = [float(p) for p in prices]
            if closed and max(prices) >= 0.99:
                winner = outcomes[prices.index(max(prices))]
                try:
                    cur.execute("UPDATE markets SET resolution=? WHERE slug=?", (winner, slug))
                    db.commit()
                except Exception:
                    pass
                return winner
    except Exception:
        pass
    return None


def simulate_window(slug, start_ts, btc_ticks, price_snap, db):
    """Simulate one 5-min window. Returns both mode results."""
    window_len = 300
    if not btc_ticks or len(btc_ticks) < 2:
        return None

    btc_open = btc_ticks[0][1]
    settlement = get_settlement(db, slug)

    # Shared entry logic (same for both modes)
    entries = []  # entries list shared
    first_btc_change = None
    yes_mid = 0.5  # default midpoint

    snap_idx = 0
    for i, (ts_str, btc_price) in enumerate(btc_ticks):
        if isinstance(ts_str, str):
            tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            continue
        elapsed = tick_dt.timestamp() - start_ts
        if elapsed < 0 or elapsed > window_len:
            continue

        btc_change = btc_price - btc_open

        # Get latest YES price
        while snap_idx < len(price_snap) and price_snap[snap_idx][0] <= ts_str:
            snap = price_snap[snap_idx]
            yes_mid = snap[1]
            snap_idx += 1

        if yes_mid <= 0.001 or yes_mid >= 0.999:
            continue

        # ── Entry (same for both modes) ──
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

    if not entries:
        return None

    # ── Mode A: Full exit logic (TAKE_PROFIT + STOP_LOSS + SETTLEMENT) ──
    mode_a_entries = []
    for e in entries:
        me = dict(e)
        me["exited"] = False
        mode_a_entries.append(me)

    # Replay ticks for exit checks
    snap_idx = 0
    for ts_str, btc_price in btc_ticks[1:]:
        if isinstance(ts_str, str):
            tick_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        else:
            continue
        elapsed = tick_dt.timestamp() - start_ts
        if elapsed < 0 or elapsed > window_len:
            continue
        btc_change = btc_price - btc_open

        while snap_idx < len(price_snap) and price_snap[snap_idx][0] <= ts_str:
            snap = price_snap[snap_idx]
            yes_mid = snap[1]
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
                me["exit_time"] = elapsed
                me["exit_yes"] = yes_mid
                me["pnl"] = round((yes_mid - entry_yes) * me["size"], 2)
                me["exit_reason"] = "TAKE_PROFIT"
            elif direction == "BUY_NO" and yes_mid <= 1.0 - TAKE_PROFIT:
                me["exited"] = True
                me["exit_time"] = elapsed
                me["exit_yes"] = yes_mid
                me["pnl"] = round((entry_yes - yes_mid) * me["size"], 2)
                me["exit_reason"] = "TAKE_PROFIT"

            # STOP LOSS BTC
            if not me["exited"] and first_btc_change is not None:
                btc_th = max(abs(first_btc_change) * STOP_LOSS_BTC_MULTIPLIER, STOP_LOSS_BTC_MIN)
                if direction == "BUY_YES" and first_btc_change < 0 and btc_change < -btc_th:
                    me["exited"] = True
                    me["exit_time"] = elapsed
                    me["exit_yes"] = yes_mid
                    me["pnl"] = round((yes_mid - entry_yes) * me["size"], 2)
                    me["exit_reason"] = "STOP_LOSS_BTC"
                elif direction == "BUY_NO" and first_btc_change > 0 and btc_change > btc_th:
                    me["exited"] = True
                    me["exit_time"] = elapsed
                    me["exit_yes"] = yes_mid
                    me["pnl"] = round((entry_yes - yes_mid) * me["size"], 2)
                    me["exit_reason"] = "STOP_LOSS_BTC"

    # Settlement for unexited mode A entries
    for me in mode_a_entries:
        if not me["exited"]:
            me["exit_reason"] = "SETTLEMENT"
            won = (settlement == "Up" and me["side"] == "YES") or (settlement == "Down" and me["side"] == "NO")
            if won:
                me["exit_yes"] = 1.0
                me["pnl"] = round((1.0 - me["entry_price"]) * me["size"], 2)
            else:
                me["exit_yes"] = 0.0
                me["pnl"] = round(-me["entry_price"] * me["size"], 2)
            me["settlement"] = settlement

    # ── Mode B: Naked (entry only, hold to SETTLEMENT) ──
    mode_b_entries = []
    for e in entries:
        me = dict(e)
        won = (settlement == "Up" and me["side"] == "YES") or (settlement == "Down" and me["side"] == "NO")
        if won:
            me["pnl"] = round((1.0 - me["entry_price"]) * me["size"], 2)
            me["result"] = "WIN"
        else:
            me["pnl"] = round(-me["entry_price"] * me["size"], 2)
            me["result"] = "LOSS"
        mode_b_entries.append(me)

    return {
        "slug": slug,
        "btc_open": round(btc_open, 2),
        "btc_range": round(max(b[1] for b in btc_ticks) - min(b[1] for b in btc_ticks), 2),
        "settlement": settlement or "?",
        "mode_a": {"entries": mode_a_entries, "total_pnl": round(sum(e.get("pnl", 0) for e in mode_a_entries), 2)},
        "mode_b": {"entries": mode_b_entries, "total_pnl": round(sum(e.get("pnl", 0) for e in mode_b_entries), 2)},
    }


def main():
    start_ts = 1777766100  # ~00:00 UTC May 3
    end_ts = int(time.time())

    print(f"Backtest from {datetime.fromtimestamp(start_ts, tz=timezone.utc)}")
    print(f"         to {datetime.fromtimestamp(end_ts, tz=timezone.utc)}")
    print()

    db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))

    window_start = (start_ts // 300) * 300
    results = []
    while window_start + 300 <= end_ts:
        slug = f"btc-updown-5m-{window_start}"
        btc = load_btc_ticks(db, window_start, window_start + 300)
        ps = load_price_snapshots(db, slug, window_start, window_start + 300)
        r = simulate_window(slug, window_start, btc, ps, db)
        if r:
            results.append(r)
        window_start += 300

    # Aggregate
    mode_a_pnl = sum(r["mode_a"]["total_pnl"] for r in results)
    mode_b_pnl = sum(r["mode_b"]["total_pnl"] for r in results)
    mode_a_wins = sum(1 for r in results if r["mode_a"]["total_pnl"] > 0)
    mode_a_losses = sum(1 for r in results if r["mode_a"]["total_pnl"] < 0)
    mode_b_wins = sum(1 for r in results if r["mode_b"]["total_pnl"] > 0)
    mode_b_losses = sum(1 for r in results if r["mode_b"]["total_pnl"] < 0)
    mode_a_trades = sum(len(r["mode_a"]["entries"]) for r in results)
    mode_b_trades = sum(len(r["mode_b"]["entries"]) for r in results)

    print(f"{'Window':20s} {'Settlement':10s} {'Mode A (full)':>25s} {'Mode B (naked)':>25s}")
    print("-" * 80)
    for r in results:
        ws_dt = datetime.fromtimestamp(int(r["slug"].split("-")[-1]), tz=timezone.utc)
        a_pnl = r["mode_a"]["total_pnl"]
        b_pnl = r["mode_b"]["total_pnl"]
        a_detail = "; ".join(f"{e['side']}→{e.get('exit_reason','?')[:6]}${e['pnl']:+.1f}" for e in r["mode_a"]["entries"])
        b_detail = "; ".join(f"{e['side']}${e['pnl']:+.1f}" for e in r["mode_b"]["entries"])
        a_str = f"{'✅' if a_pnl>0 else '❌'} ${a_pnl:+.1f} ({a_detail[:35]})"
        b_str = f"{'✅' if b_pnl>0 else '❌'} ${b_pnl:+.1f} ({b_detail[:35]})"
        print(f"{ws_dt.strftime('%H:%M'):20s} {r['settlement'] or '?':10s} {a_str:>25s} {b_str:>25s}")

    print("-" * 80)
    print(f"\n{'':20s} {'Mode A (full)':>25s} {'Mode B (naked)':>25s}")
    print(f"{'Windows traded':20s} {len(results):>25d} {len(results):>25d}")
    print(f"{'Win/Loss':20s} {mode_a_wins:>3d}W/{mode_a_losses:>2d}L {mode_b_wins:>18d}W/{mode_b_losses:>2d}L")
    if mode_a_wins + mode_a_losses > 0:
        print(f"{'Win rate':20s} {mode_a_wins/(mode_a_wins+mode_a_losses)*100:>24.1f}%", end="")
        if mode_b_wins + mode_b_losses > 0:
            print(f" {mode_b_wins/(mode_b_wins+mode_b_losses)*100:>24.1f}%")
        else:
            print()
    print(f"{'Total trades':20s} {mode_a_trades:>25d} {mode_b_trades:>25d}")
    print(f"{'Total PnL':20s} ${mode_a_pnl:>+22.2f} ${mode_b_pnl:>+22.2f}")

    # Per-trade stats for mode A
    print(f"\n=== Mode A exit breakdown ===")
    reasons = defaultdict(lambda: {"count": 0, "total_pnl": 0.0})
    for r in results:
        for e in r["mode_a"]["entries"]:
            reason = e.get("exit_reason", "?")
            reasons[reason]["count"] += 1
            reasons[reason]["total_pnl"] += e.get("pnl", 0)
    for reason, data in sorted(reasons.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        print(f"  {reason:15s} {data['count']:3d} trades | total ${data['total_pnl']:+.2f} | avg ${data['total_pnl']/data['count']:+.2f}")

    db.close()


if __name__ == "__main__":
    main()
