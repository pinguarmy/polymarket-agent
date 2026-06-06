import sqlite3
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import market_discovery
from binance_client import fetch_btc_price
from clob_client import fetch_order_book
from polymarket_collector import PolymarketCollector


class InMemoryDatabase:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema = Path(__file__).resolve().parents[1] / "schema.sql"
        self.conn.executescript(schema.read_text(encoding="utf-8"))

    def get_connection(self):
        return self.conn


def _active_token_id(db):
    row = db.get_connection().execute(
        "SELECT yes_token_id FROM markets WHERE active = 1 AND yes_token_id != '' LIMIT 1"
    ).fetchone()
    return row["yes_token_id"] if row else None


def test_binance_price_fetch():
    price = fetch_btc_price()
    if not price:
        pytest.skip("Binance public API unavailable")
    assert price["price"] > 0
    assert price["timestamp"]


def test_clob_book_fetch():
    db = InMemoryDatabase()
    market_discovery.discover_and_store(db)
    token_id = _active_token_id(db)
    if not token_id:
        pytest.skip("No active Polymarket BTC market discovered")

    book = fetch_order_book(token_id)
    if book is None:
        pytest.skip("Polymarket CLOB public API unavailable")

    required = {
        "best_bid",
        "best_ask",
        "midpoint",
        "spread",
        "spread_pct",
        "bid_depth_5",
        "ask_depth_5",
        "last_trade_price",
    }
    assert required.issubset(book)


def test_collector_integration():
    db = InMemoryDatabase()
    market_count = market_discovery.discover_and_store(db)
    if market_count == 0:
        pytest.skip("No active Polymarket BTC markets discovered")

    collector = PolymarketCollector(db)
    snapshots = collector.collect_all(db)
    if snapshots == 0:
        pytest.skip("Polymarket CLOB public API unavailable")

    conn = db.get_connection()
    price_rows = conn.execute("SELECT COUNT(*) AS c FROM polymarket_price_snapshots").fetchone()["c"]
    book_rows = conn.execute("SELECT COUNT(*) AS c FROM polymarket_orderbook_snapshots").fetchone()["c"]
    assert snapshots > 0
    assert price_rows == snapshots
    assert book_rows == snapshots
