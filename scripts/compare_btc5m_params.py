#!/usr/bin/env python3
"""Compare the old and new BTC 5m bot parameters on the current dataset."""

import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"
DB_PATH = PROJECT / "data" / "btc5m.db"

WINDOW_LEN = 300
ENTRY_DELAY = 20
YES_ENTRY_END = 270
NO_ENTRY_END = 180
EXIT_END = 285
SIGNAL_PERSIST_SEC = 20
STOP_LOSS_BTC_MIN = 10.0


@dataclass(frozen=True)
class Params:
    label: str
    btc_yes: float
    btc_no: float
    yes_low: float
    yes_high: float
    sl_yes: float
    sl_no: float
    max_entries: int
    tp_yes: float
    tp_no: float
    size: float


PARAMS = [
    Params(
        label="New",
        btc_yes=3.0,
        btc_no=10.0,
        yes_low=0.30,
        yes_high=0.45,
        sl_yes=3.0,
        sl_no=3.0,
        max_entries=2,
        tp_yes=0.85,
        tp_no=0.88,
        size=10.0,
    ),
    Params(
        label="Old",
        btc_yes=3.0,
        btc_no=5.0,
        yes_low=0.25,
        yes_high=0.45,
        sl_yes=4.0,
        sl_no=3.0,
        max_entries=1,
        tp_yes=0.85,
        tp_no=0.88,
        size=10.0,
    ),
]


def load_windows():
    with open(CACHE) as f:
        settlements = json.load(f)

    db = sqlite3.connect(str(DB_PATH))
    cur = db.cursor()
    windows = []
    window_start = 1777766100

    while window_start + WINDOW_LEN <= int(time.time()):
        slug = f"btc-updown-5m-{window_start}"
        settlement = settlements.get(slug)
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start))
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(window_start + WINDOW_LEN))

        cur.execute(
            "SELECT timestamp, price FROM binance_btc_ticks "
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid",
            (start, end),
        )
        btc_ticks = [(row[0], float(row[1])) for row in cur.fetchall()]

        cur.execute("SELECT market_id FROM markets WHERE slug=?", (slug,))
        row = cur.fetchone()
        yes_snapshots = []
        if row:
            cur.execute(
                "SELECT timestamp, midpoint FROM polymarket_price_snapshots "
                "WHERE market_id=? AND side='YES' AND timestamp >= ? AND timestamp <= ? "
                "ORDER BY rowid",
                (row[0], start, end),
            )
            yes_snapshots = [(r[0], float(r[1]) if r[1] else 0.5) for r in cur.fetchall()]

        if len(btc_ticks) >= 5 and len(yes_snapshots) >= 3 and settlement:
            windows.append((slug, window_start, btc_ticks, yes_snapshots, settlement))
        window_start += WINDOW_LEN

    db.close()
    return windows, len(settlements)


def pnl_for_market_exit(side, entry_yes, exit_yes, size):
    if side == "YES":
        return round((exit_yes - entry_yes) * size, 2)
    return round((entry_yes - exit_yes) * size, 2)


def pnl_for_settlement(side, entry_yes, settlement, size):
    entry_price = entry_yes if side == "YES" else 1.0 - entry_yes
    won = (settlement == "Up" and side == "YES") or (settlement == "Down" and side == "NO")
    if won:
        return round((1.0 - entry_price) * size, 2), True
    return round(-entry_price * size, 2), False


def stop_hit(trade, btc_change, params):
    sl_mult = params.sl_yes if trade["side"] == "YES" else params.sl_no
    threshold = max(abs(trade["entry_btc_change"]) * sl_mult, STOP_LOSS_BTC_MIN)
    if trade["side"] == "YES" and trade["entry_btc_change"] < 0:
        return btc_change < -threshold
    if trade["side"] == "NO" and trade["entry_btc_change"] > 0:
        return btc_change > threshold
    return False


