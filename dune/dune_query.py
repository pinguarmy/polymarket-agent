#!/usr/bin/env python3
"""Query Dune Analytics API for BTC 5-min Polymarket historical data.

Usage:
  python3 dune/dune_query.py --output data/dune_results.csv
"""

import argparse
import csv
import io
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests

# Load API key from .env
def load_api_key():
    env_path = PROJECT_ROOT / ".env"
    for line in open(env_path):
        if line.startswith("DUNE_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("DUNE_API_KEY not found in .env")

DUNE_API_KEY = load_api_key()
DUNE_API = "https://api.dune.com/api/v1"
SESSION = requests.Session()
SESSION.headers.update({"X-Dune-Api-Key": DUNE_API_KEY})

BTC_5MIN_QUERY = """
SELECT
  t.block_time,
  t.condition_id,
  t.question,
  t.token_outcome_name,
  t.price,
  t.shares,
  t.amount,
  t.tx_hash
FROM polymarket_polygon.market_trades t
WHERE t.question LIKE 'Bitcoin Up or Down%'
ORDER BY t.block_time DESC
LIMIT 50000
"""

def execute_query(sql: str, name: str = "polymarket-btc-5min", engine: str = "medium") -> str | None:
    """Execute a SQL query on Dune. Returns execution_id.
    
    Dune API flow: create query → execute query → poll execution → download results.
    """
    # Step 1: Create (or update) the query
    print(f"  Creating query on Dune...")
    resp = SESSION.post(f"{DUNE_API}/query/", json={
        "name": name,
        "query_sql": sql,
        "query_engine": engine,
    })
    
    if resp.status_code != 200:
        print(f"  Create failed: {resp.status_code} {resp.text[:200]}")
        return None
    
    data = resp.json()
    query_id = data.get("query_id")
    if not query_id:
        print(f"  No query_id: {json.dumps(data)[:200]}")
        return None
    
    print(f"  Query ID: {query_id}")
    
    # Step 2: Execute the query
    print(f"  Executing (engine={engine})...")
    exec_resp = SESSION.post(f"{DUNE_API}/query/{query_id}/execute", json={
        "query_engine": engine,
    })
    
    if exec_resp.status_code != 200:
        print(f"  Execute failed: {exec_resp.status_code} {exec_resp.text[:200]}")
        return None
    
    exec_data = exec_resp.json()
    exec_id = exec_data.get("execution_id")
    if not exec_id:
        print(f"  No execution_id: {json.dumps(exec_data)[:200]}")
        return None
    
    print(f"  Execution ID: {exec_id}")
    
    # Step 3: Poll until complete
    max_wait = 600  # 10 min
    elapsed = 0
    while elapsed < max_wait:
        time.sleep(5)
        elapsed += 5
        
        status_resp = SESSION.get(f"{DUNE_API}/execution/{exec_id}/status")
        if status_resp.status_code != 200:
            continue
        
        status = status_resp.json()
        state = status.get("state", "QUERY_STATE_UNKNOWN")
        
        rows_written = status.get("result_metadata", {}).get("total_row_count", "?")
        print(f"  [{elapsed}s] {state} ({rows_written} rows)", end="\r")
        
        if state == "QUERY_STATE_COMPLETED":
            print(f"\n  Query completed! {rows_written} rows")
            return exec_id
        elif state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            error = status.get("error", "unknown")
            print(f"\n  Query failed: {str(error)[:200]}")
            return None
    
    print(f"\n  Timeout after {max_wait}s")
    return None

def download_results(exec_id: str, output_path: str, page_size: int = 1000) -> int:
    """Download results with pagination. Returns total row count."""
    print(f"  Downloading results (paginated, {page_size} rows/page)...")
    
    all_rows = []
    offset = 0
    page = 1
    
    while True:
        resp = SESSION.get(
            f"{DUNE_API}/execution/{exec_id}/results",
            params={"limit": page_size, "offset": offset}
        )
        
        if resp.status_code != 200:
            if page == 1:
                print(f"  Download failed: {resp.status_code}")
                return 0
            break
        
        data = resp.json()
        rows = data.get("result", {}).get("rows", [])
        if not rows:
            break
        
        all_rows.extend(rows)
        total = data.get("result", {}).get("metadata", {}).get("total_row_count", "?")
        print(f"  Page {page}: {len(rows)} rows (total: {len(all_rows)}/{total})", end="\r")
        
        next_uri = data.get("next_uri")
        if not next_uri:
            break
        
        offset += page_size
        page += 1
    
    print(f"\n  Downloaded {len(all_rows)} rows total")
    
    if all_rows:
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"  Saved → {output_path}")
    
    return len(all_rows)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/dune_results.csv")
    parser.add_argument("--engine", default="medium", choices=["medium", "large"])
    args = parser.parse_args()
    
    print("=" * 60)
    print("DUNE API QUERY")
    print("=" * 60)
    print(f"  API Key: {DUNE_API_KEY[:8]}...")
    
    exec_id = execute_query(BTC_5MIN_QUERY, engine=args.engine)
    if not exec_id:
        sys.exit(1)
    
    output = PROJECT_ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    
    rows = download_results(exec_id, str(output))
    if rows > 0:
        print(f"\nDone! {rows} rows saved.")
        print(f"Next: python3 dune/dune_import.py --csv {output}")
    else:
        print("\nNo results or download failed.")

if __name__ == "__main__":
    main()
