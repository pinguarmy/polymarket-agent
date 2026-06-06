#!/usr/bin/env python3
"""Phase C strategy experiments for the BTC 5m bot.

This intentionally leaves the existing sweep/backtest scripts untouched.  The
baseline logic and cost treatment match scripts/phase_b.py so results are
directly comparable to the current +$152.34 adjusted baseline.
"""
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"

WINDOW_LEN = 300
ENTRY_DELAY = 20

BTC_YES = 3
BTC_NO = 5
YES_LOW = 0.25
YES_HIGH = 0.45
SL_YES = 4
SL_NO = 3
MAX_ENTRIES = 1
SIZE = 10.0

TP_YES = 0.85
TP_NO = 0.88
TP1 = 0.70
TP1_FRACTION = 0.50

SLIPPAGE = 0.005
COST_PER_LEG = 0.02
FEE_PER_SHARE = SLIPPAGE + COST_PER_LEG

BASELINE_PNL = 152.34
BASELINE_WR = 73.3
BASELINE_TRADES = 105


@dataclass
class Result:
    pnl: float
    raw_pnl: float
    wins: int
    losses: int
    tp_exits: int
    sl_exits: int
    time_exits: int
    settlement_exits: int
    tp1_partials: int

    @property
    def trades(self):
        return self.wins + self.losses

    @property
    def win_rate(self):
        return round(self.wins / self.trades * 100, 1) if self.trades else 0.0


def load_settlements():
    with open(CACHE) as f:
        return json.load(f)


def load_all_windows():
    settlements = load_settlements()
    db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))
    cur = db.cursor()
    ws = 1777766100
    all_windows = []

    while ws + WINDOW_LEN <= int(time.time()):
        slug = f"btc-updown-5m-{ws}"
        settlement = settlements.get(slug)
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws))
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ws + WINDOW_LEN))

        cur.execute(
            "SELECT timestamp, price FROM binance_btc_ticks "
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid",
            (start, end),
        )
        btc_ticks = [(row[0], float(row[1])) for row in cur.fetchall()]

        cur.execute("SELECT m.market_id FROM markets m WHERE m.slug=?", (slug,))
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
            all_windows.append((slug, ws, btc_ticks, yes_snapshots, settlement))
        ws += WINDOW_LEN

    db.close()
    return all_windows


def position_price(side, yes_price):
    return yes_price if side == "YES" else 1.0 - yes_price


def exit_direction(entry_yes):
    return "YES" if entry_yes >= 0.55 else "NO"


def rounded_exit_pnl(direction, entry_yes, exit_yes, size):
    if direction == "YES":
        return round((exit_yes - entry_yes) * size, 2)
    return round((entry_yes - exit_yes) * size, 2)


def settlement_pnl(side, entry_yes, settlement, size):
    entry_price = position_price(side, entry_yes)
    won = (settlement == "Up" and side == "YES") or (settlement == "Down" and side == "NO")
    if won:
        return round((1.0 - entry_price) * size, 2), True
    return round(-entry_price * size, 2), False


def close_position(trade, yes_price, size, reason):
    size = min(size, trade["open_size"])
    if size <= 0:
        return 0.0
    pnl = rounded_exit_pnl(trade["direction"], trade["entry_yes"], yes_price, size)
    trade["raw_pnl"] += pnl
    trade["explicit_exit_size"] += size
    trade["open_size"] = round(trade["open_size"] - size, 10)
    if trade["open_size"] <= 0:
        trade["x"] = True
        trade["final_reason"] = reason
    return pnl


def maybe_fixed_stop(trade, btc_change):
    direction = trade["direction"]
    sl = SL_YES if direction == "YES" else SL_NO
    threshold = max(abs(trade["entry_btc_change"]) * sl, 10)
    if direction == "YES" and trade["entry_btc_change"] < 0 and btc_change < -threshold:
        return True
    if direction == "NO" and trade["entry_btc_change"] > 0 and btc_change > threshold:
        return True
    return False


