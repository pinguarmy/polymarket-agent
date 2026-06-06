"""Discover and persist Polymarket BTC 5-minute markets."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

try:
    from config_btc import ConfigBTC
    from db import Database
    import gamma_client
except ImportError:  # pragma: no cover - package import path
    from .config_btc import ConfigBTC
    from .db import Database
    from . import gamma_client


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def discover_and_store(db: Database) -> int:
    """Discover BTC 5-minute markets and upsert markets/token mappings."""
    try:
        markets = gamma_client.discover_btc_markets()
    except Exception as exc:
        logger.warning("BTC market discovery failed: %s", exc)
        return 0

    if not markets:
        return 0

    stored = 0
    created_at = _now_iso()
    now_utc = datetime.now(timezone.utc)
    with db.get_connection() as conn:
        for market in markets:
            market_id = str(market.get("market_id") or "")
            slug = market.get("slug") or ""
            if not market_id or not slug:
                logger.warning("Skipping discovered market without id/slug: %r", market)
                continue

            yes_token_id = str(market.get("yes_token_id") or "")
            no_token_id = str(market.get("no_token_id") or "")
            close_time_str = market.get("close_time") or ""
            
            truly_active = False
            if close_time_str:
                try:
                    ct = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    truly_active = ct > now_utc
                except (ValueError, TypeError):
                    pass
            
            conn.execute(
                """
                INSERT INTO markets (
                    market_id, slug, question, condition_id, yes_token_id, no_token_id,
                    open_time, close_time, resolution, volume, active, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    slug = excluded.slug,
                    question = excluded.question,
                    condition_id = excluded.condition_id,
                    yes_token_id = excluded.yes_token_id,
                    no_token_id = excluded.no_token_id,
                    open_time = excluded.open_time,
                    close_time = excluded.close_time,
                    volume = excluded.volume,
                    active = excluded.active
                """,
                (
                    market_id,
                    slug,
                    market.get("question") or "",
                    market.get("condition_id") or "",
                    yes_token_id,
                    no_token_id,
                    market.get("open_time") or "",
                    market.get("close_time") or "",
                    None,
                    float(market.get("volume") or 0),
                    1 if truly_active else 0,
                    created_at,
                ),
            )

            for side, token_id in (("YES", yes_token_id), ("NO", no_token_id)):
                if not token_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO token_mapping (token_id, market_id, side)
                    VALUES (?, ?, ?)
                    ON CONFLICT(token_id) DO UPDATE SET
                        market_id = excluded.market_id,
                        side = excluded.side
                    """,
                    (token_id, market_id, side),
                )
            stored += 1
        conn.commit()

    return stored


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    config = ConfigBTC()
    db = Database(config.db_path)
    db.init_schema()
    count = discover_and_store(db)
    print(f"Markets found: {count}")


if __name__ == "__main__":
    main()
