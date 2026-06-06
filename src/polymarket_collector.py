"""Polymarket CLOB snapshot collector."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from clob_client import DATA_API, fetch_order_book, fetch_price
    from config_btc import ConfigBTC
    from db import Database
except ImportError:  # pragma: no cover - package import path
    from .clob_client import DATA_API, fetch_order_book, fetch_price
    from .config_btc import ConfigBTC
    from .db import Database


logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "PolymarketAgent/0.1 (polymarket-collector; no-auth)",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _trade_timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _fetch_recent_trades(condition_id: str, limit: int = 20) -> list[dict[str, Any]]:
    if not condition_id:
        return []
    try:
        response = _session.get(
            f"{DATA_API}/trades",
            params={"market": condition_id, "limit": limit},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [trade for trade in payload if isinstance(trade, dict)]
        if isinstance(payload, dict):
            data = payload.get("data", [])
            return [trade for trade in data if isinstance(trade, dict)]
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Polymarket recent trades fetch failed for market=%s: %s", condition_id, exc)
    return []


class PolymarketCollector:
    """Collect Polymarket CLOB snapshots for stored active markets."""

    def __init__(self, db: Database):
        self.db = db
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def collect_for_market(self, market_id: str, yes_token_id: str, no_token_id: str) -> int:
        """Collect YES and NO token snapshots for one market."""
        timestamp = _now_iso()
        snapshots: list[tuple[str, str, dict[str, Any], float | None, float | None]] = []

        for side, token_id in (("YES", yes_token_id), ("NO", no_token_id)):
            if not token_id:
                continue
            book = fetch_order_book(token_id)
            if book is None:
                continue
            buy_price = fetch_price(token_id, "BUY")
            sell_price = fetch_price(token_id, "SELL")
            snapshots.append((side, token_id, book, buy_price, sell_price))

        if not snapshots:
            return 0

        with self.db.get_connection() as conn:
            condition_row = conn.execute(
                "SELECT condition_id FROM markets WHERE market_id = ?",
                (market_id,),
            ).fetchone()
            condition_id = condition_row["condition_id"] if condition_row else ""

            for side, token_id, book, buy_price, sell_price in snapshots:
                best_ask = buy_price if buy_price is not None else book["best_ask"]
                best_bid = sell_price if sell_price is not None else book["best_bid"]
                midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else book["midpoint"]
                spread = best_ask - best_bid if best_bid and best_ask else book["spread"]
                spread_pct = (spread / midpoint * 100) if midpoint else book["spread_pct"]

                conn.execute(
                    """
                    INSERT INTO polymarket_price_snapshots (
                        market_id, token_id, side, best_bid, best_ask, midpoint,
                        spread, spread_pct, last_trade_price, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_id,
                        token_id,
                        side,
                        best_bid,
                        best_ask,
                        midpoint,
                        spread,
                        spread_pct,
                        book["last_trade_price"],
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO polymarket_orderbook_snapshots (
                        market_id, token_id, side, bid_depth_5, ask_depth_5,
                        raw_bids, raw_asks, timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_id,
                        token_id,
                        side,
                        book["bid_depth_5"],
                        book["ask_depth_5"],
                        json.dumps(book.get("raw_bids", []), separators=(",", ":")),
                        json.dumps(book.get("raw_asks", []), separators=(",", ":")),
                        timestamp,
                    ),
                )

            recorded_at = _now_iso()
            for trade in _fetch_recent_trades(condition_id):
                conn.execute(
                    """
                    INSERT INTO polymarket_trades (
                        market_id, side, price, size, trade_timestamp, recorded_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_id,
                        trade.get("side") or trade.get("outcome") or "",
                        float(trade.get("price") or 0),
                        float(trade.get("size") or 0),
                        _trade_timestamp(trade.get("timestamp")),
                        recorded_at,
                    ),
                )
            conn.commit()

        return len(snapshots)

    def collect_all(self, db: Database | None = None) -> int:
        """Collect snapshots for every active stored market."""
        active_db = db or self.db
        self.db = active_db
        with active_db.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT market_id, yes_token_id, no_token_id
                FROM markets
                WHERE active = 1
                """
            ).fetchall()

        total = 0
        for row in rows:
            total += self.collect_for_market(
                row["market_id"],
                row["yes_token_id"],
                row["no_token_id"],
            )
        return total

    def start(self, db: Database | None = None, interval_sec: float = 2.0) -> None:
        """Start polling all active markets in a background thread."""
        if self._thread and self._thread.is_alive():
            return

        active_db = db or self.db
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(active_db, interval_sec),
            name="PolymarketCollector",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background collector thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self, db: Database, interval_sec: float) -> None:
        while not self._stop_event.is_set():
            try:
                self.collect_all(db)
            except Exception as exc:
                logger.warning("Polymarket collect_all failed: %s", exc)
            self._stop_event.wait(max(interval_sec, 0.0))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    config = ConfigBTC()
    db = Database(config.db_path)
    db.init_schema()
    collector = PolymarketCollector(db)
    print(f"Snapshots collected: {collector.collect_all(db)}")


if __name__ == "__main__":
    main()
