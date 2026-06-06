"""Polymarket Agent — Phase 1: Read-Only Market Data.

All functions use public REST APIs. No authentication required.
Covers tasks 1.1 through 1.6 from CODING_TASKS.md.

Usage:
    python src/market_data.py --slug will-donald-trump-win-the-2028-presidential-election
    python src/market_data.py --slug <slug> --summary
    python src/market_data.py --slug <slug> --orderbook
    python src/market_data.py --slug <slug> --trades
    python src/market_data.py --slug <slug> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ── API base URLs ────────────────────────────────────────────────────

GAMMA_API  = "https://gamma-api.polymarket.com"
CLOB_API   = "https://clob.polymarket.com"
DATA_API   = "https://data-api.polymarket.com"

# Shared session for connection reuse
_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "PolymarketAgent/0.1 (research; no-automation)",
})


# ── helpers ──────────────────────────────────────────────────────────

def _get(url: str, params: Optional[dict] = None) -> dict | list:
    """GET a JSON endpoint. Raises on non-200 or parse failure."""
    resp = _session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Handle CLOB API wrapped responses
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        return data["data"]
    return data


def _parse_json_field(raw: str | list) -> list:
    """Polymarket double-encodes some fields (JSON string inside JSON).
    Parse them: if already a list, return as-is; if a string, json.loads.

    Examples:
        >>> _parse_json_field('["0.65", "0.35"]')
        ['0.65', '0.35']
        >>> _parse_json_field(["0.65", "0.35"])
        ['0.65', '0.35']
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        return json.loads(raw)
    return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Task 1.1: Market Lookup by Slug ─────────────────────────────────

def get_market_by_slug(slug: str) -> dict | None:
    """Look up a Polymarket market by its slug.

    Uses Gamma API: GET /markets?slug=<slug>

    Args:
        slug: Market slug from the Polymarket URL
              e.g. 'will-donald-trump-win-the-2028-presidential-election'

    Returns:
        dict with keys: question, slug, conditionId, clobTokenIds (parsed),
        outcomePrices (parsed), volume, liquidity, endDate, active, closed
        or None if not found.
    """
    url = f"{GAMMA_API}/markets"
    resp = _session.get(url, params={"slug": slug}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or len(data) == 0:
        return None

    raw = data[0]

    # Parse double-encoded fields
    clob_token_ids = _parse_json_field(raw.get("clobTokenIds", "[]"))
    outcome_prices = _parse_json_field(raw.get("outcomePrices", "[]"))
    outcomes = _parse_json_field(raw.get("outcomes", "[]"))

    return {
        "question":        raw.get("question", ""),
        "slug":            raw.get("slug", slug),
        "condition_id":    raw.get("conditionId", ""),
        "clob_token_ids":  clob_token_ids,
        "yes_token_id":    clob_token_ids[0] if len(clob_token_ids) > 0 else "",
        "no_token_id":     clob_token_ids[1] if len(clob_token_ids) > 1 else "",
        "outcome_prices":  outcome_prices,
        "yes_price":       float(outcome_prices[0]) if len(outcome_prices) > 0 else None,
        "no_price":        float(outcome_prices[1]) if len(outcome_prices) > 1 else None,
        "outcomes":        outcomes,
        "volume":          float(raw.get("volume", 0)),
        "liquidity":       float(raw.get("liquidity", 0)),
        "end_date":        raw.get("endDate", ""),
        "active":          raw.get("active", False),
        "closed":          raw.get("closed", False),
        "description":     raw.get("description", ""),
        "fetched_at":      _now_iso(),
    }


# ── Task 1.2: Order Book ────────────────────────────────────────────

def get_order_book(token_id: str) -> dict | None:
    """Get the full order book for a CLOB token.

    Uses CLOB API: GET /book?token_id=<token_id>

    Returns:
        dict with: asset_id, bids (list of {price, size}), asks (list),
        min_order_size, tick_size, last_trade_price, best_bid, best_ask,
        spread, spread_pct, mid_price
        or None on error.
    """
    url = f"{CLOB_API}/book"
    try:
        data = _get(url, {"token_id": token_id})
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    best_bid = float(bids[0]["price"]) if bids else 0.0
    best_ask = float(asks[0]["price"]) if asks else 1.0
    spread = best_ask - best_bid
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask < 1 else None
    spread_pct = (spread / best_ask * 100) if best_ask > 0 else None

    # Calculate bid/ask depth (total size at best levels)
    bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
    ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])

    return {
        "asset_id":         data.get("asset_id", token_id),
        "bids":             bids,
        "asks":             asks,
        "best_bid":         best_bid,
        "best_ask":         best_ask,
        "spread":           round(spread, 4),
        "spread_pct":       round(spread_pct, 2) if spread_pct else None,
        "mid_price":        round(mid, 4) if mid else None,
        "bid_depth_5":      bid_depth,
        "ask_depth_5":      ask_depth,
        "min_order_size":   data.get("min_order_size", "5"),
        "tick_size":        data.get("tick_size", "0.01"),
        "last_trade_price": data.get("last_trade_price"),
    }


