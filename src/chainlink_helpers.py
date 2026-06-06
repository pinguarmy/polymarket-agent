"""Chainlink BTC/USD read-only helper and health check.

Provides functions to query chainlink_btc_ticks table for settlement prices.
Does NOT modify any trading logic — read-only.

Usage:
    from chainlink_helpers import (
        get_latest_chainlink_price,
        get_chainlink_price_at_or_before,
        get_chainlink_price_nearest,
        write_chainlink_health,
    )

    # Latest price
    tick = get_latest_chainlink_price(db)
    print(f"BTC: ${tick['value_normalized']:.2f} at {tick['source_timestamp_ms']}")

    # Price at market settlement
    settlement_ts = window_start + 300_000  # 5 min in ms
    tick = get_chainlink_price_at_or_before(db, settlement_ts)
    if tick:
        print(f"Settlement BTC: ${tick['value_normalized']:.2f}")
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEALTH_FILE = PROJECT_ROOT / "logs" / "chainlink_health.json"
logger = logging.getLogger("chainlink_helpers")


def get_chainlink_price_at_or_before(db, timestamp_ms: int) -> Optional[dict]:
    """Get the Chainlink oracle price at or before a given timestamp.

    Args:
        db: Database instance
        timestamp_ms: Unix timestamp in milliseconds

    Returns:
        dict with keys: value_normalized, value_raw, source_timestamp_ms,
        latency_ms, or None if no data found
    """
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT value_normalized, value_raw, source_timestamp_ms, latency_ms
                FROM chainlink_btc_ticks
                WHERE source_timestamp_ms <= ?
                ORDER BY source_timestamp_ms DESC
                LIMIT 1
                """,
                (timestamp_ms,),
            ).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        logger.warning("Failed to get Chainlink price at or before %s: %s", timestamp_ms, e)
    return None


def get_chainlink_price_nearest(
    db, timestamp_ms: int, tolerance_ms: int = 5000
) -> Optional[dict]:
    """Get the Chainlink oracle price nearest to a timestamp, within tolerance.

    Args:
        db: Database instance
        timestamp_ms: Target Unix timestamp in milliseconds
        tolerance_ms: Max allowed deviation in ms (default 5000 = 5s)

    Returns:
        dict with keys: value_normalized, value_raw, source_timestamp_ms,
        latency_ms, deviation_ms, or None if no data within tolerance
    """
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT value_normalized, value_raw, source_timestamp_ms, latency_ms,
                       ABS(source_timestamp_ms - ?) AS deviation_ms
                FROM chainlink_btc_ticks
                ORDER BY deviation_ms ASC
                LIMIT 1
                """,
                (timestamp_ms,),
            ).fetchone()
            if row and row["deviation_ms"] <= tolerance_ms:
                return dict(row)
    except Exception as e:
        logger.warning("Failed to get nearest Chainlink price for %s: %s", timestamp_ms, e)
    return None


def get_latest_chainlink_price(db) -> Optional[dict]:
    """Get the most recent Chainlink oracle price.

    Returns:
        dict with keys: value_normalized, value_raw, source_timestamp_ms,
        latency_ms, topic, created_at, or None
    """
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                """
                SELECT value_normalized, value_raw, source_timestamp_ms,
                       latency_ms, topic, created_at
                FROM chainlink_btc_ticks
                ORDER BY id DESC
                LIMIT 1
                """,
            ).fetchone()
            if row:
                return dict(row)
    except Exception as e:
        logger.warning("Failed to get latest Chainlink price: %s", e)
    return None


def get_chainlink_stats_60s(db) -> dict:
    """Get health stats for the last 60 seconds.

    Returns:
        dict with: count_60s, latest_price, latest_ts_ms, latency_ms,
        age_seconds, is_healthy, messages_per_minute
    """
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - 60_000

    stats = {
        "count_60s": 0,
        "latest_price": None,
        "latest_ts_ms": None,
        "latency_ms": None,
        "age_seconds": None,
        "is_healthy": False,
        "messages_per_minute": 0,
    }

    try:
        with db.get_connection() as conn:
            # Count in last 60s
            count = conn.execute(
                "SELECT COUNT(*) as c FROM chainlink_btc_ticks WHERE source_timestamp_ms >= ?",
                (cutoff_ms,),
            ).fetchone()["c"]
            stats["count_60s"] = count
            stats["messages_per_minute"] = count

            # Latest tick
            latest = conn.execute(
                """
                SELECT value_normalized, source_timestamp_ms, latency_ms
                FROM chainlink_btc_ticks
                ORDER BY id DESC LIMIT 1
                """,
            ).fetchone()

            if latest:
                stats["latest_price"] = latest["value_normalized"]
                stats["latest_ts_ms"] = latest["source_timestamp_ms"]
                stats["latency_ms"] = latest["latency_ms"]
                stats["age_seconds"] = (
                    (now_ms - latest["source_timestamp_ms"]) / 1000.0
                    if latest["source_timestamp_ms"]
                    else None
                )
                # Healthy if we got an update in the last 10s
                stats["is_healthy"] = (
                    stats["age_seconds"] is not None and stats["age_seconds"] < 10
                )
    except Exception as e:
        logger.warning("Failed to get Chainlink 60s stats: %s", e)

    return stats


def write_chainlink_health(db, output_path: Optional[Path] = None) -> dict:
    """Write chainlink health stats to a JSON file.

    Args:
        db: Database instance
        output_path: Path to write health JSON (default: logs/chainlink_health.json)

    Returns:
        dict of health stats
    """
    stats = get_chainlink_stats_60s(db)
    stats["timestamp"] = time.time()
    stats["timestamp_iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    path = output_path or HEALTH_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        logger.warning("Failed to write Chainlink health file %s: %s", path, e)

    return stats
