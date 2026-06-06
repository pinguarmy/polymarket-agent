#!/usr/bin/env python3
"""Import Dune CSV into local SQLite database.

Handles Dune's specific column format:
  block_time, condition_id, question, token_outcome_name, price, shares, amount, tx_hash

Parses question text to extract market timestamps for BTC price matching.
"""

import argparse
import csv
import os
import sqlite3
import sys
import time
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import re

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "btc5m.db"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# === BTC Price Download ===

def fetch_binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 500) -> list:
    url = f"{BINANCE_KLINES_URL}?symbol={symbol}&interval={interval}&startTime={start_ms}&endTime={end_ms}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-agent/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

def download_btc_prices(start_ts: int, end_ts: int, db_conn) -> int:
    existing = db_conn.execute("SELECT MAX(timestamp) FROM binance_btc_ticks").fetchone()
    if existing[0]:
        try:
            exist_dt = datetime.fromisoformat(existing[0].replace("Z", "+00:00"))
            start_ts = max(start_ts, int(exist_dt.timestamp()) + 60)
        except:
            pass
    
    inserted = 0
    chunk_start = start_ts * 1000
    chunk_end = end_ts * 1000
    
    while chunk_start < chunk_end:
        try:
            klines = fetch_binance_klines("BTCUSDT", "1m", chunk_start, chunk_end, 500)
        except Exception as e:
            print(f"  Binance error: {e}, retrying...")
            time.sleep(2)
            continue
        
        if not klines:
            break
        
        for k in klines:
            ts = int(k[0]) // 1000
            close_price = float(k[4])
            high_price = float(k[2])
            low_price = float(k[3])
            ts_iso = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            try:
                db_conn.execute(
                    "INSERT OR IGNORE INTO binance_btc_ticks (timestamp, price, bid, ask) VALUES (?, ?, ?, ?)",
                    (ts_iso, close_price, low_price, high_price)
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass
        
        chunk_start = int(klines[-1][0]) + 60000
        print(f"  BTC: {inserted} ticks...", end="\r")
        time.sleep(0.1)
    
    print(f"\n  BTC: {inserted} new ticks")
    db_conn.commit()
    return inserted

# === Question Parser ===

def parse_question(question: str) -> tuple[str, int]:
    """Parse Dune question to extract slug and timestamp.
    
    Question format: "Bitcoin Up or Down - April 30, 1:30AM-1:35AM ET"
    Token format: "Up-Bitcoin Up or Down - April 30, 1:30AM-1:35AM ET"
    
    Returns (slug, open_timestamp)
    """
    # Try to extract date and time range
    # Pattern: "Month DD, H:MMAM-H:MMAM ET"
    date_match = re.search(r'(\w+ \d+), (\d+:\d+[AP]M)-(\d+:\d+[AP]M) ET', question)
    if date_match:
        date_str = date_match.group(1)  # "April 30"
        time_str = date_match.group(2)  # "1:30AM"
        
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%B %d %I:%M%p")
            # Assume current year if not specified
            now = datetime.now()
            dt = dt.replace(year=now.year)
            ts = int(dt.timestamp())
            slug = f"btc-updown-5m-{ts}"
            return slug, ts
        except ValueError:
            pass
    
    return question, 0

# === Import ===

def import_dune_csv(csv_path: str, db_conn) -> dict:
    new_markets = 0
    new_trades = 0
    skipped = 0
    
    # Get existing condition_ids for dedup
    existing_conditions = set()
    for row in db_conn.execute("SELECT condition_id FROM markets").fetchall():
        existing_conditions.add(row[0])
    
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch = []
        
        for row in reader:
            try:
                condition_id = row.get("condition_id", "").strip()
                question = row.get("question", "").strip()
                token_outcome = row.get("token_outcome_name", "").strip()
                price = float(row.get("price", 0))
                shares = float(row.get("shares", 0))
                block_time = row.get("block_time", "").strip()
                tx_hash = row.get("tx_hash", "").strip()
                
                if not condition_id or not question:
                    skipped += 1
                    continue
                
                # Parse question for slug and timestamp
                slug, open_ts = parse_question(question)
                
                # Determine token side
                token_outcome_clean = token_outcome.replace(f"{question}", "").strip("-")
                if token_outcome_clean.lower().startswith("up"):
                    side = "YES"
                elif token_outcome_clean.lower().startswith("down"):
                    side = "NO"
                else:
                    side = "YES" if "Up" in token_outcome else "NO"
                
                # Insert market if new
                if condition_id not in existing_conditions:
                    open_time = datetime.fromtimestamp(open_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if open_ts else block_time
                    db_conn.execute(
                        "INSERT OR IGNORE INTO markets (slug, question, condition_id, open_time) VALUES (?, ?, ?, ?)",
                        (slug, question, condition_id, open_time)
                    )
                    existing_conditions.add(condition_id)
                    new_markets += 1
                
                # Get market_id
                market_row = db_conn.execute(
                    "SELECT market_id FROM markets WHERE condition_id = ?", (condition_id,)
                ).fetchone()
                if not market_row:
                    skipped += 1
                    continue
                
                market_id = market_row[0]
                
                # Normalize timestamp
                trade_ts = block_time
                if not trade_ts.endswith("Z"):
                    trade_ts = trade_ts.replace(" UTC", "Z").replace(" ", "T")
                
                # Insert trade
                db_conn.execute(
                    """INSERT OR IGNORE INTO polymarket_trades 
                       (market_id, side, price, size, trade_timestamp, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (market_id, side, price, shares, trade_ts, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                )
                new_trades += 1
                
            except Exception as e:
                skipped += 1
                if skipped <= 3:
                    print(f"  Skipping row: {e}")
        
    db_conn.commit()
    
    return {"new_markets": new_markets, "new_trades": new_trades, "skipped": skipped}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--no-btc", action="store_true")
    args = parser.parse_args()
    
    if not os.path.exists(args.csv):
        print(f"ERROR: {args.csv} not found")
        sys.exit(1)
    
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    
    print("=" * 60)
    print("DUNE CSV IMPORT")
    print("=" * 60)
    
    before_trades = db.execute("SELECT COUNT(*) FROM polymarket_trades").fetchone()[0]
    before_markets = db.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    
    print(f"\n[1/2] Importing {args.csv}...")
    result = import_dune_csv(args.csv, db)
    
    after_trades = db.execute("SELECT COUNT(*) FROM polymarket_trades").fetchone()[0]
    after_markets = db.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    
    print(f"  Markets: {before_markets} → {after_markets} (+{result['new_markets']})")
    print(f"  Trades: {before_trades} → {after_trades} (+{result['new_trades']})")
    print(f"  Skipped: {result['skipped']}")
    
    if not args.no_btc:
        print(f"\n[2/2] Downloading BTC prices...")
        date_range = db.execute("""
            SELECT MIN(trade_timestamp), MAX(trade_timestamp)
            FROM polymarket_trades WHERE trade_timestamp IS NOT NULL
        """).fetchone()
        
        if date_range[0]:
            try:
                min_dt = datetime.fromisoformat(date_range[0].replace("Z", "+00:00").replace(" UTC", "+00:00"))
                max_dt = datetime.fromisoformat(date_range[1].replace("Z", "+00:00").replace(" UTC", "+00:00"))
                start_ts = int(min_dt.timestamp()) - 3600
                end_ts = int(max_dt.timestamp()) + 3600
            except:
                end_ts = int(time.time())
                start_ts = end_ts - 90 * 86400
        else:
            end_ts = int(time.time())
            start_ts = end_ts - 90 * 86400
        
        print(f"  Range: {datetime.fromtimestamp(start_ts)} → {datetime.fromtimestamp(end_ts)}")
        download_btc_prices(start_ts, end_ts, db)
    
    print(f"\nDatabase summary:")
    for table in ["markets", "polymarket_trades", "binance_btc_ticks"]:
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count}")
    
    print(f"\nDone. Run: python3 dune/dune_backtest.py")
    db.close()

if __name__ == "__main__":
    main()
