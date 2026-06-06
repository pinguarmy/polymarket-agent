#!/usr/bin/env python3
"""Download BTC/USDT 1-minute klines from Binance for backtest coverage.

Fills gaps in binance_btc_ticks table for all markets in the database.
Free, no API key required. Rate limit: ~1200 req/min.
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "btc5m.db"
BINANCE_URL = "https://api.binance.com/api/v3/klines"

def fetch_klines(start_ms: int, end_ms: int) -> list:
    """Fetch up to 500 1-minute klines from Binance."""
    url = f"{BINANCE_URL}?symbol=BTCUSDT&interval=1m&startTime={start_ms}&endTime={end_ms}&limit=500"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-agent/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def download_all(db_path: str):
    db = sqlite3.connect(db_path)
    
    print("=" * 60)
    print("BTC PRICE DOWNLOAD")
    print("=" * 60)
    
    # Find the date range we need: from earliest market to latest
    markets = db.execute("""
        SELECT slug, open_time FROM markets 
        WHERE slug LIKE 'btc-updown-5m-%'
        ORDER BY open_time
    """).fetchall()
    
    if not markets:
        print("No BTC 5-min markets found")
        return
    
    # Parse timestamps from slugs (more reliable than open_time)
    timestamps = []
    for slug, open_time in markets:
        try:
            ts = int(slug[0].split("-")[-1]) if isinstance(slug, tuple) else int(slug.split("-")[-1])
            timestamps.append(ts)
        except (ValueError, IndexError):
            continue
    
    timestamps.sort()
    first_ts = timestamps[0] - 3600  # 1h buffer before
    last_ts = timestamps[-1] + 3600   # 1h buffer after
    
    start_dt = datetime.fromtimestamp(first_ts, timezone.utc)
    end_dt = datetime.fromtimestamp(last_ts, timezone.utc)
    
    print(f"Markets: {len(timestamps)}")
    print(f"Range: {start_dt} → {end_dt}")
    
    # Check existing coverage
    existing = db.execute("""
        SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM binance_btc_ticks
    """).fetchone()
    print(f"Existing BTC ticks: {existing[0]} ({existing[1]} → {existing[2]})")
    
    # Build set of timestamps we already have
    existing_ts = set()
    for row in db.execute("SELECT timestamp FROM binance_btc_ticks"):
        try:
            dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            existing_ts.add(int(dt.timestamp()))
        except:
            pass
    
    # Download in chunks
    chunk_start = first_ts * 1000
    chunk_end = last_ts * 1000
    inserted = 0
    skipped = 0
    chunks = 0
    
    print(f"\nDownloading...")
    
    while chunk_start < chunk_end:
        try:
            klines = fetch_klines(chunk_start, chunk_end)
        except Exception as e:
            print(f"\n  Error: {e}, retrying...")
            time.sleep(3)
            continue
        
        if not klines:
            break
        
        batch = []
        for k in klines:
            ts = int(k[0]) // 1000  # ms → seconds
            
            # Skip if we already have this minute
            if ts in existing_ts:
                skipped += 1
                continue
            
            close_price = float(k[4])
            high_price = float(k[2])
            low_price = float(k[3])
            ts_iso = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            batch.append((ts_iso, close_price, low_price, high_price))
            existing_ts.add(ts)
        
        if batch:
            db.executemany(
                "INSERT OR IGNORE INTO binance_btc_ticks (timestamp, price, bid, ask) VALUES (?, ?, ?, ?)",
                batch
            )
            inserted += len(batch)
        
        chunk_start = int(klines[-1][0]) + 60000  # next minute
        chunks += 1
        
        pct = min(100, int((chunk_start - first_ts * 1000) / (chunk_end - first_ts * 1000) * 100))
        print(f"  [{pct}%] {inserted} new, {skipped} skipped, {chunks} chunks", end="\r")
        time.sleep(0.05)
    
    db.commit()
    
    final = db.execute("SELECT COUNT(*) FROM binance_btc_ticks").fetchone()[0]
    print(f"\n\nDone: {inserted} new ticks added ({final} total)")
    db.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()
    download_all(args.db)
