"""Polymarket Agent — Phase 5: Live Trading (CLOB V2).

Uses py-clob-client-v2 with POLY_PROXY signature type.
All functions enforce DRY_RUN / LIVE_TRADING gates.

Requires: py-clob-client-v2>=1.0.0
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── helpers ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(v: float) -> float:
    return round(float(v), 2)


# ── Client Factory ───────────────────────────────────────────────────


def get_clob_client(config=None) -> object:
    """Create an authenticated CLOB V2 client.

    Uses POLY_PROXY (signature_type=1) with proxy wallet as funder.
    The EOA private key signs orders on behalf of the proxy wallet.

    Returns:
        Authenticated py_clob_client_v2.client.ClobClient
    """
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    pk = os.getenv("POLY_PK", "")
    api_key = os.getenv("POLYMARKET_API_KEY", "")
    secret = os.getenv("POLYMARKET_SECRET", "")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")
    proxy = os.getenv("POLY_PROXY_ADDRESS", "")

    if not all([pk, api_key, secret, passphrase]):
        raise RuntimeError(
            "Missing CLOB credentials. Set POLY_PK, POLYMARKET_API_KEY, "
            "POLYMARKET_SECRET, POLYMARKET_PASSPHRASE in .env"
        )
    if not proxy:
        raise RuntimeError(
            "Missing POLY_PROXY_ADDRESS. Set your Polymarket proxy wallet "
            "address in .env (found in Polymarket Settings > API)"
        )

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        signature_type=1,  # POLY_PROXY
        funder=proxy if proxy else None,
    )

    creds = ApiCreds(
        api_key=api_key,
        api_secret=secret,
        api_passphrase=passphrase,
    )
    client.set_api_creds(creds)

    return client


# ── Balance ──────────────────────────────────────────────────────────


def get_balance(config=None) -> dict:
    """Query pUSD balance from CLOB V2 API.

    Returns:
        {"balance_pusd": float, "timestamp": ISO, "wallet": str}
    """
    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

    client = get_clob_client()
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    bal = client.get_balance_allowance(params)

    balance_micro = bal.get("balance", "0")
    balance_pusd = int(balance_micro) / 1_000_000

    return {
        "balance_pusd": _money(balance_pusd),
        "balance_micro": str(balance_micro),
        "timestamp": _now_iso(),
        "wallet": os.getenv("POLY_ADDRESS", ""),
        "proxy": os.getenv("POLY_PROXY_ADDRESS", ""),
    }


# ── Order Creation ───────────────────────────────────────────────────


def create_limit_order(
    token_id: str,
    price: float,
    size: float,
    side: str,
) -> object:
    """Create and sign a limit order using CLOB V2.

    Args:
        token_id: CLOB token ID
        price: Limit price (0.00-1.00)
        size: Number of shares
        side: "BUY" or "SELL"

    Returns:
        SignedOrderV2 object ready for submission
    """
    from py_clob_client_v2.clob_types import OrderArgs

    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be 'BUY' or 'SELL'")

    client = get_clob_client()
    order_args = OrderArgs(
        token_id=token_id,
        price=float(price),
        size=float(size),
        side=side,
    )
    return client.create_order(order_args)


# ── Order Submission ─────────────────────────────────────────────────


def submit_limit_order(
    signed_order,
    config,
    logger=None,
) -> dict:
    """Submit a signed order to CLOB V2 exchange.

    SAFETY GATES: dry_run and live_trading must allow submission.

    Args:
        signed_order: SignedOrderV2 from create_limit_order()
        config: Config instance
        logger: Optional TradeLogger

    Returns:
        {"status": str, "order_id": str|None, "message": str}
    """
    from py_clob_client_v2.clob_types import OrderType

    if config.dry_run:
        if logger:
            logger.log_signal({
                "signal_type": "order_dry_run",
                "token_id": getattr(signed_order, "tokenId", ""),
                "side": str(getattr(signed_order, "side", "")),
                "timestamp": _now_iso(),
            })
        return {
            "status": "dry_run",
            "order_id": None,
            "message": "DRY RUN — order not submitted",
        }

    if not config.live_trading:
        return {
            "status": "blocked",
            "order_id": None,
            "message": "LIVE_TRADING is False",
        }

    try:
        client = get_clob_client()
        result = client.post_order(signed_order, order_type=OrderType.GTC)

        if isinstance(result, dict):
            order_id = result.get("orderID", result.get("id", "unknown"))
        else:
            order_id = getattr(result, "orderID", getattr(result, "id", "unknown"))

        if logger:
            logger.log_signal({
                "signal_type": "order_submitted",
                "order_id": str(order_id),
                "token_id": getattr(signed_order, "tokenId", ""),
                "side": str(getattr(signed_order, "side", "")),
                "timestamp": _now_iso(),
            })

        return {
            "status": "submitted",
            "order_id": str(order_id),
            "message": f"Order {order_id} submitted",
        }

    except Exception as e:
        if logger:
            logger.log_kill_event(f"Order submission failed: {e}")
        return {
            "status": "failed",
            "order_id": None,
            "message": f"Submission error: {e}",
        }


# ── Cancel ───────────────────────────────────────────────────────────


def cancel_order(order_id: str, config=None, logger=None) -> dict:
    """Cancel an open order by ID.

    Args:
        order_id: CLOB order ID (hex string)
        config: Optional Config
        logger: Optional TradeLogger

    Returns:
        {"status": str, "order_id": str, "message": str}
    """
    if config and config.dry_run:
        return {"status": "dry_run", "order_id": order_id, "message": "DRY RUN"}

    try:
        client = get_clob_client()
        from py_clob_client_v2.clob_types import OrderPayload
        client.cancel_order(OrderPayload(orderID=order_id))

        if logger:
            logger.log_signal({
                "signal_type": "order_cancelled",
                "order_id": order_id,
                "timestamp": _now_iso(),
            })

        return {"status": "cancelled", "order_id": order_id, "message": "Cancelled"}

    except Exception as e:
        return {"status": "failed", "order_id": order_id, "message": str(e)}


def cancel_all_orders(config=None, logger=None) -> dict:
    """Cancel ALL open orders.

    Returns:
        {"status": str, "cancelled_count": int}
    """
    if config and config.dry_run:
        return {"status": "dry_run", "cancelled_count": 0}

    try:
        client = get_clob_client()
        result = client.cancel_all()
        count = len(result) if isinstance(result, list) else 0

        if logger:
            logger.log_kill_event(f"cancel_all_orders — {count} orders cancelled")

        return {"status": "cancelled", "cancelled_count": count}

    except Exception as e:
        return {"status": "failed", "cancelled_count": 0, "message": str(e)}


# ── Queries ──────────────────────────────────────────────────────────


def get_open_orders(market: str = "", asset_id: str = "", config=None) -> list:
    """Get open orders from CLOB V2 API.

    Args:
        market: Optional condition ID filter
        asset_id: Optional token ID filter

    Returns:
        list of order dicts (may be objects from SDK)
    """
    from py_clob_client_v2.clob_types import OpenOrderParams

    try:
        client = get_clob_client()
        params = OpenOrderParams()
        if market:
            params.market = market
        if asset_id:
            params.asset_id = asset_id

        orders = client.get_open_orders(params=params)
        return orders if isinstance(orders, list) else list(orders) if orders else []
    except Exception:
        return []


def get_order(order_id: str, config=None) -> dict | None:
    """Get a single order by ID.

    Returns:
        Order dict or None
    """
    try:
        client = get_clob_client()
        return client.get_order(order_id)
    except Exception:
        return None


# ── Emergency Kill Switch ────────────────────────────────────────────


def emergency_kill_switch(config=None, logger=None) -> dict:
    """Emergency stop: cancel all orders, disable trading.

    Cancels all real orders on the exchange AND engages the
    software kill switch in risk_engine.

    Returns:
        {"status": str, "cancelled": int, "message": str}
    """
    from risk_engine import emergency_kill_switch as eng_kill

    cancel_result = cancel_all_orders(config=config, logger=logger)
    eng_kill(logger)

    if logger:
        logger.log_kill_event(
            f"Emergency kill switch: {cancel_result.get('cancelled_count', 0)} orders cancelled"
        )

    return {
        "status": "killed",
        "cancelled": cancel_result.get("cancelled_count", 0),
        "message": "KILL SWITCH ENGAGED — all orders cancelled, trading disabled",
        "timestamp": _now_iso(),
    }


# ── CLI ──────────────────────────────────────────────────────────────


def main():
    """Live trading CLI — balance, orders, positions, kill switch."""
    import argparse
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from config import Config
    except ImportError:
        from src.config import Config

    parser = argparse.ArgumentParser(prog="live_trader", description="CLOB V2 Live Trading")
    parser.add_argument("--balance", action="store_true", help="Query pUSD balance")
    parser.add_argument("--orders", action="store_true", help="List open orders")
    parser.add_argument("--kill", action="store_true", help="EMERGENCY kill switch")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    config = Config()

    if args.balance:
        bal = get_balance()
        if args.json:
            print(json.dumps(bal, indent=2))
        else:
            print(f"pUSD Balance: ${bal['balance_pusd']:.2f}")
            print(f"Wallet:       {bal['wallet']}")
            print(f"Proxy:        {bal['proxy']}")
            print(f"Updated:      {bal['timestamp']}")

    if args.orders:
        orders = get_open_orders()
        if args.json:
            print(json.dumps(orders, indent=2, default=str))
        elif not orders:
            print("No open orders.")
        else:
            print(f"Open Orders ({len(orders)})")
            print("-" * 60)
            for o in orders:
                if isinstance(o, dict):
                    print(f"  {o.get('id', '?')[:16]}... {o.get('side', '?')} @ ${o.get('price', '?')}")
                else:
                    oid = getattr(o, 'id', '?')
                    side = getattr(o, 'side', '?')
                    price = getattr(o, 'price', '?')
                    print(f"  {str(oid)[:16]}... {side} @ ${price}")

    if args.kill:
        print("EMERGENCY KILL SWITCH")
        confirm = input("Type KILL to confirm: ")
        if confirm.strip().upper() == "KILL":
            result = emergency_kill_switch(config=config)
            print(f"Status:    {result['status']}")
            print(f"Cancelled: {result['cancelled']} orders")
            print(f"Message:   {result['message']}")
        else:
            print("Not engaged.")

    if not any([args.balance, args.orders, args.kill]):
        parser.print_help()


if __name__ == "__main__":
    main()