# ── Task 1.3: Price, Midpoint, Spread ───────────────────────────────

def get_price(token_id: str, side: str = "buy") -> float | None:
    """Get the current best price for buying or selling a token.

    Uses CLOB API: GET /price?token_id=<id>&side=buy|sell
    """
    url = f"{CLOB_API}/price"
    try:
        data = _get(url, {"token_id": token_id, "side": side})
        if isinstance(data, dict) and "price" in data:
            return float(data["price"])
        return None
    except Exception:
        return None


def get_midpoint(token_id: str) -> float | None:
    """Get the midpoint price for a token.

    Uses CLOB API: GET /midpoint?token_id=<id>
    """
    url = f"{CLOB_API}/midpoint"
    try:
        data = _get(url, {"token_id": token_id})
        if isinstance(data, dict) and "mid" in data:
            return float(data["mid"])
        return None
    except Exception:
        return None


def get_spread(token_id: str) -> float | None:
    """Get the bid-ask spread for a token.

    Uses CLOB API: GET /spread?token_id=<id>
    """
    url = f"{CLOB_API}/spread"
    try:
        data = _get(url, {"token_id": token_id})
        if isinstance(data, dict) and "spread" in data:
            return float(data["spread"])
        return None
    except Exception:
        return None


# ── Task 1.4: Recent Trades ─────────────────────────────────────────

def get_recent_trades(condition_id: str, limit: int = 20) -> list[dict]:
    """Get recent trades for a market by condition ID.

    Uses Data API: GET /trades?market=<condition_id>&limit=N

    Returns:
        list of dicts with: side, size, price, timestamp, outcome, slug
    """
    url = f"{DATA_API}/trades"
    try:
        data = _get(url, {"market": condition_id, "limit": limit})
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def get_price_history(condition_id: str,
                      interval: str = "1d",
                      fidelity: int = 50) -> list[dict]:
    """Get price history for a market.

    Uses CLOB API: GET /prices-history?market=<condition_id>&interval=<>&fidelity=<>

    Args:
        condition_id: Market condition ID (with 0x prefix)
        interval: all, 1d, 1w, 1m, 3m, 6m, 1y
        fidelity: number of data points

    Returns:
        list of {t: unix_timestamp, p: price}
    """
    url = f"{CLOB_API}/prices-history"
    try:
        data = _get(url, {
            "market": condition_id,
            "interval": interval,
            "fidelity": fidelity,
        })
        if isinstance(data, dict) and "history" in data:
            return data["history"]
        return []
    except Exception:
        return []


# ── Task 1.5: Market Summary ────────────────────────────────────────

