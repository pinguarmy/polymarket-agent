#!/usr/bin/env python3
"""Probe Dune table schemas to find correct column names."""
import json, requests, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
env_path = PROJECT_ROOT / ".env"
key = None
for line in open(env_path):
    if line.startswith("DUNE_API_KEY="):
        key = line.split("=", 1)[1].strip()

s = requests.Session()
s.headers.update({"X-Dune-Api-Key": key})

# Try probing queries
probes = [
    ("market_details columns", "SELECT * FROM polymarket_polygon.market_details WHERE slug LIKE 'btc-updown%' LIMIT 1"),
    ("market_details sample", "SELECT * FROM polymarket_polygon.market_details LIMIT 1"),
    ("market_trades sample", "SELECT * FROM polymarket_polygon.market_trades LIMIT 1"),
]

for name, sql in probes:
    print(f"\n=== {name} ===")
    resp = s.post("https://api.dune.com/api/v1/query/", json={
        "name": f"probe-{name.replace(' ', '-')}",
        "query_sql": sql,
        "query_engine": "medium",
    })
    if resp.status_code != 200:
        print(f"  Create: {resp.status_code} {resp.text[:200]}")
        continue
    
    qid = resp.json().get("query_id")
    exec_resp = s.post(f"https://api.dune.com/api/v1/query/{qid}/execute", json={"query_engine": "medium"})
    if exec_resp.status_code != 200:
        print(f"  Execute: {exec_resp.status_code}")
        continue
    
    eid = exec_resp.json().get("execution_id")
    print(f"  Execution: {eid}")
    
    import time
    for _ in range(12):  # 60s timeout
        time.sleep(5)
        st = s.get(f"https://api.dune.com/api/v1/execution/{eid}/status")
        if st.status_code != 200:
            continue
        state = st.json().get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            res = s.get(f"https://api.dune.com/api/v1/execution/{eid}/results")
            if res.status_code == 200:
                data = res.json()
                rows = data.get("result", {}).get("rows", [])
                print(f"  Columns: {list(rows[0].keys()) if rows else 'NO ROWS'}")
                if rows:
                    print(f"  Sample: {json.dumps(rows[0], default=str)[:300]}")
            break
        elif state == "QUERY_STATE_FAILED":
            err = st.json().get("error", {})
            print(f"  Failed: {err.get('message', 'unknown')[:200]}")
            break
    else:
        print("  Timeout")
