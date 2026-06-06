"""Polymarket BTC 5-Minute Live Collector Orchestrator.

Runs continuously: market discovery → snapshot collection → BTC ticks.
Stores everything in SQLite. Restartable, graceful shutdown.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from db import Database
from config_btc import ConfigBTC
from market_discovery import discover_and_store
from polymarket_collector import PolymarketCollector
from binance_client import BinanceCollector
from chainlink_collector import ChainlinkCollector

logger = logging.getLogger("live_collector")
_shutdown = Event()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def main():
    parser = argparse.ArgumentParser(description="BTC 5-Minute Live Collector")
    parser.add_argument("--db", default="data/btc5m.db", help="SQLite database path")
    parser.add_argument("--cycle", type=int, default=10, help="Seconds between collection cycles")
    parser.add_argument("--btc-interval", type=float, default=1.0, help="BTC tick interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(args.db)
    db.init_schema()

    pm_collector = PolymarketCollector(db)
    btc_collector = BinanceCollector(db)
    cl_collector = ChainlinkCollector(db)

    # Graceful shutdown
    signal.signal(signal.SIGINT, lambda s, f: _shutdown.set())
    signal.signal(signal.SIGTERM, lambda s, f: _shutdown.set())

    # Start BTC collector (continuous)
    btc_collector.start(interval_sec=args.btc_interval)
    logger.info("BTC collector started (every %.1fs)", args.btc_interval)

    # Start Chainlink RTDS collector (continuous)
    cl_collector.start()
    logger.info("Chainlink RTDS collector started")

    cycle = 0
    logger.info("Live collector running (cycle=%ds). Ctrl+C to stop.", args.cycle)

    try:
        while not _shutdown.is_set():
            cycle += 1
            t0 = time.time()

            # 1. Discover markets
            try:
                n_markets = discover_and_store(db)
            except Exception as e:
                logger.warning("Discovery failed: %s", e)
                n_markets = 0

            # 2. Collect snapshots for active markets
            try:
                n_snapshots = pm_collector.collect_all(db)
            except Exception as e:
                logger.warning("Snapshot collection failed: %s", e)
                n_snapshots = 0

            # 3. Status
            elapsed = time.time() - t0
            with closing(db.get_connection()) as conn:
                btc_ticks = conn.execute("SELECT COUNT(*) as c FROM binance_btc_ticks").fetchone()["c"]
                markets_total = conn.execute("SELECT COUNT(*) as c FROM markets").fetchone()["c"]
                markets_active = conn.execute("SELECT COUNT(*) as c FROM markets WHERE active=1").fetchone()["c"]
                snapshots_total = conn.execute(
                    "SELECT COUNT(*) as c FROM polymarket_price_snapshots"
                ).fetchone()["c"]
                cl_tick_count = conn.execute(
                    "SELECT COUNT(*) as c FROM chainlink_btc_ticks"
                ).fetchone()["c"]
                cl_latest = conn.execute(
                    "SELECT source_timestamp_ms, latency_ms, value_normalized "
                    "FROM chainlink_btc_ticks ORDER BY id DESC LIMIT 1"
                ).fetchone()

            logger.info(
                "cycle=%d | markets=%d active=%d | snapshots=%d(+%d) | btc_ticks=%d | chainlink=%d | "
                "cl_btc=%s latency=%s | %.1fs",
                cycle, markets_total, markets_active, snapshots_total, n_snapshots, btc_ticks,
                cl_tick_count,
                f"${cl_latest['value_normalized']:.2f}" if cl_latest else "N/A",
                f"{cl_latest['latency_ms']}ms" if cl_latest and cl_latest['latency_ms'] is not None else "N/A",
                elapsed,
            )

            if args.once:
                break

            # Wait for next cycle (account for time already spent)
            wait = max(0, args.cycle - elapsed)
            _shutdown.wait(wait)

    finally:
        cl_collector.stop()
        btc_collector.stop()
        with closing(db.get_connection()) as conn:
            markets_total = conn.execute("SELECT COUNT(*) as c FROM markets").fetchone()["c"]
            snapshots_total = conn.execute(
                "SELECT COUNT(*) as c FROM polymarket_price_snapshots"
            ).fetchone()["c"]
            btc_ticks = conn.execute("SELECT COUNT(*) as c FROM binance_btc_ticks").fetchone()["c"]
        logger.info(
            "Shutdown. Total: %d cycles, %d markets, %d snapshots, %d btc_ticks",
            cycle,
            markets_total,
            snapshots_total,
            btc_ticks,
        )


if __name__ == "__main__":
    main()
