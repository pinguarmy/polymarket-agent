"""Public Polymarket CLOB market-data helpers."""

from __future__ import annotations

import logging
from typing import Any

import requests


CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "PolymarketAgent/0.1 (clob-snapshot-collector; no-auth)",
})


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _levels(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _price(level: dict[str, Any]) -> float:
    return _as_float(level.get("price"))


def _size(level: dict[str, Any]) -> float:
    return _as_float(level.get("size"))


def _empty_book() -> dict[str, Any]:
    return {
        "best_bid": 0.0,
        "best_ask": 0.0,
        "midpoint": 0.0,
        "spread": 0.0,
        "spread_pct": 0.0,
        "bid_depth_5": 0.0,
        "ask_depth_5": 0.0,
        "last_trade_price": 0.0,
        "raw_bids": [],
        "raw_asks": [],
    }


def fetch_price(token_id: str, side: str) -> float | None:
    """Fetch the public CLOB best price for a token side."""
    try:
        response = _session.get(
            f"{CLOB_API}/price",
            params={"token_id": token_id, "side": side.upper()},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        value = payload.get("price") if isinstance(payload, dict) else payload
        return float(value)
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.warning("CLOB price fetch failed for token=%s side=%s: %s", token_id, side, exc)
        return None


def fetch_last_trade_price(token_id: str) -> float | None:
    """Fetch the most recent public trade price for a token when available."""
    try:
        response = _session.get(
            f"{CLOB_API}/last-trade-price",
            params={"token_id": token_id},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            value = payload.get("price") or payload.get("last_trade_price")
        else:
            value = payload
        return float(value)
    except (requests.RequestException, TypeError, ValueError) as exc:
        logger.debug("CLOB last trade price fetch failed for token=%s: %s", token_id, exc)

    try:
        response = _session.get(
            f"{DATA_API}/trades",
            params={"asset": token_id, "limit": 1},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        trades = payload if isinstance(payload, list) else payload.get("data", [])
        if trades and isinstance(trades[0], dict):
            return float(trades[0].get("price"))
    except (requests.RequestException, AttributeError, TypeError, ValueError, IndexError) as exc:
        logger.debug("Data API last trade price fetch failed for token=%s: %s", token_id, exc)
    return None


def fetch_order_book(token_id: str) -> dict[str, Any] | None:
    """Fetch a token order book and derive top-of-book metrics."""
    try:
        response = _session.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("CLOB order book fetch failed for token=%s: %s", token_id, exc)
        return None

    bids = _levels(payload.get("bids")) if isinstance(payload, dict) else []
    asks = _levels(payload.get("asks")) if isinstance(payload, dict) else []
    if not bids and not asks:
        return _empty_book()

    best_bids = sorted(bids, key=_price, reverse=True)
    best_asks = sorted(asks, key=_price)
    best_bid = _price(best_bids[0]) if best_bids else 0.0
    best_ask = _price(best_asks[0]) if best_asks else 0.0
    midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
    spread = best_ask - best_bid if best_bid and best_ask else 0.0
    spread_pct = (spread / midpoint * 100) if midpoint else 0.0
    last_trade_price = fetch_last_trade_price(token_id) or 0.0

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "midpoint": midpoint,
        "spread": spread,
        "spread_pct": spread_pct,
        "bid_depth_5": sum(_size(level) for level in best_bids[:5]),
        "ask_depth_5": sum(_size(level) for level in best_asks[:5]),
        "last_trade_price": last_trade_price,
        "raw_bids": bids,
        "raw_asks": asks,
    }
