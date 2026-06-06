"""Binance BTC price collection helpers."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from config_btc import ConfigBTC
    from db import Database
except ImportError:  # pragma: no cover - package import path
    from .config_btc import ConfigBTC
    from .db import Database


BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "PolymarketAgent/0.1 (binance-btc-collector; no-auth)",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_btc_price() -> dict[str, Any] | None:
    """Fetch the current Binance BTC/USDT ticker price."""
    config = ConfigBTC()
    try:
        response = _session.get(
            BINANCE_TICKER_URL,
            params={"symbol": config.binance_symbol},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        return {"price": float(payload["price"]), "timestamp": _now_iso()}
    except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
        logger.warning("Binance BTC price fetch failed: %s", exc)
        return None


class BinanceCollector:
    """Background collector that stores Binance BTC ticks."""

    def __init__(self, db: Database):
        self.db = db
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def collect_once(self) -> bool:
        """Fetch one price tick and store it."""
        tick = fetch_btc_price()
        if not tick:
            return False

        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO binance_btc_ticks (price, bid, ask, timestamp)
                    VALUES (?, ?, ?, ?)
                    """,
                    (tick["price"], None, None, tick["timestamp"]),
                )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("Binance BTC tick insert failed: %s", exc)
            return False

    def start(self, interval_sec: float = 1.0) -> None:
        """Start polling Binance in a background thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(interval_sec,),
            name="BinanceCollector",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background collector thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self, interval_sec: float) -> None:
        while not self._stop_event.is_set():
            self.collect_once()
            self._stop_event.wait(max(interval_sec, 0.0))
