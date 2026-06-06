"""Historical backfill for BTC 5-minute markets.

Runs alongside live_collector.py. Uses stored condition_ids from the DB
to query Polymarket Data API for trade history on closed markets.
Also scans further back in time for undiscovered markets.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from db import Database
from config_btc import ConfigBTC
from gamma_client import _get_json, _extract_markets, _normalize_market, BTC_5M_SLUG_RE
from market_discovery import discover_and_store

logger = logging.getLogger("backfill")

DATA_API = "https://data-api.polymarket.com"


def backfill_trades(db: Database) -> int:
    """For every market in DB that has a condition_id but no trades recorded,
    query Data API and store results in polymarket_trades table."""
    conn = db.get_connection()
    markets = conn.execute(
        "SELECT market_id, condition_id, slug FROM markets WHERE condition_id != ''"
    ).fetchall()
    conn.close()

    stored = 0
    for m in markets:
        # Check if we already have trades
        existing = db.get_connection().execute(
            "SELECT COUNT(*) as c FROM polymarket_trades WHERE market_id = ?",
            (m["market_id"],),
        ).fetchone()["c"]
        if existing > 0:
            continue

        try:
            resp = requests.get(
                f"{DATA_API}/trades",
                params={"market": m["condition_id"], "limit": 200},
                timeout=15,
            )
            resp.raise_for_status()
            trades = resp.json()
            if not isinstance(trades, list):
                continue

            with db.get_connection() as c:
                for t in trades:
                    c.execute(
                        """INSERT OR IGNORE INTO polymarket_trades
                        (market_id, side, price, size, trade_timestamp, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            m["market_id"],
                            t.get("side", ""),
                            float(t.get("price", 0)),
                            float(t.get("size", 0)),
                            str(t.get("timestamp", "")),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                c.commit()
            stored += len(trades)
            logger.info("  %s: %d trades", m["slug"][:50], len(trades))
        except Exception as e:
            logger.debug("  %s: failed — %s", m["slug"][:40], e)

    return stored


def scan_deeper(db: Database, hours_back: int = 24) -> int:
    """Scan timestamps further back than the live collector's 1-hour window.
    Finds closed BTC 5-min markets that Gamma API still knows about."""
    now = int(time.time())
    window_start = now - now % 300
    discovered = 0

    # Skip the first hour (already covered by live collector)
    for offset in range(3600, hours_back * 3600, 300):
        ts = window_start - offset
        slug = f"btc-updown-5m-{ts}"

        # Check if already in DB
        existing = db.get_connection().execute(
            "SELECT COUNT(*) as c FROM markets WHERE slug = ?", (slug,)
        ).fetchone()["c"]
        if existing > 0:
            continue

        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"slug": slug},
                timeout=10,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                market = _normalize_market(data[0])
                if market:
                    with db.get_connection() as conn:
                        conn.execute(
                            """INSERT OR IGNORE INTO markets
                            (market_id, slug, question, condition_id, yes_token_id, no_token_id,
                             open_time, close_time, volume, active, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                            (
                                market["market_id"], market["slug"], market["question"],
                                market["condition_id"], market["yes_token_id"],
                                market["no_token_id"], market["open_time"],
                                market["close_time"], market["volume"],
                                datetime.now(timezone.utc).isoformat(),
                            ),
                        )
                        conn.commit()
                    discovered += 1
                    if discovered % 50 == 0:
                        logger.info("  ... %d markets found so far", discovered)
        except Exception:
            continue

    return discovered


def main():
    parser = argparse.ArgumentParser(description="BTC 5-Minute Historical Backfill")
    parser.add_argument("--db", default="data/btc5m.db")
    parser.add_argument("--hours", type=int, default=24, help="How far back to scan")
    parser.add_argument("--trades-only", action="store_true", help="Only backfill trades, no scanning")
    parser.add_argument("--scan-only", action="store_true", help="Only scan for markets, no trades")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")

    db = Database(args.db)
    db.init_schema()

    if not args.trades_only:
        logger.info("Scanning %dh back for undiscovered markets...", args.hours)
        t0 = time.time()
        n = scan_deeper(db, args.hours)
        logger.info("Found %d new closed markets in %.1fs", n, time.time() - t0)

    if not args.scan_only:
        logger.info("Backfilling trade history from Data API...")
        t0 = time.time()
        n = backfill_trades(db)
        logger.info("Stored %d trades in %.1fs", n, time.time() - t0)

    # Summary
    conn = db.get_connection()
    markets = conn.execute("SELECT COUNT(*) as c FROM markets").fetchone()["c"]
    trades = conn.execute("SELECT COUNT(*) as c FROM polymarket_trades").fetchone()["c"]
    conn.close()
    logger.info("DB now: %d markets, %d trades", markets, trades)


if __name__ == "__main__":
    main()
