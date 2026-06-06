#!/usr/bin/env python3
"""One-shot probe to discover Dune table schemas."""
import json, requests, time, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
key = None
for line in open(PROJECT_ROOT / ".env"):
    if line.startswith("DUNE_API_KEY="):
        key = line.split("=", 1)[1].strip()

s = requests.Session()
s.headers.update({"X-Dune-Api-Key": key})

# Single query to discover all schemas
sql = """
SELECT 'TRADES' as source, t.* 
FROM polymarket_polygon.market_trades t 
LIMIT 1
"""

print("Probing Dune schema...")
resp = s.post("https://api.dune.com/api/v1/query/", json={
    "name": "schema-probe", "query_sql": sql, "query_engine": "medium",
})

if resp.status_code != 200:
    print(f"Create failed: {resp.status_code}")
    # Try market_details instead
    sql2 = "SELECT * FROM polymarket_polygon.market_details LIMIT 1"
    resp = s.post("https://api.dune.com/api/v1/query/", json={
        "name": "schema-probe-2", "query_sql": sql2, "query_engine": "medium",
    })
    if resp.status_code != 200:
        print(f"Details also failed: {resp.text[:200]}")
        sys.exit(1)

qid = resp.json()["query_id"]
exec_r = s.post(f"https://api.dune.com/api/v1/query/{qid}/execute", json={"query_engine": "medium"})
eid = exec_r.json()["execution_id"]

for _ in range(20):
    time.sleep(5)
    st = s.get(f"https://api.dune.com/api/v1/execution/{eid}/status")
    state = st.json().get("state", "")
    print(f"  {state}")
    if state == "QUERY_STATE_COMPLETED":
        res = s.get(f"https://api.dune.com/api/v1/execution/{eid}/results")
        data = res.json()
        rows = data.get("result", {}).get("rows", [])
        if rows:
            print(f"\nCOLUMNS: {list(rows[0].keys())}")
            print(f"Sample: {json.dumps(rows[0], default=str)[:400]}")
        break
    elif "FAILED" in state:
        err = st.json().get("error", {})
        print(f"Failed: {err.get('message', '')[:200]}")
        break
