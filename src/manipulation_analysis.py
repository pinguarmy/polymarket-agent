"""Analyze BTC 5-minute markets for price/probability divergence signals.

Usage:
    python3 src/manipulation_analysis.py
    python3 src/manipulation_analysis.py --btc-threshold 10 --divergence-threshold 0.05
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "btc5m.db"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "logs" / "manipulation_analysis.json"
NEAREST_TICK_SECONDS = 5
SIGNAL_START_SECONDS = 60
SIGNAL_END_SECONDS = 240
BTC_DOLLARS_PER_15_PROB = 30.0
YES_PROB_MOVE_PER_30_DOLLARS = 0.15


@dataclass(frozen=True)
class Thresholds:
    btc_dollars: float
    divergence: float


def parse_timestamp(value: str) -> datetime:
    """Parse UTC ISO timestamps emitted by the collector."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def round_float(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_markets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT market_id, slug, close_time
            FROM markets
            ORDER BY close_time, slug
            """
        )
    )


def fetch_price_rows(conn: sqlite3.Connection, market_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT side, best_bid, best_ask, midpoint, timestamp
            FROM polymarket_price_snapshots
            WHERE market_id = ?
            ORDER BY timestamp, side
            """,
            (market_id,),
        )
    )


def fetch_btc_ticks(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT price, timestamp
        FROM binance_btc_ticks
        WHERE price IS NOT NULL
        ORDER BY timestamp
        """
    )
    return [
        {
            "timestamp": row["timestamp"],
            "dt": parse_timestamp(row["timestamp"]),
            "price": float(row["price"]),
        }
        for row in rows
    ]


def nearest_tick(
    ticks: list[dict[str, Any]],
    timestamp: datetime,
    start_idx: int,
) -> tuple[dict[str, Any] | None, int]:
    """Return nearest BTC tick within NEAREST_TICK_SECONDS and the next scan index."""
    if not ticks:
        return None, start_idx

    lower_bound = timestamp - timedelta(seconds=NEAREST_TICK_SECONDS)
    idx = start_idx
    while idx < len(ticks) and ticks[idx]["dt"] < lower_bound:
        idx += 1

    best_tick: dict[str, Any] | None = None
    best_delta: float | None = None
    scan_idx = idx
    upper_bound = timestamp + timedelta(seconds=NEAREST_TICK_SECONDS)
    while scan_idx < len(ticks) and ticks[scan_idx]["dt"] <= upper_bound:
        delta = abs((ticks[scan_idx]["dt"] - timestamp).total_seconds())
        if best_delta is None or delta < best_delta:
            best_tick = ticks[scan_idx]
            best_delta = delta
        scan_idx += 1

    return best_tick, idx


def opening_btc_price(
    ticks: list[dict[str, Any]],
    close_time: str | None,
    fallback: float | None,
) -> float | None:
    if close_time:
        window_end = parse_timestamp(close_time)
        window_start = window_end - timedelta(minutes=5)
        for tick in ticks:
            if window_start <= tick["dt"] <= window_end:
                return float(tick["price"])
    return fallback


def build_timeline(
    rows: list[sqlite3.Row],
    btc_ticks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        timestamp = row["timestamp"]
        side = str(row["side"]).upper()
        point = grouped.setdefault(timestamp, {"timestamp": timestamp})
        if side == "YES":
            point.update(
                {
                    "yes_mid": row["midpoint"],
                    "yes_bid": row["best_bid"],
                    "yes_ask": row["best_ask"],
                }
            )
        elif side == "NO":
            point.update(
                {
                    "no_mid": row["midpoint"],
                    "no_bid": row["best_bid"],
                    "no_ask": row["best_ask"],
                }
            )

    timeline: list[dict[str, Any]] = []
    tick_idx = 0
    for timestamp in sorted(grouped):
        point = grouped[timestamp]
        if point.get("yes_mid") is None or point.get("no_mid") is None:
            continue

        snapshot_dt = parse_timestamp(timestamp)
        tick, tick_idx = nearest_tick(btc_ticks, snapshot_dt, tick_idx)
        if tick is None:
            continue

        timeline.append(
            {
                "timestamp": timestamp,
                "btc_timestamp": tick["timestamp"],
                "btc_price": float(tick["price"]),
                "yes_mid": float(point["yes_mid"]),
                "no_mid": float(point["no_mid"]),
                "yes_bid": round_float(point.get("yes_bid")),
                "yes_ask": round_float(point.get("yes_ask")),
                "no_bid": round_float(point.get("no_bid")),
                "no_ask": round_float(point.get("no_ask")),
            }
        )

    return timeline


def signal_direction(btc_change: float, yes_divergence: float) -> str:
    if btc_change > 0:
        return "BTC_UP_YES_OVERPRICED" if yes_divergence > 0 else "BTC_UP_YES_FLAT"
    return "BTC_DOWN_NO_FLAT" if yes_divergence > 0 else "BTC_DOWN_YES_UNDERPRICED"


def analyze_market(
    market: sqlite3.Row,
    snapshot_rows: list[sqlite3.Row],
    btc_ticks: list[dict[str, Any]],
    thresholds: Thresholds,
) -> dict[str, Any] | None:
    timeline = build_timeline(snapshot_rows, btc_ticks)
    if not timeline:
        return None

    first_point = timeline[0]
    first_snapshot_at = parse_timestamp(first_point["timestamp"])
    opening_btc = opening_btc_price(btc_ticks, market["close_time"], first_point["btc_price"])
    if opening_btc is None:
        return None

    opening_yes = first_point["yes_mid"]
    opening_no = first_point["no_mid"]
    enriched_points: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    for point in timeline:
        seconds_elapsed = int((parse_timestamp(point["timestamp"]) - first_snapshot_at).total_seconds())
        btc_change = point["btc_price"] - opening_btc
        btc_change_pct = (btc_change / opening_btc) * 100 if opening_btc else 0.0
        yes_change = point["yes_mid"] - opening_yes
        no_change = point["no_mid"] - opening_no
        expected_yes = clamp_probability(
            opening_yes + (btc_change / BTC_DOLLARS_PER_15_PROB) * YES_PROB_MOVE_PER_30_DOLLARS
        )
        yes_divergence = point["yes_mid"] - expected_yes

        enriched = {
            **point,
            "seconds_elapsed": seconds_elapsed,
            "btc_change_dollar": round_float(btc_change, 4),
            "btc_change_pct": round_float(btc_change_pct, 4),
            "yes_change": round_float(yes_change, 6),
            "no_change": round_float(no_change, 6),
            "expected_yes": round_float(expected_yes, 6),
            "yes_divergence": round_float(yes_divergence, 6),
        }
        enriched_points.append(enriched)

        if (
            SIGNAL_START_SECONDS <= seconds_elapsed <= SIGNAL_END_SECONDS
            and abs(btc_change) > thresholds.btc_dollars
            and abs(yes_divergence) > thresholds.divergence
        ):
            signals.append(
                {
                    "market_slug": market["slug"],
                    "timestamp": point["timestamp"],
                    "seconds_elapsed": seconds_elapsed,
                    "btc_change_dollar": round_float(btc_change, 4),
                    "yes_mid": round_float(point["yes_mid"], 6),
                    "expected_yes": round_float(expected_yes, 6),
                    "divergence": round_float(yes_divergence, 6),
                    "direction": signal_direction(btc_change, yes_divergence),
                }
            )

    return {
        "market_id": market["market_id"],
        "market_slug": market["slug"],
        "window_start": isoformat_z(parse_timestamp(market["close_time"]) - timedelta(minutes=5))
        if market["close_time"]
        else None,
        "window_end": market["close_time"],
        "opening_btc": round_float(opening_btc, 4),
        "opening_yes_mid": round_float(opening_yes, 6),
        "opening_no_mid": round_float(opening_no, 6),
        "points_analyzed": len(enriched_points),
        "signals_found": len(signals),
        "signals": signals,
        "timeline": enriched_points,
    }


def analyze(db_path: Path, thresholds: Thresholds) -> dict[str, Any]:
    with connect(db_path) as conn:
        markets = fetch_markets(conn)
        btc_ticks = fetch_btc_ticks(conn)

        market_results: list[dict[str, Any]] = []
        skipped_markets: list[dict[str, str]] = []
        for market in markets:
            rows = fetch_price_rows(conn, market["market_id"])
            if not rows:
                skipped_markets.append({"market_slug": market["slug"], "reason": "no snapshots"})
                continue

            result = analyze_market(market, rows, btc_ticks, thresholds)
            if result is None:
                skipped_markets.append({"market_slug": market["slug"], "reason": "no joined timeline"})
                continue
            market_results.append(result)

    signals = [signal for result in market_results for signal in result["signals"]]
    return {
        "summary": {
            "markets_total": len(markets),
            "markets_analyzed": len(market_results),
            "markets_skipped": len(skipped_markets),
            "signals_found": len(signals),
            "thresholds": {
                "btc_change_dollar": thresholds.btc_dollars,
                "yes_divergence": thresholds.divergence,
                "seconds_elapsed_min": SIGNAL_START_SECONDS,
                "seconds_elapsed_max": SIGNAL_END_SECONDS,
                "nearest_tick_seconds": NEAREST_TICK_SECONDS,
            },
        },
        "signals": signals,
        "markets": market_results,
        "skipped_markets": skipped_markets,
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(
        "Markets analyzed: "
        f"{summary['markets_analyzed']}/{summary['markets_total']} "
        f"(skipped {summary['markets_skipped']})"
    )
    print(f"Signals found: {summary['signals_found']}")
    print(
        "Thresholds: "
        f"BTC > ${summary['thresholds']['btc_change_dollar']:.2f}, "
        f"YES divergence > {summary['thresholds']['yes_divergence']:.2%}"
    )

    if not report["signals"]:
        print("\nNo manipulation signals found.")
        return

    print("\nSignals:")
    header = (
        "market_slug",
        "elapsed",
        "btc_change",
        "yes_mid",
        "expected",
        "divergence",
        "direction",
    )
    print(
        f"{header[0]:<28} {header[1]:>7} {header[2]:>11} "
        f"{header[3]:>7} {header[4]:>9} {header[5]:>10} {header[6]}"
    )
    for signal in report["signals"]:
        print(
            f"{signal['market_slug']:<28} "
            f"{signal['seconds_elapsed']:>7} "
            f"{signal['btc_change_dollar']:>11.2f} "
            f"{signal['yes_mid']:>7.3f} "
            f"{signal['expected_yes']:>9.3f} "
            f"{signal['divergence']:>10.3f} "
            f"{signal['direction']}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="JSON output path",
    )
    parser.add_argument(
        "--btc-threshold",
        type=float,
        default=15.0,
        help="Minimum absolute BTC dollar move for a signal",
    )
    parser.add_argument(
        "--divergence-threshold",
        type=float,
        default=0.08,
        help="Minimum absolute YES divergence for a signal",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thresholds = Thresholds(
        btc_dollars=args.btc_threshold,
        divergence=args.divergence_threshold,
    )
    report = analyze(args.db, thresholds)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print_report(report)
    print(f"\nSaved JSON report to {args.output}")


if __name__ == "__main__":
    main()