def simulate(windows, *, tiered_tp=False, time_stop=False, trailing_stop=False):
    adjusted_pnl = 0.0
    raw_pnl_total = 0.0
    wins = 0
    losses = 0
    tp_exits = 0
    sl_exits = 0
    time_exits = 0
    settlement_exits = 0
    tp1_partials = 0

    for _slug, window_start, btc_ticks, yes_snapshots, settlement in windows:
        open_price = btc_ticks[0][1]
        trades = []
        snapshot_idx = 0
        yes_mid = 0.5

        for timestamp, btc_price in btc_ticks:
            tick_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
            elapsed = tick_time - window_start
            if elapsed < 0 or elapsed >= WINDOW_LEN:
                continue

            btc_change = btc_price - open_price
            while snapshot_idx < len(yes_snapshots) and yes_snapshots[snapshot_idx][0] <= timestamp:
                yes_mid = yes_snapshots[snapshot_idx][1]
                snapshot_idx += 1

            if yes_mid <= 0.001 or yes_mid >= 0.999:
                continue

            if elapsed >= ENTRY_DELAY:
                can_enter = True
                if trades and len(trades) >= MAX_ENTRIES:
                    can_enter = False
                if trades and elapsed < trades[-1]["entry_elapsed"] + 20:
                    can_enter = False

                if can_enter and btc_change > BTC_NO and elapsed <= 180 and yes_mid <= YES_LOW:
                    direction = exit_direction(yes_mid)
                    trades.append(
                        {
                            "side": "NO",
                            "direction": direction,
                            "entry_yes": yes_mid,
                            "entry_price": position_price(direction, yes_mid),
                            "size": SIZE,
                            "open_size": SIZE,
                            "entry_elapsed": elapsed,
                            "entry_btc_change": btc_change,
                            "x": False,
                            "raw_pnl": 0.0,
                            "explicit_exit_size": 0.0,
                            "final_reason": None,
                            "tp1_done": False,
                            "peak": position_price(direction, yes_mid),
                            "trailing_active": False,
                        }
                    )
                elif can_enter and btc_change < -BTC_YES and elapsed <= 270 and yes_mid >= YES_HIGH:
                    direction = exit_direction(yes_mid)
                    trades.append(
                        {
                            "side": "YES",
                            "direction": direction,
                            "entry_yes": yes_mid,
                            "entry_price": position_price(direction, yes_mid),
                            "size": SIZE,
                            "open_size": SIZE,
                            "entry_elapsed": elapsed,
                            "entry_btc_change": btc_change,
                            "x": False,
                            "raw_pnl": 0.0,
                            "explicit_exit_size": 0.0,
                            "final_reason": None,
                            "tp1_done": False,
                            "peak": position_price(direction, yes_mid),
                            "trailing_active": False,
                        }
                    )

            if elapsed >= 285:
                continue

            for trade in trades:
                if trade["x"]:
                    continue

                current_price = position_price(trade["direction"], yes_mid)
                if current_price > trade["peak"]:
                    trade["peak"] = current_price

                if tiered_tp and not trade["tp1_done"] and current_price >= TP1:
                    close_position(trade, yes_mid, trade["size"] * TP1_FRACTION, "TP")
                    trade["tp1_done"] = True
                    tp1_partials += 1
                    if trade["x"]:
                        continue

                tp_threshold = TP_YES if trade["direction"] == "YES" else TP_NO
                if current_price >= tp_threshold:
                    close_position(trade, yes_mid, trade["open_size"], "TP")
                    continue

                if maybe_fixed_stop(trade, btc_change):
                    close_position(trade, yes_mid, trade["open_size"], "SL")
                    continue

                if time_stop and elapsed - trade["entry_elapsed"] >= 120:
                    favorable_move = trade["peak"] - trade["entry_price"]
                    if favorable_move < 0.02:
                        close_position(trade, yes_mid, trade["open_size"], "TIME")
                        continue

                if trailing_stop:
                    profit = current_price - trade["entry_price"]
                    if profit >= 0.03:
                        trade["trailing_active"] = True
                    if trade["trailing_active"]:
                        stop_price = max(trade["entry_price"], trade["peak"] - 0.02)
                        if current_price <= stop_price:
                            close_position(trade, yes_mid, trade["open_size"], "SL")

        for trade in trades:
            settlement_win_size = 0.0
            if not trade["x"] and trade["open_size"] > 0:
                pnl, won = settlement_pnl(
                    trade["side"],
                    trade["entry_yes"],
                    settlement,
                    trade["open_size"],
                )
                trade["raw_pnl"] += pnl
                trade["final_reason"] = "SETTLEMENT"
                if won:
                    settlement_win_size = trade["open_size"]
                trade["open_size"] = 0.0
                trade["x"] = True

            raw_pnl = round(trade["raw_pnl"], 2)
            raw_pnl_total += raw_pnl

            entry_fee = FEE_PER_SHARE * trade["size"]
            exit_fee = FEE_PER_SHARE * (trade["explicit_exit_size"] + settlement_win_size)
            adjusted_pnl += raw_pnl - entry_fee - exit_fee

            if raw_pnl > 0:
                wins += 1
            elif raw_pnl < 0:
                losses += 1

            if trade["final_reason"] == "TP":
                tp_exits += 1
            elif trade["final_reason"] == "SL":
                sl_exits += 1
            elif trade["final_reason"] == "TIME":
                time_exits += 1
            elif trade["final_reason"] == "SETTLEMENT":
                settlement_exits += 1

    return Result(
        pnl=round(adjusted_pnl, 2),
        raw_pnl=round(raw_pnl_total, 2),
        wins=wins,
        losses=losses,
        tp_exits=tp_exits,
        sl_exits=sl_exits,
        time_exits=time_exits,
        settlement_exits=settlement_exits,
        tp1_partials=tp1_partials,
    )


def print_strategy(name, result):
    improvement = result.pnl - BASELINE_PNL
    print(f"Strategy: {name}")
    print(f"  PnL: ${result.pnl:+.2f}  (vs baseline ${BASELINE_PNL:.2f})")
    print(f"  WR: {result.win_rate:.1f}%   (vs baseline {BASELINE_WR:.1f}%)")
    print(f"  Trades: {result.trades}   (vs baseline {BASELINE_TRADES})")
    exit_line = (
        f"  TP exits: {result.tp_exits} | SL exits: {result.sl_exits} | "
        f"Settlement: {result.settlement_exits}"
    )
    if result.time_exits:
        exit_line += f" | Time exits: {result.time_exits}"
    if result.tp1_partials:
        exit_line += f" | TP1 partials: {result.tp1_partials}"
    print(exit_line)
    print(f"  Improvement: ${improvement:+.2f}")
    print()


def main():
    windows = load_all_windows()
    print(f"Windows: {len(windows)}")
    print()

    baseline = simulate(windows)
    print_strategy("Baseline check", baseline)

    strategies = [
        ("C1 Tiered Take-Profit", {"tiered_tp": True}),
        ("C2 Time-Based Stop", {"time_stop": True}),
        ("C3 Trailing Stop", {"trailing_stop": True}),
        (
            "C4 Combined",
            {"tiered_tp": True, "time_stop": True, "trailing_stop": True},
        ),
    ]

    for name, kwargs in strategies:
        print_strategy(name, simulate(windows, **kwargs))


if __name__ == "__main__":
    main()