def get_market_summary(slug: str) -> dict | None:
    """Generate a full market summary combining all read-only data.

    Combines market lookup, order book (both Yes and No tokens), recent
    trades, and price history into a single dict.

    Returns None if market not found.
    """
    market = get_market_by_slug(slug)
    if market is None:
        return None

    condition_id = market["condition_id"]
    yes_token = market["yes_token_id"]
    no_token = market["no_token_id"]

    # Order books for both tokens
    yes_book = get_order_book(yes_token) if yes_token else None
    no_book = get_order_book(no_token) if no_token else None

    # Recent trades
    trades = get_recent_trades(condition_id, limit=10)

    # Recent price changes (last hour)
    price_history = get_price_history(condition_id, interval="1d", fidelity=24)

    # Calculate price changes
    price_change_1h = None
    price_change_24h = None
    if price_history and len(price_history) >= 2:
        current = float(price_history[-1]["p"])
        if len(price_history) >= 2:
            prev_1h = float(price_history[-2]["p"])
            price_change_1h = round(current - prev_1h, 4)
        first = float(price_history[0]["p"])
        price_change_24h = round(current - first, 4)

    summary = {
        # ── identity ──
        "market_name":        market["question"],
        "market_slug":        slug,
        "condition_id":       condition_id,
        "description":        market["description"],
        "active":             market["active"],
        "closed":             market["closed"],
        "end_date":           market["end_date"],

        # ── tokens ──
        "yes_token_id":       yes_token,
        "no_token_id":        no_token,
        "yes_price":          market["yes_price"],
        "no_price":           market["no_price"],

        # ── order book stats ──
        "yes_best_bid":       yes_book["best_bid"] if yes_book else None,
        "yes_best_ask":       yes_book["best_ask"] if yes_book else None,
        "yes_spread":         yes_book["spread"] if yes_book else None,
        "yes_spread_pct":     yes_book["spread_pct"] if yes_book else None,
        "yes_mid":            yes_book["mid_price"] if yes_book else None,
        "yes_bid_depth":      yes_book["bid_depth_5"] if yes_book else None,
        "yes_ask_depth":      yes_book["ask_depth_5"] if yes_book else None,

        "no_best_bid":        no_book["best_bid"] if no_book else None,
        "no_best_ask":        no_book["best_ask"] if no_book else None,
        "no_spread":          no_book["spread"] if no_book else None,
        "no_spread_pct":      no_book["spread_pct"] if no_book else None,
        "no_mid":             no_book["mid_price"] if no_book else None,
        "no_bid_depth":       no_book["bid_depth_5"] if no_book else None,
        "no_ask_depth":       no_book["ask_depth_5"] if no_book else None,

        # ── volume & liquidity ──
        "volume":             market["volume"],
        "liquidity":          market["liquidity"],
        "recent_trades":      trades,
        "recent_trade_count": len(trades),

        # ── price changes ──
        "price_change_1h":    price_change_1h,
        "price_change_24h":   price_change_24h,

        # ── meta ──
        "data_timestamp":     _now_iso(),
    }

    return summary


# ── CLI ──────────────────────────────────────────────────────────────

def _format_price(p: float | None) -> str:
    """Format a price as a dollar amount with 4 decimal places."""
    if p is None:
        return "N/A"
    return f"${p:.4f}"


def _print_summary(summary: dict) -> None:
    """Pretty-print a market summary to stdout."""
    print()
    print("=" * 72)
    print(f"  {summary['market_name']}")
    print("=" * 72)
    print()
    print(f"  Slug:         {summary['market_slug']}")
    print(f"  Condition ID: {summary['condition_id']}")
    print(f"  Status:       {'ACTIVE' if summary['active'] else 'INACTIVE'}"
          f"{' (CLOSED)' if summary['closed'] else ''}")
    if summary["end_date"]:
        print(f"  Ends:         {summary['end_date']}")
    print()

    # Prices
    print("  ── Prices ──")
    print(f"  YES:  {_format_price(summary['yes_price'])}  "
          f"(bid {_format_price(summary['yes_best_bid'])} / "
          f"ask {_format_price(summary['yes_best_ask'])}, "
          f"spread {summary['yes_spread_pct']}%)")
    print(f"  NO:   {_format_price(summary['no_price'])}  "
          f"(bid {_format_price(summary['no_best_bid'])} / "
          f"ask {_format_price(summary['no_best_ask'])}, "
          f"spread {summary['no_spread_pct']}%)")

    # Volume & liquidity
    print()
    print("  ── Volume & Liquidity ──")
    print(f"  Volume:       ${summary['volume']:,.0f}")
    print(f"  Liquidity:    ${summary['liquidity']:,.0f}")
    print(f"  Recent trades: {summary['recent_trade_count']}")

    # Price changes
    if summary["price_change_1h"] is not None:
        chg = summary["price_change_1h"]
        direction = "+" if chg >= 0 else ""
        print(f"  1h change:    {direction}{chg:.4f}")
    if summary["price_change_24h"] is not None:
        chg = summary["price_change_24h"]
        direction = "+" if chg >= 0 else ""
        print(f"  24h change:   {direction}{chg:.4f}")

    # Recent trades
    if summary["recent_trades"]:
        print()
        print("  ── Recent Trades ──")
        for t in summary["recent_trades"][:5]:
            side = t.get("side", "?")
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
            print(f"  {side:4s} {size:8.0f} @ ${price:.4f}")

    print()
    print(f"  Data timestamp: {summary['data_timestamp']}")
    print()


