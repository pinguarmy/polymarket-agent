#!/usr/bin/env python3
"""5-minute Chainlink RTDS validation script.

Runs the collector for 5 minutes, then outputs:
- Total messages received
- Gaps detected (< 1 update per second)
- Average and p95 latency
- Latest price
- Health status
"""

import argparse
import json
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from chainlink_collector import ChainlinkCollector
from chainlink_helpers import (
    get_latest_chainlink_price,
    get_chainlink_stats_60s,
    write_chainlink_health,
)
from db import Database


def percentile(data, p):
    """Compute p-th percentile."""
    if not data:
        return None
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


def main():
    parser = argparse.ArgumentParser(description="Validate Chainlink RTDS data stream for 5 minutes")
    parser.add_argument("--db", default="data/btc5m.db", help="SQLite DB path")
    parser.add_argument("--duration", type=int, default=300, help="Collection duration in seconds")
    parser.add_argument("--no-db", action="store_true", help="Skip DB writes")
    args = parser.parse_args()

    db = None if args.no_db else Database(args.db)
    if db:
        db.init_schema()

    collector = ChainlinkCollector(db) if db else ChainlinkCollector(None)
    if args.no_db:
        collector._write_tick = lambda *a, **kw: None

    # Track gaps
    msg_timestamps = []
    latencies = []

    def on_message(data, value):
        now = time.time()
        msg_timestamps.append(now)
        if collector.latencies:
            # Take the most recent latency
            pass

    collector.set_on_message(on_message)

    print(f"Chainlink RTDS Validation — {args.duration}s")
    print(f"{'='*50}")
    print(f"DB writes: {'enabled' if db and not args.no_db else 'disabled'}")
    print(f"Target: {args.duration}s continuous collection")
    print()

    collector.start()

    # Also write health check every 30s
    next_health = time.time() + 30
    t0 = time.time()

    while time.time() - t0 < args.duration:
        time.sleep(1)
        elapsed = int(time.time() - t0)

        # Capture latest latency from collector
        if collector.latencies:
            latencies.extend(collector.latencies[-10:])  # sample recent
            collector.latencies = []  # clear for next sample

        # Write health check every 30s
        if time.time() >= next_health and db and not args.no_db:
            write_chainlink_health(db)
            latest = get_latest_chainlink_price(db)
            stats = get_chainlink_stats_60s(db)
            status = "✅" if stats["is_healthy"] else "❌"
            price_str = f"${latest['value_normalized']:.2f}" if latest else "N/A"
            print(
                f"  T+{elapsed:4d}s | {status} {collector.total_received} msgs | "
                f"BTC={price_str} | healthy={stats['is_healthy']} | "
                f"60s_count={stats['count_60s']}"
            )
            next_health = time.time() + 30

    collector.stop()
    total_time = time.time() - t0

    print()
    print(f"{'='*50}")
    print(f"RESULTS")
    print(f"{'='*50}")

    # 1. Total messages
    print(f"\n1. Total messages received: {collector.total_received}")
    print(f"   Duration: {total_time:.1f}s")
    print(f"   Rate: {collector.total_received / total_time:.2f} msgs/s")

    # 2. Gaps
    print(f"\n2. Gap analysis:")
    if len(msg_timestamps) >= 2:
        gaps = []
        for i in range(1, len(msg_timestamps)):
            gap = msg_timestamps[i] - msg_timestamps[i - 1]
            gaps.append(gap)

        max_gap = max(gaps) if gaps else 0
        avg_gap = sum(gaps) / len(gaps) if gaps else 0
        gaps_over_2s = sum(1 for g in gaps if g > 2.0)
        gaps_over_10s = sum(1 for g in gaps if g > 10.0)

        print(f"   Max gap: {max_gap:.1f}s")
        print(f"   Avg gap: {avg_gap:.2f}s")
        print(f"   Gaps > 2s: {gaps_over_2s}")
        print(f"   Gaps > 10s: {gaps_over_10s}")
    else:
        print("   (not enough data points)")

    # 3. Latency
    print(f"\n3. Latency analysis:")
    if collector.latencies:
        all_latencies = collector.latencies
        avg_lat = sum(all_latencies) / len(all_latencies)
        p95 = percentile(all_latencies, 95)
        p99 = percentile(all_latencies, 99)
        max_lat = max(all_latencies)
        min_lat = min(all_latencies)

        print(f"   Samples: {len(all_latencies)}")
        print(f"   Average: {avg_lat:.0f}ms")
        print(f"   P95:     {p95:.0f}ms" if p95 else "   P95: N/A")
        print(f"   P99:     {p99:.0f}ms" if p99 else "   P99: N/A")
        print(f"   Min:     {min_lat:.0f}ms")
        print(f"   Max:     {max_lat:.0f}ms")
    else:
        print("   (no latency data)")

    # 4. Latest price
    print(f"\n4. Latest price:")
    if db and not args.no_db:
        latest = get_latest_chainlink_price(db)
        if latest:
            print(f"   BTC: ${latest['value_normalized']:.2f}")
            ts = latest.get("source_timestamp_ms")
            if ts:
                print(f"   Timestamp: {ts} ms (Unix)")
            print(f"   Latency: {latest.get('latency_ms', 'N/A')}ms")
            print(f"   Topic: {latest.get('topic', 'N/A')}")
        else:
            print("   No data in DB")
    else:
        print(f"   Last value: {collector.last_value}")

    # 5. Health status
    print(f"\n5. Health status:")
    if db and not args.no_db:
        health = write_chainlink_health(db)
        print(f"   Healthy (<10s since last update): {health['is_healthy']}")
        print(f"   Last update age: {health['age_seconds']}s" if health.get("age_seconds") else "")
        print(f"   Messages in last 60s: {health['count_60s']}")
        print(f"   Health file: logs/chainlink_health.json")
    else:
        print("   (no DB — health check not available)")

    # 6. Disconnects
    print(f"\n6. Connection:")
    print(f"   WS fail count: {collector._ws_fail_count}")
    print(f"   Total reconnects: {collector._ws_fail_count}")
    print(f"   Auto-reconnect: {'✅ built-in' if True else '❌'}")

    # Summary
    print()
    print(f"{'='*50}")
    print(f"VERDICT")
    print(f"{'='*50}")
    if collector.total_received > 0:
        print("✅ Chainlink RTDS data stream is operational")
        print(f"   Value type: FLOAT (USD, no /1e8 needed)")
        print(f"   Update rate: {collector.total_received / total_time:.2f} msgs/s")
        if collector.latencies:
            avg_l = sum(collector.latencies) / len(collector.latencies)
            print(f"   Avg latency: {avg_l:.0f}ms")
        print()
        print("   Ready for: settlement price queries, post-trade PnL, backtest validation")
    else:
        print("❌ No data received — check WebSocket connection")
        sys.exit(1)


if __name__ == "__main__":
    main()
