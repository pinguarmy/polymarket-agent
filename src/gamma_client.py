"""Gamma API discovery helpers for Polymarket BTC 5-minute markets."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

try:
    from config_btc import ConfigBTC
except ImportError:  # pragma: no cover - package import path
    from .config_btc import ConfigBTC


GAMMA_API = "https://gamma-api.polymarket.com"
BTC_5M_SLUG_RE = re.compile(r"^btc-updown-5m-\d+$")

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "PolymarketAgent/0.1 (market-discovery; no-auth)",
})


def _get_json(path: str, params: dict[str, Any]) -> Any:
    """Fetch a Gamma endpoint and return decoded JSON."""
    response = _session.get(f"{GAMMA_API}{path}", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _parse_json_field(value: Any) -> list[Any]:
    """Parse Gamma fields that may be lists or JSON strings of JSON strings."""
    parsed = value
    for _ in range(3):
        if isinstance(parsed, list):
            return parsed
        if not isinstance(parsed, str) or not parsed.strip():
            return []
        try:
            parsed = json.loads(parsed)
        except (TypeError, json.JSONDecodeError):
            logger.warning("Could not parse Gamma JSON field: %r", value)
            return []
    return parsed if isinstance(parsed, list) else []


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _extract_markets(payload: Any) -> list[dict[str, Any]]:
    """Extract market objects from Gamma list, search, or nested event payloads."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    markets: list[dict[str, Any]] = []
    direct = payload.get("markets")
    if isinstance(direct, list):
        markets.extend(item for item in direct if isinstance(item, dict))

    for event in payload.get("events", []):
        if not isinstance(event, dict):
            continue
        event_markets = event.get("markets")
        if isinstance(event_markets, list):
            markets.extend(item for item in event_markets if isinstance(item, dict))

    data = payload.get("data")
    if data is not payload:
        markets.extend(_extract_markets(data))

    return markets


def _normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    slug = str(raw.get("slug") or "")
    if not BTC_5M_SLUG_RE.match(slug):
        return None

    clob_token_ids = _parse_json_field(raw.get("clobTokenIds"))
    outcome_prices = _parse_json_field(raw.get("outcomePrices"))

    market_id = raw.get("id")
    if market_id is None:
        logger.warning("Skipping BTC 5-minute market without Gamma id: %s", slug)
        return None

    return {
        "market_id": str(market_id),
        "slug": slug,
        "question": raw.get("question") or raw.get("title") or "",
        "condition_id": raw.get("conditionId") or raw.get("condition_id") or "",
        "yes_token_id": str(clob_token_ids[0]) if len(clob_token_ids) > 0 else "",
        "no_token_id": str(clob_token_ids[1]) if len(clob_token_ids) > 1 else "",
        "open_time": raw.get("startDate") or "",
        "close_time": raw.get("endDate") or "",
        "volume": _as_float(raw.get("volume")),
        "active": bool(raw.get("active", False)),
        "outcomePrices": outcome_prices,
        "clobTokenIds": clob_token_ids,
    }


def discover_btc_markets() -> list[dict]:
    """Discover active Polymarket BTC 5-minute Up/Down markets via Gamma.

    Uses public Gamma endpoints plus timestamp-based slug scanning
    for the current and recent 5-minute windows.
    """
    config = ConfigBTC()
    discovered: dict[str, dict[str, Any]] = {}

    # Strategy 1: Search for BTC updown markets
    search_queries = ["btc-updown-5m", "bitcoin up or down"]
    for query in search_queries:
        try:
            payload = _get_json("/public-search", {"q": query})
        except Exception:
            continue
        for raw in _extract_markets(payload):
            market = _normalize_market(raw)
            if market and market["active"]:
                discovered[market["slug"]] = market

    # Strategy 2: Scan recent 5-minute timestamps directly
    import time
    now = int(time.time())
    window_start = now - now % 300  # align to 5-min boundary
    # Check current window and up to 1 hour back (12 markets)
    for ts in range(window_start, window_start - 3600, -300):
        slug = f"btc-updown-5m-{ts}"
        if slug in discovered:
            continue
        try:
            payload = _get_json("/markets", {"slug": slug})
            for raw in _extract_markets(payload):
                market = _normalize_market(raw)
                if market is not None:  # accept all
                    discovered[slug] = market
        except Exception:
            continue

    markets = sorted(discovered.values(), key=lambda m: m.get("close_time") or "")
    if config.max_markets > 0:
        return markets[: config.max_markets]
    return markets