def _print_orderbook(book: dict, label: str = "") -> None:
    """Pretty-print an order book."""
    print()
    if label:
        print(f"  ── {label} Order Book ──")
    print(f"  Best Bid:  {_format_price(book['best_bid'])}  "
          f"(depth 5: {book['bid_depth_5']:.0f})")
    print(f"  Best Ask:  {_format_price(book['best_ask'])}  "
          f"(depth 5: {book['ask_depth_5']:.0f})")
    print(f"  Spread:    {book['spread']:.4f}  ({book['spread_pct']}%)")
    print(f"  Mid Price: {_format_price(book['mid_price'])}")
    print(f"  Tick Size: {book['tick_size']}")
    print()
    print("  Bids (top 5):")
    for bid in book["bids"][:5]:
        print(f"    ${float(bid['price']):.4f}  x {float(bid['size']):.0f}")
    print("  Asks (top 5):")
    for ask in book["asks"][:5]:
        print(f"    ${float(ask['price']):.4f}  x {float(ask['size']):.0f}")
    print()


def main():
    parser = argparse.ArgumentParser(
        prog="market_data",
        description="Polymarket Agent — Read-Only Market Data",
    )
    parser.add_argument(
        "--slug", required=True,
        help="Market slug (e.g. will-donald-trump-win-the-2028-presidential-election)"
    )
    parser.add_argument("--summary", action="store_true",
                        help="Show full market summary (default)")
    parser.add_argument("--orderbook", action="store_true",
                        help="Show order book for both Yes and No tokens")
    parser.add_argument("--trades", action="store_true",
                        help="Show recent trades")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON (machine-readable)")
    parser.add_argument("--history", action="store_true",
                        help="Show price history (24h)")

    args = parser.parse_args()

    slug = args.slug.strip()

    # Determine mode: summary is default if nothing specified
    show_summary = args.summary or not (args.orderbook or args.trades or args.history)

    # JSON output: dump the full summary
    if args.json:
        summary = get_market_summary(slug)
        if summary is None:
            print(f"Error: market not found for slug '{slug}'", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return

    # Show price history
    if args.history:
        market = get_market_by_slug(slug)
        if market is None:
            print(f"Error: market not found for slug '{slug}'", file=sys.stderr)
            sys.exit(1)
        history = get_price_history(market["condition_id"], interval="1d", fidelity=24)
        if not history:
            print("No price history available.")
        else:
            print()
            print(f"  Price History (24h) — {market['question']}")
            print("  " + "-" * 40)
            for point in history:
                ts = datetime.fromtimestamp(point["t"], tz=timezone.utc)
                print(f"  {ts.strftime('%H:%M')}  ${float(point['p']):.4f}")
            print()

    # Show order book
    if args.orderbook:
        market = get_market_by_slug(slug)
        if market is None:
            print(f"Error: market not found for slug '{slug}'", file=sys.stderr)
            sys.exit(1)
        yes_book = get_order_book(market["yes_token_id"])
        no_book = get_order_book(market["no_token_id"])
        if yes_book:
            _print_orderbook(yes_book, "YES")
        if no_book:
            _print_orderbook(no_book, "NO")

    # Show recent trades
    if args.trades:
        market = get_market_by_slug(slug)
        if market is None:
            print(f"Error: market not found for slug '{slug}'", file=sys.stderr)
            sys.exit(1)
        trades = get_recent_trades(market["condition_id"], limit=20)
        if not trades:
            print("No recent trades found.")
        else:
            print()
            print(f"  Recent Trades — {market['question']}")
            print("  " + "-" * 50)
            for t in trades:
                print(f"  {t.get('side', '?'):4s}  "
                      f"{float(t.get('size', 0)):8.0f} @ "
                      f"${float(t.get('price', 0)):.4f}  "
                      f"({t.get('timestamp', '?')})")
            print()

    # Show summary
    if show_summary:
        summary = get_market_summary(slug)
        if summary is None:
            print(f"Error: market not found for slug '{slug}'", file=sys.stderr)
            print("Tip: copy the slug from a Polymarket URL, e.g.")
            print("  polymarket.com/event/will-bitcoin-hit-100k-before-june")
            print("  slug = 'will-bitcoin-hit-100k-before-june'")
            sys.exit(1)
        _print_summary(summary)


if __name__ == "__main__":
    main()
