#!/usr/bin/env python3
"""Trading-cost and execution stress test for the BTC 5m strategy."""
import json
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parent.parent
CACHE = PROJECT / "scripts" / "settlement_cache.json"

WINDOW_LEN = 300
ENTRY_DELAY = 20
ENTRY_WINDOW_END = 240
SETTLEMENT_PROTECT_SEC = 60

BTC_YES = 3
BTC_NO = 5
YES_LOW = 0.25
YES_HIGH = 0.45
SL_YES = 4
SL_NO = 3
MAX_ENTRIES = 1
REQUESTED_SIZE = 10.0

TP_YES = 0.85
TP_NO = 0.88


@dataclass
class Scenario:
    name: str
    slippage: float
    cost_per_leg: float
    delay_sec: float = 0.0
    failure_rate: float = 0.0
    fill_fraction: float = 1.0


@dataclass
class Result:
    pnl: float
    raw_pnl: float
    wins: int
    losses: int
    opportunities: int
    skipped: int
    partial_fills: int
    tp_exits: int
    sl_exits: int
    settlement_exits: int

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
    windows = []

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
            windows.append((slug, ws, btc_ticks, yes_snapshots, settlement))
        ws += WINDOW_LEN

    db.close()
    return windows


def position_price(side, yes_price):
    return yes_price if side == "YES" else 1.0 - yes_price


def exit_direction(entry_yes):
    return "YES" if entry_yes >= 0.55 else "NO"


def market_exit_pnl(side, entry_yes, exit_yes, size):
    if side == "YES":
        return round((exit_yes - entry_yes) * size, 2)
    return round((entry_yes - exit_yes) * size, 2)


def settlement_pnl(side, entry_yes, settlement, size):
    entry_price = position_price(side, entry_yes)
    won = (settlement == "Up" and side == "YES") or (settlement == "Down" and side == "NO")
    if won:
        return round((1.0 - entry_price) * size, 2), True
    return round(-entry_price * size, 2), False


def stop_hit(trade, btc_change):
    threshold = max(abs(trade["entry_btc_change"]) * trade["sl_mult"], 10)
    if trade["direction"] == "YES" and trade["entry_btc_change"] < 0:
        return btc_change < -threshold
    if trade["direction"] == "NO" and trade["entry_btc_change"] > 0:
        return btc_change > threshold
    return False


def open_trade(side, yes_mid, elapsed, btc_change, fill_fraction):
    direction = exit_direction(yes_mid)
    size = REQUESTED_SIZE * fill_fraction
    return {
        "side": side,
        "direction": direction,
        "entry_yes": yes_mid,
        "size": size,
        "entry_elapsed": elapsed,
        "entry_btc_change": btc_change,
        "sl_mult": SL_YES if direction == "YES" else SL_NO,
        "raw_pnl": 0.0,
        "explicit_exit_size": 0.0,
        "final_reason": None,
        "x": False,
    }


def execute_exit(trade, yes_mid, reason):
    trade["raw_pnl"] += market_exit_pnl(
        trade["direction"], trade["entry_yes"], yes_mid, trade["size"]
    )
    trade["explicit_exit_size"] = trade["size"]
    trade["final_reason"] = reason
    trade["x"] = True


