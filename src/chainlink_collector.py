"""Polymarket RTDS Chainlink BTC/USD Data Stream collector.

Connects to Polymarket's Real-Time Data Stream (RTDS) WebSocket,
subscribes to crypto_prices_chainlink / btc/usd, and records
Chainlink oracle prices into chainlink_btc_ticks table.

Purpose: Settlement truth source for BTC Up/Down 5-min markets.
Not a trading signal source — use Binance WS for that.

RTDS:   wss://ws-live-data.polymarket.com
Topic:  crypto_prices_chainlink
Symbol: btc/usd

Usage:
    collector = ChainlinkCollector(db)
    collector.start()         # non-blocking, daemon thread
    collector.stop()          # graceful shutdown
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import websocket
except ImportError:
    import os
    os.system("pip3 install websocket-client -q")
    import websocket

RTDS_URL = "wss://ws-live-data.polymarket.com"
SUBSCRIBE_MSG = {
    "action": "subscribe",
    "subscriptions": [
        {
            "topic": "crypto_prices_chainlink",
            "type": "*",
            "filters": '{"symbol":"btc/usd"}',
        }
    ],
}


class ChainlinkCollector:
    """Collect Chainlink BTC/USD oracle prices from Polymarket RTDS."""

    def __init__(self, db, source_name: str = "polymarket_rtds_chainlink"):
        self.db = db
        self.source = source_name
        self.ws: Optional[websocket.WebSocketApp] = None
        self.running = True
        self._thread: Optional[threading.Thread] = None
        self._on_message_cb = None  # optional callback for testing
        self._ws_fail_count = 0

        # Stats
        self.total_received = 0
        self.last_value = None
        self.last_ts_ms = 0
        self.latencies = []

    def set_on_message(self, cb):
        """Register a callback for each received message (for testing)."""
        self._on_message_cb = cb

    def _on_open(self, ws):
        subscribe = json.dumps(SUBSCRIBE_MSG)
        ws.send(subscribe)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        self.total_received += 1

        # Extract value — try multiple known field paths
        value_raw = self._extract_value(data)
        source_ts_ms = self._extract_timestamp(data)
        topic = data.get("topic") or data.get("type", "")
        msg_type = data.get("message_type") or data.get("event_type", "")

        # Normalize: RTDS value is ALREADY a USD float (e.g. 78416.50)
        # Only divide by 1e8 if value is a suspiciously large int (> 1e9)
        value_normalized = None
        if value_raw is not None:
            if isinstance(value_raw, (int, float)) and value_raw > 1_000_000_000:
                value_normalized = value_raw / 100_000_000.0
            else:
                value_normalized = value_raw

        received_at_ms = int(time.time() * 1000)
        latency = (received_at_ms - source_ts_ms) if source_ts_ms else None
        self.last_value = value_normalized or value_raw
        self.last_ts_ms = source_ts_ms or 0
        if latency is not None:
            self.latencies.append(latency)

        # Write to DB
        self._write_tick(
            source=self.source,
            symbol="btc/usd",
            value_raw=value_raw,
            value_normalized=value_normalized,
            source_timestamp_ms=source_ts_ms,
            received_at_ms=received_at_ms,
            latency_ms=latency,
            topic=topic,
            message_type=msg_type,
            raw_payload=message,
        )

        # Callback for testing
        if self._on_message_cb:
            self._on_message_cb(data, value_normalized or value_raw)

    def _extract_value(self, data) -> Optional[float]:
        """Try multiple possible field paths for the oracle price.

        RTDS sends two formats:
        1. Initial subscribe response: payload.data[0...N].value (array of historical)
        2. Updates: payload.value (single value, already float USD price)
        
        Also handle payload.full_accuracy_value (string, high-precision int).
        """
        # Format 2: single value in payload (update messages)
        candidates = [
            data.get("payload", {}).get("value"),
            data.get("payload", {}).get("price"),
            data.get("value"),
            data.get("price"),
            data.get("data", {}).get("value"),
            data.get("data", {}).get("price"),
        ]
        for v in candidates:
            if v is not None:
                try:
                    return float(v)
                except (ValueError, TypeError):
                    continue

        # Format 1: array in payload.data (subscribe response with history)
        payload_data = data.get("payload", {}).get("data")
        if isinstance(payload_data, list) and len(payload_data) > 0:
            # Return the LAST entry's value (most recent)
            last_entry = payload_data[-1]
            last_val = last_entry.get("value")
            if last_val is not None:
                try:
                    return float(last_val)
                except (ValueError, TypeError):
                    pass

        # Full accuracy fallback (string like '78411240915500000000000')
        fa = data.get("payload", {}).get("full_accuracy_value")
        if fa:
            try:
                # This is a high-precision int string. Divide by 1e8 or 1e12 depending on length
                fa_int = int(fa)
                if fa_int > 1e18:
                    return fa_int / 1e15
                elif fa_int > 1e12:
                    return fa_int / 1e10
                else:
                    return float(fa_int)
            except (ValueError, TypeError):
                pass

        return None

    def _extract_timestamp(self, data) -> Optional[int]:
        """Try multiple possible field paths for the source timestamp."""
        candidates = [
            data.get("payload", {}).get("timestamp"),
            data.get("payload", {}).get("timestamp_ms"),
            data.get("timestamp"),
            data.get("timestamp_ms"),
            data.get("data", {}).get("timestamp"),
            data.get("data", {}).get("timestamp_ms"),
            data.get("payload", {}).get("updated_at"),
            data.get("payload", {}).get("source_timestamp"),
        ]
        for v in candidates:
            if v is not None:
                try:
                    return int(v)
                except (ValueError, TypeError):
                    continue
        return None

    def _on_error(self, ws, error):
        self._ws_fail_count += 1
        print(f"  Chainlink RTDS error (#{self._ws_fail_count}): {error}")

    def _on_close(self, ws, status, msg):
        if self.running and self._ws_fail_count < 10:
            backoff = min(30, 3 * (1 << self._ws_fail_count))
            print(f"  Chainlink RTDS disconnected, reconnecting in {backoff}s...")
            time.sleep(backoff)
            self._ws_fail_count += 1
            self._connect()

    def _connect(self):
        """Create WebSocket connection (internal)."""
        self.ws = websocket.WebSocketApp(
            RTDS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(
            target=lambda: self.ws.run_forever(reconnect=3, ping_interval=30, ping_timeout=10),
            daemon=True,
        )
        self._thread.start()

    def start(self):
        """Start the collector (non-blocking)."""
        self._connect()
        print(f"Chainlink RTDS collector started: {RTDS_URL}")

    def stop(self):
        """Graceful shutdown."""
        self.running = False
        if self.ws:
            self.ws.close()

    def _write_tick(
        self,
        source: str,
        symbol: str,
        value_raw: Optional[float],
        value_normalized: Optional[float],
        source_timestamp_ms: Optional[int],
        received_at_ms: Optional[int],
        latency_ms: Optional[int],
        topic: str,
        message_type: str,
        raw_payload: str,
    ):
        """Insert a tick into chainlink_btc_ticks."""
        try:
            with self.db.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO chainlink_btc_ticks
                        (source, symbol, value_raw, value_normalized,
                         source_timestamp_ms, received_at_ms, latency_ms,
                         topic, message_type, raw_payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        symbol,
                        value_raw,
                        value_normalized,
                        source_timestamp_ms,
                        received_at_ms,
                        latency_ms,
                        topic,
                        message_type,
                        raw_payload,
                    ),
                )
                conn.commit()
        except Exception as e:
            print(f"  Chainlink DB write error: {e}")

    def stats(self) -> dict:
        """Return collector stats."""
        avg_latency = (
            round(sum(self.latencies) / len(self.latencies), 1)
            if self.latencies
            else None
        )
        return {
            "total_received": self.total_received,
            "last_value": self.last_value,
            "last_ts_ms": self.last_ts_ms,
            "avg_latency_ms": avg_latency,
            "samples": len(self.latencies),
        }


def main():
    """Standalone CLI for testing."""
    import argparse
    from db import Database

    parser = argparse.ArgumentParser(description="Chainlink RTDS Collector Test")
    parser.add_argument("--db", default="data/btc5m.db", help="SQLite DB path")
    parser.add_argument("--count", type=int, default=20, help="Messages to collect")
    args = parser.parse_args()

    db = Database(args.db)
    db.init_schema()

    collector = ChainlinkCollector(db)

    received = []
    collector.set_on_message(lambda data, value: received.append((data, value)))

    collector.start()

    try:
        print(f"Collecting {args.count} Chainlink BTC/USD ticks...")
        while len(received) < args.count:
            time.sleep(0.5)
        collector.stop()
    except KeyboardInterrupt:
        collector.stop()

    stats = collector.stats()
    print(f"\n=== Results ===")
    print(f"Total received: {stats['total_received']}")
    print(f"Last value: {stats['last_value']}")
    print(f"Avg latency: {stats['avg_latency_ms']}ms")
    print(f"Samples: {stats['samples']}")

    # Show first 3 raw payloads
    print(f"\n=== First {min(3, len(received))} payloads ===")
    for i, (data, value) in enumerate(received[:3]):
        print(f"\n--- Message {i + 1} ---")
        print(f"Raw: {json.dumps(data, indent=2)}")
        print(f"Extracted value: {value}")

    # DB count
    with db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) as c FROM chainlink_btc_ticks").fetchone()["c"]
        print(f"\nDB rows written: {count}")


if __name__ == "__main__":
    main()
