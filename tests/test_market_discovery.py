import sqlite3
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import market_discovery


class InMemoryDatabase:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        schema = Path(__file__).resolve().parents[1] / "schema.sql"
        self.conn.executescript(schema.read_text(encoding="utf-8"))

    def get_connection(self):
        return self.conn


def sample_market(**overrides):
    market = {
        "market_id": "12345",
        "slug": "btc-updown-5m-1710000000",
        "question": "Bitcoin Up or Down - 5 Minute",
        "condition_id": "0xcondition",
        "yes_token_id": "111",
        "no_token_id": "222",
        "open_time": "2026-04-30T12:00:00Z",
        "close_time": "2026-04-30T12:05:00Z",
        "volume": 42.5,
        "active": True,
        "outcomePrices": ["0.51", "0.49"],
        "clobTokenIds": ["111", "222"],
    }
    market.update(overrides)
    return market


def test_discover_and_store_smoke(monkeypatch):
    db = InMemoryDatabase()
    monkeypatch.setattr(
        market_discovery.gamma_client,
        "discover_btc_markets",
        lambda: [sample_market()],
    )

    count = market_discovery.discover_and_store(db)

    assert count == 1
    rows = db.get_connection().execute("SELECT * FROM markets").fetchall()
    assert len(rows) == 1


def test_no_markets_handled_gracefully(monkeypatch):
    db = InMemoryDatabase()
    monkeypatch.setattr(market_discovery.gamma_client, "discover_btc_markets", lambda: [])

    count = market_discovery.discover_and_store(db)

    assert count == 0
    rows = db.get_connection().execute("SELECT * FROM markets").fetchall()
    assert rows == []


def test_market_insertion(monkeypatch):
    db = InMemoryDatabase()
    monkeypatch.setattr(
        market_discovery.gamma_client,
        "discover_btc_markets",
        lambda: [sample_market(volume=100)],
    )

    count = market_discovery.discover_and_store(db)

    assert count == 1
    monkeypatch.setattr(
        market_discovery.gamma_client,
        "discover_btc_markets",
        lambda: [sample_market(volume=125)],
    )

    assert market_discovery.discover_and_store(db) == 1
    market = db.get_connection().execute(
        "SELECT slug, question, yes_token_id, no_token_id, volume FROM markets WHERE market_id = ?",
        ("12345",),
    ).fetchone()
    assert market["slug"] == "btc-updown-5m-1710000000"
    assert market["question"] == "Bitcoin Up or Down - 5 Minute"
    assert market["yes_token_id"] == "111"
    assert market["no_token_id"] == "222"
    assert market["volume"] == 125

    mappings = db.get_connection().execute(
        "SELECT token_id, side FROM token_mapping ORDER BY side"
    ).fetchall()
    assert [(row["token_id"], row["side"]) for row in mappings] == [
        ("222", "NO"),
        ("111", "YES"),
    ]