def simulate(windows, scenario):
    rng = random.Random(42)
    fee_per_share = scenario.slippage + scenario.cost_per_leg
    adjusted_pnl = 0.0
    raw_pnl_total = 0.0
    wins = 0
    losses = 0
    opportunities = 0
    skipped = 0
    partial_fills = 0
    tp_exits = 0
    sl_exits = 0
    settlement_exits = 0
    protect_start = WINDOW_LEN - SETTLEMENT_PROTECT_SEC

    for _slug, window_start, btc_ticks, yes_snapshots, settlement in windows:
        open_price = btc_ticks[0][1]
        trades = []
        snapshot_idx = 0
        yes_mid = 0.5
        pending_entry = None
        pending_exit = None
        signal_skipped = False

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

            if pending_entry and elapsed >= pending_entry["due_elapsed"]:
                trades.append(
                    open_trade(
                        pending_entry["side"],
                        yes_mid,
                        elapsed,
                        btc_change,
                        scenario.fill_fraction,
                    )
                )
                if scenario.fill_fraction < 1.0:
                    partial_fills += 1
                pending_entry = None

            if pending_exit and elapsed >= pending_exit["due_elapsed"]:
                if elapsed < protect_start and not pending_exit["trade"]["x"]:
                    execute_exit(pending_exit["trade"], yes_mid, pending_exit["reason"])
                pending_exit = None

            if elapsed >= ENTRY_DELAY and not pending_entry and not signal_skipped:
                can_enter = True
                if trades and len(trades) >= MAX_ENTRIES:
                    can_enter = False
                if trades and elapsed < trades[-1]["entry_elapsed"] + 20:
                    can_enter = False

                side = None
                if can_enter and btc_change > BTC_NO and elapsed <= 180 and yes_mid <= YES_LOW:
                    side = "NO"
                elif (
                    can_enter
                    and btc_change < -BTC_YES
                    and elapsed <= ENTRY_WINDOW_END
                    and yes_mid >= YES_HIGH
                ):
                    side = "YES"

                if side:
                    opportunities += 1
                    if scenario.failure_rate and rng.random() < scenario.failure_rate:
                        skipped += 1
                        signal_skipped = True
                    elif scenario.delay_sec:
                        pending_entry = {
                            "side": side,
                            "due_elapsed": elapsed + scenario.delay_sec,
                        }
                    else:
                        trades.append(
                            open_trade(
                                side,
                                yes_mid,
                                elapsed,
                                btc_change,
                                scenario.fill_fraction,
                            )
                        )
                        if scenario.fill_fraction < 1.0:
                            partial_fills += 1

            if elapsed >= protect_start:
                continue

            for trade in trades:
                if trade["x"] or pending_exit:
                    continue

                current_price = position_price(trade["direction"], yes_mid)
                tp_threshold = TP_YES if trade["direction"] == "YES" else TP_NO
                reason = None
                if current_price >= tp_threshold:
                    reason = "TP"
                elif stop_hit(trade, btc_change):
                    reason = "SL"

                if reason:
                    if scenario.delay_sec:
                        pending_exit = {
                            "trade": trade,
                            "reason": reason,
                            "due_elapsed": elapsed + scenario.delay_sec,
                        }
                    else:
                        execute_exit(trade, yes_mid, reason)

        for trade in trades:
            settlement_win_size = 0.0
            if not trade["x"]:
                pnl, won = settlement_pnl(
                    trade["side"], trade["entry_yes"], settlement, trade["size"]
                )
                trade["raw_pnl"] += pnl
                trade["final_reason"] = "SETTLEMENT"
                if won:
                    settlement_win_size = trade["size"]
                trade["x"] = True

            raw_pnl = round(trade["raw_pnl"], 2)
            raw_pnl_total += raw_pnl
            entry_fee = fee_per_share * trade["size"]
            exit_fee = fee_per_share * (trade["explicit_exit_size"] + settlement_win_size)
            adjusted_pnl += raw_pnl - entry_fee - exit_fee

            if raw_pnl > 0:
                wins += 1
            elif raw_pnl < 0:
                losses += 1

            if trade["final_reason"] == "TP":
                tp_exits += 1
            elif trade["final_reason"] == "SL":
                sl_exits += 1
            elif trade["final_reason"] == "SETTLEMENT":
                settlement_exits += 1

    return Result(
        pnl=round(adjusted_pnl, 2),
        raw_pnl=round(raw_pnl_total, 2),
        wins=wins,
        losses=losses,
        opportunities=opportunities,
        skipped=skipped,
        partial_fills=partial_fills,
        tp_exits=tp_exits,
        sl_exits=sl_exits,
        settlement_exits=settlement_exits,
    )


def main():
    windows = load_all_windows()
    scenarios = [
        Scenario("Baseline", slippage=0.005, cost_per_leg=0.02),
        Scenario("Conservative", slippage=0.01, cost_per_leg=0.03),
        Scenario("Conservative + 2s delay", slippage=0.01, cost_per_leg=0.03, delay_sec=2),
        Scenario(
            "Conservative + 10% order failure",
            slippage=0.01,
            cost_per_leg=0.03,
            failure_rate=0.10,
        ),
        Scenario(
            "Conservative + partial fill",
            slippage=0.01,
            cost_per_leg=0.03,
            fill_fraction=0.70,
        ),
        Scenario(
            "ALL combined",
            slippage=0.01,
            cost_per_leg=0.03,
            delay_sec=2,
            failure_rate=0.10,
            fill_fraction=0.70,
        ),
    ]

    print(f"Windows: {len(windows)}")
    print(f"ENTRY_WINDOW_END={ENTRY_WINDOW_END}s")
    print(f"SETTLEMENT_PROTECT_SEC={SETTLEMENT_PROTECT_SEC}s")
    print()
    print(
        f"{'Scenario':36s} {'PnL':>9s} {'WR':>7s} {'Trades':>7s} "
        f"{'Opps':>6s} {'Skip':>5s} {'Partial':>7s}"
    )
    print("-" * 86)
    for scenario in scenarios:
        result = simulate(windows, scenario)
        print(
            f"{scenario.name:36s} ${result.pnl:>+8.2f} "
            f"{result.win_rate:>6.1f}% {result.trades:>7d} "
            f"{result.opportunities:>6d} {result.skipped:>5d} {result.partial_fills:>7d}"
        )
        print(
            f"{'':36s} exits: TP {result.tp_exits}, SL {result.sl_exits}, "
            f"Settlement {result.settlement_exits}; raw ${result.raw_pnl:+.2f}"
        )


if __name__ == "__main__":
    main()
