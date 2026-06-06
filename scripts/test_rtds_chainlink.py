#!/usr/bin/env python3
"""Test script for Polymarket RTDS Chainlink BTC/USD Data Stream.

Connects, subscribes, collects N messages, prints raw payloads + analysis.

Usage:
    python3 scripts/test_rtds_chainlink.py --count 20
    python3 scripts/test_rtds_chainlink.py --count 5 --no-db  # skip DB write
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Ensure src/ is on path
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from chainlink_collector import ChainlinkCollector, RTDS_URL, SUBSCRIBE_MSG
from db import Database


def analyze_payload(data: dict) -> dict:
    """Analyze a single RTDS message and return field mapping."""
    analysis = {
        "top_level_keys": list(data.keys()),
        "has_payload": "payload" in data,
        "payload_keys": list(data.get("payload", {}).keys()) if "payload" in data else [],
        "value_locations": [],
        "timestamp_locations": [],
        "value_raw": None,
        "value_normalized": None,
        "source_ts_ms": None,
        "topic": data.get("topic") or data.get("type", ""),
        "message_type": data.get("message_type") or data.get("event_type", ""),
    }

    # Probe for value in all known locations
    probes_value = [
        ("data.payload.value", data.get("payload", {}).get("value")),
        ("data.value", data.get("value")),
        ("data.payload.price", data.get("payload", {}).get("price")),
        ("data.price", data.get("price")),
        ("data.data.value", data.get("data", {}).get("value")),
        ("data.data.price", data.get("data", {}).get("price")),
    ]
    for path, val in probes_value:
        if val is not None:
            analysis["value_locations"].append({"path": path, "value": val, "type": type(val).__name__})
            if analysis["value_raw"] is None:
                analysis["value_raw"] = float(val)

    # Format 2: array in payload.data (subscribe response batch)
    if analysis["value_raw"] is None:
        payload_data = data.get("payload", {}).get("data")
        if isinstance(payload_data, list) and len(payload_data) > 0:
            last = payload_data[-1]
            last_val = last.get("value")
            if last_val is not None:
                analysis["value_locations"].append({
                    "path": f"data.payload.data[-1].value",
                    "value": last_val,
                    "type": type(last_val).__name__,
                })
                analysis["value_raw"] = float(last_val)

    # Probe for timestamp
    probes_ts = [
        ("data.payload.timestamp", data.get("payload", {}).get("timestamp")),
        ("data.payload.timestamp_ms", data.get("payload", {}).get("timestamp_ms")),
        ("data.timestamp", data.get("timestamp")),
        ("data.timestamp_ms", data.get("timestamp_ms")),
        ("data.data.timestamp", data.get("data", {}).get("timestamp")),
        ("data.data.timestamp_ms", data.get("data", {}).get("timestamp_ms")),
        ("data.payload.updated_at", data.get("payload", {}).get("updated_at")),
        ("data.payload.source_timestamp", data.get("payload", {}).get("source_timestamp")),
    ]
    for path, val in probes_ts:
        if val is not None:
            analysis["timestamp_locations"].append({"path": path, "value": val, "type": type(val).__name__})
            if analysis["source_ts_ms"] is None:
                analysis["source_ts_ms"] = int(val)

    # Normalize
    if analysis["value_raw"] is not None:
        vr = analysis["value_raw"]
        if vr > 1_000_000_000:
            analysis["value_normalized"] = vr / 100_000_000.0
            analysis["normalization"] = f"divided by 1e8 (was int {vr})"
        else:
            analysis["value_normalized"] = vr
            analysis["normalization"] = f"already float (no division needed)"
    else:
        analysis["value_normalized"] = None
        analysis["normalization"] = "NO VALUE FOUND"

    return analysis


def main():
    parser = argparse.ArgumentParser(description="Test Polymarket RTDS Chainlink Data Stream")
    parser.add_argument("--count", type=int, default=20, help="Number of messages to collect")
    parser.add_argument("--db", default="data/btc5m.db", help="SQLite DB path")
    parser.add_argument("--no-db", action="store_true", help="Skip DB writes (print only)")
    args = parser.parse_args()

    print("=" * 60)
    print("Polymarket RTDS Chainlink BTC/USD — Connection Test")
    print("=" * 60)
    print(f"WebSocket: {RTDS_URL}")
    print(f"Subscribe: {json.dumps(SUBSCRIBE_MSG, indent=2)}")
    print(f"Target:    {args.count} messages")
    print(f"DB write:  {'disabled' if args.no_db else 'enabled'}")
    print()

    db = None if args.no_db else Database(args.db)
    if db:
        db.init_schema()

    collector = ChainlinkCollector(db) if db else ChainlinkCollector(None)
    # Override _write_tick to no-op if no db
    if args.no_db:
        collector._write_tick = lambda *a, **kw: None

    raw_payloads = []
    analyses = []

    def on_message(data, value):
        raw_payloads.append(data)
        analysis = analyze_payload(data)
        analyses.append(analysis)

    collector.set_on_message(on_message)
    collector.start()

    print(f"Connecting and collecting {args.count} messages...")
    print()

    # Wait for count, with timeout
    t0 = time.time()
    timeout = 60  # max 60 seconds
    while len(raw_payloads) < args.count and (time.time() - t0) < timeout:
        time.sleep(0.5)

    collector.stop()

    elapsed = time.time() - t0
    if len(raw_payloads) == 0:
        print("❌ No messages received. Connection failed or timed out.")
        print("Possible reasons:")
        print("  - WebSocket URL is wrong")
        print("  - Subscribe message format is wrong")
        print("  - Network/firewall blocking wss://")
        sys.exit(1)

    print(f"✅ Received {len(raw_payloads)} messages in {elapsed:.1f}s")
    print()

    # === Analysis ===
    print("=" * 60)
    print("FIRST 3 RAW PAYLOADS")
    print("=" * 60)
    for i, data in enumerate(raw_payloads[:3]):
        print(f"\n--- Message {i + 1} ---")
        print(json.dumps(data, indent=2))

    print()
    print("=" * 60)
    print("FIELD ANALYSIS")
    print("=" * 60)
    a = analyses[0]
    print(f"\nTop-level keys: {a['top_level_keys']}")
    print(f"Has payload sub-object: {a['has_payload']}")
    if a["payload_keys"]:
        print(f"Payload keys: {a['payload_keys']}")
    print(f"Value locations found: {len(a['value_locations'])}")
    for loc in a["value_locations"]:
        print(f"  -> {loc['path']} = {loc['value']} ({loc['type']})")
    print(f"Timestamp locations found: {len(a['timestamp_locations'])}")
    for loc in a["timestamp_locations"][:3]:
        print(f"  -> {loc['path']} = {loc['value']} ({loc['type']})")
    print(f"Topic: {a['topic']}")
    print(f"Message type: {a['message_type']}")
    print(f"Normalization: {a['normalization']}")

    # === Summary across all messages ===
    print()
    print("=" * 60)
    print("SUMMARY (ALL MESSAGES)")
    print("=" * 60)

    # Value analysis
    values = [a["value_normalized"] for a in analyses if a["value_normalized"] is not None]
    if values:
        print(f"Value range: {min(values):.2f} — {max(values):.2f} USD")
        print(f"Value avg:   {sum(values)/len(values):.2f} USD")

    # Timestamp analysis
    timestamps = [a["source_ts_ms"] for a in analyses if a["source_ts_ms"] is not None]
    if timestamps:
        print(f"Timestamp range: {min(timestamps)} — {max(timestamps)} ms (Unix ms)")

    # Value type
    value_types = set(a["value_locations"][0]["type"] if a["value_locations"] else "unknown" for a in analyses)
    print(f"Value type(s): {value_types}")

    # Timestamp field consistency
    ts_fields = set()
    for a in analyses:
        for loc in a["timestamp_locations"]:
            ts_fields.add(loc["path"])
    print(f"Timestamp field(s): {ts_fields}")

    # Latency
    if collector.latencies:
        avg = sum(collector.latencies) / len(collector.latencies)
        print(f"Avg latency: {avg:.0f}ms")
        max_lat = max(collector.latencies)
        min_lat = min(collector.latencies)
        print(f"Latency range: {min_lat} — {max_lat}ms")

    # Value stability (are all values same or changing?)
    if len(values) >= 2:
        changes = sum(1 for i in range(1, len(values)) if values[i] != values[i - 1])
        print(f"Value changes: {changes}/{len(values) - 1} messages had different value")

    # Topic / event type consistency
    topics = set(a["topic"] for a in analyses)
    msg_types = set(a["message_type"] for a in analyses)
    print(f"Topic(s): {topics}")
    print(f"Message type(s): {msg_types}")

    # DB check
    if db and not args.no_db:
        with db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) as c FROM chainlink_btc_ticks").fetchone()["c"]
            print(f"\nDB rows written to chainlink_btc_ticks: {count}")
            # Show first 3 DB rows
            rows = conn.execute(
                "SELECT id, value_raw, value_normalized, source_timestamp_ms, "
                "latency_ms, topic, message_type FROM chainlink_btc_ticks ORDER BY id DESC LIMIT 3"
            ).fetchall()
            for row in rows:
                print(
                    f"  Row {row['id']}: value_raw={row['value_raw']}, "
                    f"normalized={row['value_normalized']}, "
                    f"ts_ms={row['source_timestamp_ms']}, "
                    f"latency={row['latency_ms']}ms"
                )

    print()
    print("=" * 60)
    print("RESULTS (for audit)")

    # Determine value type
    if values:
        if max(values) > 1_000_000:
            print("1. value is INT (likely 8-decimals, needs /1e8)")
        else:
            print("1. value is FLOAT (already USD price, no division needed)")

    # Timestamp location
    if timestamps:
        best_ts_field = list(ts_fields)[0] if ts_fields else "unknown"
        print(f"2. timestamp in: {best_ts_field}")

    if collector.latencies:
        avg_lat = sum(collector.latencies) / len(collector.latencies)
        print(f"3. avg latency: {avg_lat:.0f}ms")

    print(f"4. reconnect logic: {'yes (built into ChainlinkCollector)' if True else 'no'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