def simulate(params, windows):
    trades = []

    for slug, window_start, btc_ticks, yes_snapshots, settlement in windows:
        open_price = btc_ticks[0][1]
        open_trades = []
        snap_idx = 0
        yes_mid = 0.5

        for timestamp, btc_price in btc_ticks:
            tick_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
            elapsed = tick_time - window_start
            if elapsed < 0 or elapsed >= WINDOW_LEN:
                continue

            btc_change = btc_price - open_price
            while snap_idx < len(yes_snapshots) and yes_snapshots[snap_idx][0] <= timestamp:
                yes_mid = yes_snapshots[snap_idx][1]
                snap_idx += 1

            if yes_mid <= 0.001 or yes_mid >= 0.999:
                continue

            if elapsed >= ENTRY_DELAY:
                can_enter = True
                if open_trades and len(open_trades) >= params.max_entries:
                    can_enter = False
                if open_trades and elapsed < open_trades[-1]["entry_elapsed"] + SIGNAL_PERSIST_SEC:
                    can_enter = False

                if can_enter and btc_change > params.btc_no and elapsed <= NO_ENTRY_END and yes_mid <= params.yes_low:
                    open_trades.append(
                        {
                            "slug": slug,
                            "side": "NO",
                            "entry_yes": yes_mid,
                            "entry_elapsed": elapsed,
                            "entry_btc_change": btc_change,
                            "exit_reason": None,
                            "pnl": None,
                        }
                    )
                elif can_enter and btc_change < -params.btc_yes and elapsed <= YES_ENTRY_END and yes_mid >= params.yes_high:
                    open_trades.append(
                        {
                            "slug": slug,
                            "side": "YES",
                            "entry_yes": yes_mid,
                            "entry_elapsed": elapsed,
                            "entry_btc_change": btc_change,
                            "exit_reason": None,
                            "pnl": None,
                        }
                    )

            if elapsed >= EXIT_END:
                continue

            for trade in open_trades:
                if trade["exit_reason"]:
                    continue

                if trade["side"] == "YES" and yes_mid >= params.tp_yes:
                    trade["pnl"] = pnl_for_market_exit("YES", trade["entry_yes"], yes_mid, params.size)
                    trade["exit_reason"] = "TP"
                    trade["exit_yes"] = yes_mid
                    continue
                if trade["side"] == "NO" and 1.0 - yes_mid >= params.tp_no:
                    trade["pnl"] = pnl_for_market_exit("NO", trade["entry_yes"], yes_mid, params.size)
                    trade["exit_reason"] = "TP"
                    trade["exit_yes"] = yes_mid
                    continue

                if stop_hit(trade, btc_change, params):
                    trade["pnl"] = pnl_for_market_exit(trade["side"], trade["entry_yes"], yes_mid, params.size)
                    trade["exit_reason"] = "SL"
                    trade["exit_yes"] = yes_mid

        for trade in open_trades:
            if not trade["exit_reason"]:
                trade["pnl"], trade["settlement_win"] = pnl_for_settlement(
                    trade["side"], trade["entry_yes"], settlement, params.size
                )
                trade["exit_reason"] = "Settlement"
            trade["settlement"] = settlement
            trades.append(trade)

    return summarize(params, trades)


def summarize(params, trades):
    pnl = round(sum(t["pnl"] for t in trades), 2)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    flats = sum(1 for t in trades if t["pnl"] == 0)
    trades_count = len(trades)
    reasons = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "flats": 0})
    sides = Counter()

    for trade in trades:
        reason = trade["exit_reason"]
        reasons[reason]["trades"] += 1
        reasons[reason]["pnl"] += trade["pnl"]
        reasons[reason]["wins"] += int(trade["pnl"] > 0)
        reasons[reason]["losses"] += int(trade["pnl"] < 0)
        reasons[reason]["flats"] += int(trade["pnl"] == 0)
        sides[trade["side"]] += 1

    for reason in reasons.values():
        reason["pnl"] = round(reason["pnl"], 2)

    return {
        "label": params.label,
        "pnl": pnl,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "trades": trades_count,
        "wr": round(wins / trades_count * 100, 1) if trades_count else 0.0,
        "avg": round(pnl / trades_count, 2) if trades_count else 0.0,
        "reasons": dict(reasons),
        "sides": dict(sides),
    }


def fmt_money(value):
    return f"${value:+.2f}"


def print_table(results, windows_count, settlements_count):
    print(f"Settlement cache entries: {settlements_count}")
    print(f"Tradable resolved windows: {windows_count}")
    print()
    print("=== Overall ===")
    print(f"{'Config':<8} {'PnL':>10} {'WR':>7} {'W/L/F':>13} {'Trades':>7} {'Avg/tr':>9} {'YES':>5} {'NO':>5}")
    print("-" * 75)
    for r in results:
        print(
            f"{r['label']:<8} {fmt_money(r['pnl']):>10} {r['wr']:>6.1f}% "
            f"{r['wins']:>3d}/{r['losses']}/{r['flats']:<3d} {r['trades']:>7d} "
            f"{fmt_money(r['avg']):>9} {r['sides'].get('YES', 0):>5d} {r['sides'].get('NO', 0):>5d}"
        )

    print()
    print("=== Exit Breakdown ===")
    print(f"{'Config':<8} {'Exit':<11} {'PnL':>10} {'WR':>7} {'W/L/F':>13} {'Trades':>7} {'Avg/tr':>9}")
    print("-" * 75)
    for r in results:
        for reason in ("TP", "SL", "Settlement"):
            d = r["reasons"].get(reason, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "flats": 0})
            trades = d["trades"]
            wr = round(d["wins"] / trades * 100, 1) if trades else 0.0
            avg = round(d["pnl"] / trades, 2) if trades else 0.0
            print(
                f"{r['label']:<8} {reason:<11} {fmt_money(d['pnl']):>10} {wr:>6.1f}% "
                f"{d['wins']:>3d}/{d['losses']}/{d['flats']:<3d} {trades:>7d} {fmt_money(avg):>9}"
            )


def main():
    windows, settlements_count = load_windows()
    results = [simulate(params, windows) for params in PARAMS]
    print_table(results, len(windows), settlements_count)

    best = max(results, key=lambda r: r["pnl"])
    other = min(results, key=lambda r: r["pnl"])
    print()
    print(
        f"Winner by PnL: {best['label']} "
        f"({fmt_money(best['pnl'] - other['pnl'])} vs {other['label']})."
    )


if __name__ == "__main__":
    main()
