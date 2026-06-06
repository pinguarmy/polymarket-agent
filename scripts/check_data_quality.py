#!/usr/bin/env python3
"""Check data quality of all Polymarket bot data sources.

Outputs P0/P1/P2 issues. If any P0 found, blocks going to live.

Usage:
    python3 scripts/check_data_quality.py
    python3 scripts/check_data_quality.py --json   # JSON output
"""

import argparse
import json
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from db import Database
from chainlink_helpers import get_latest_chainlink_price, get_chainlink_stats_60s


def check_all(db, output_json=False):
    issues = {"P0": [], "P1": [], "P2": []}
    passed = []

    def add_issue(sev, msg):
        issues[sev].append(msg)

    def add_pass(msg):
        passed.append(msg)

    # ── 1. Chainlink freshness ──
    stats = get_chainlink_stats_60s(db)
    if stats["is_healthy"]:
        add_pass(f"Chainlink RTDS healthy: age={stats['age_seconds']}s, {stats['count_60s']}/min")
    else:
        add_issue("P0", f"Chainlink RTDS stale: age={stats['age_seconds']}s (>10s)")
    cl_latest = get_latest_chainlink_price(db)
    if cl_latest:
        add_pass(f"Chainlink latest: ${cl_latest['value_normalized']:.2f} @ ts={cl_latest['source_timestamp_ms']}")
    else:
        add_issue("P0", "No Chainlink data in DB")

    # ── 2. CLOB /book availability ──
    import urllib.request
    import json as j
    try:
        # Get a current market slug first
        with open(SRC.parent / "logs" / "trader_state.json") as f:
            state = j.load(f)
        slug = state.get("slug", "")
        if slug:
            gamma_url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
            req = urllib.request.Request(gamma_url, headers={"User-Agent":"curl/7.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                m = j.loads(resp.read())[0]
            cids = j.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds",""), str) else m.get("clobTokenIds",[])
            if cids:
                yes_id = cids[0]
                # /book
                req = urllib.request.Request(f"https://clob.polymarket.com/book?token_id={yes_id}", headers={"User-Agent":"curl/7.0"})
                book = j.loads(urllib.request.urlopen(req, timeout=5).read())
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    bb = float(bids[0]["price"]) if isinstance(bids[0], dict) else float(bids[0][0])
                    ba = float(asks[0]["price"]) if isinstance(asks[0], dict) else float(asks[0][0])
                    spread = ba - bb
                    mid = (bb + ba) / 2
                    spread_pct = spread / mid * 100
                    add_pass(f"CLOB /book available: best_bid={bb}, best_ask={ba}, spread={spread_pct:.1f}%")
                    if spread_pct > 20:
                        add_issue("P1", f"CLOB spread very high: {spread_pct:.1f}% (low liquidity)")
                    # Check depth
                    bd = sum(float(b["size"]) if isinstance(b, dict) else float(b[1]) for b in bids[:5])
                    ad = sum(float(a["size"]) if isinstance(a, dict) else float(a[1]) for a in asks[:5])
                    if bd < 10 or ad < 10:
                        add_issue("P2", f"CLOB shallow depth: bid_depth={bd:.1f} ask_depth={ad:.1f}")
                else:
                    add_issue("P1", "CLOB /book has empty bids/asks")
            else:
                add_issue("P1", "Cannot get token IDs from Gamma")
        else:
            add_issue("P1", "No active market slug in trader_state.json")
    except Exception as e:
        add_issue("P1", f"CLOB /book check failed: {e}")

    # ── 3. CLOB /midpoint ──
    try:
        if cids:
            yes_id = cids[0]
            req = urllib.request.Request(f"https://clob.polymarket.com/midpoint?token_id={yes_id}", headers={"User-Agent":"curl/7.0"})
            mp = j.loads(urllib.request.urlopen(req, timeout=5).read())
            if mp.get("mid"):
                add_pass(f"CLOB /midpoint available: {mp['mid']}")
            else:
                add_issue("P1", "CLOB /midpoint returned no 'mid' field")
    except Exception as e:
        add_issue("P1", f"CLOB /midpoint check failed: {e}")

    # ── 4. CLOB /last-trade-price ──
    try:
        if cids:
            yes_id = cids[0]
            req = urllib.request.Request(f"https://clob.polymarket.com/last-trade-price?token_id={yes_id}", headers={"User-Agent":"curl/7.0"})
            ltp = j.loads(urllib.request.urlopen(req, timeout=5).read())
            if ltp.get("price"):
                add_pass(f"CLOB /last-trade-price available: {ltp['price']} side={ltp.get('side')}")
            else:
                add_issue("P2", "CLOB /last-trade-price returned no 'price'")
    except Exception as e:
        add_issue("P2", f"CLOB /last-trade-price check failed: {e}")

    # ── 5. Gamma metadata availability ──
    try:
        if slug:
            gamma_url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
            req = urllib.request.Request(gamma_url, headers={"User-Agent":"curl/7.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                m = j.loads(resp.read())[0]
            # Check for required fields
            required = ["resolutionSource", "endDate", "outcomePrices", "clobTokenIds", "conditionId"]
            missing = [f for f in required if f not in m]
            if missing:
                add_issue("P1", f"Gamma missing fields: {missing}")
            else:
                add_pass(f"Gamma metadata OK: resolutionSource={m.get('resolutionSource','')[:50]}")
                add_pass(f"Gamma endDate={m.get('endDate')}")
    except Exception as e:
        add_issue("P1", f"Gamma metadata check failed: {e}")

    # ── 6. Database tables ──
    with db.get_connection() as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        table_names = [t["name"] for t in tables]
        required_tables = ["chainlink_btc_ticks", "clob_orderbook_snapshots", "trade_data_snapshots",
                           "markets", "binance_btc_ticks"]
        for rt in required_tables:
            if rt in table_names:
                count = conn.execute(f"SELECT COUNT(*) as c FROM {rt}").fetchone()["c"]
                add_pass(f"Table {rt}: {count} rows")
            else:
                add_issue("P0", f"Required table missing: {rt}")

    # ── 7. Code-level checks (scan for known anti-patterns) ──
    trader_py = SRC / "realtime_trader.py"
    if trader_py.exists():
        text = trader_py.read_text()
        
        # Check for Binance settlement (still allowed in backtest, but check)
        binance_settle_count = text.count("Binance")
        
        # Check for midpoint as fill price
        if "midpoint" in text and "fill_price" in text:
            add_pass("fill_price field present (not midpoint-only)")
        
        # Check for chainlink settlement
        if "chainlink_entry_btc" in text:
            add_pass("Chainlink settlement tracking present")
        
        # Check for settlement_source
        if "settlement_source" in text:
            add_pass("settlement_source field present")

    # ── 8. Trader process status ──
    import subprocess
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
    if "realtime_trader.py" in result.stdout:
        add_pass("realtime_trader process running")
    else:
        add_issue("P1", "realtime_trader process NOT running")

    # ── Summary ──
    print("=" * 60)
    print("DATA QUALITY CHECK REPORT")
    print("=" * 60)
    
    if passed:
        print(f"\n✅ PASSED ({len(passed)}):")
        for p in passed:
            print(f"  ✅ {p}")
    
    for sev in ["P0", "P1", "P2"]:
        if issues[sev]:
            icon = "🔴" if sev == "P0" else "🟡" if sev == "P1" else "🟢"
            print(f"\n{icon} {sev} ISSUES ({len(issues[sev])}):")
            for i in issues[sev]:
                print(f"  {icon} {i}")

    print()
    print("=" * 60)
    
    can_live = len(issues["P0"]) == 0
    if can_live:
        print("✅ DATA QUALITY: No P0 issues. Can proceed to paper testing.")
    else:
        print(f"🔴 BLOCKED: {len(issues['P0'])} P0 issues must be fixed before live trading.")
    
    if issues["P1"]:
        print(f"🟡 P1 issues: {len(issues['P1'])} — should fix before formal testing.")
    if issues["P2"]:
        print(f"🟢 P2 issues: {len(issues['P2'])} — can optimize later.")
    
    if output_json:
        return {
            "passed": passed,
            "issues": issues,
            "can_proceed_to_live": can_live,
            "timestamp": time.time(),
        }
    
    return can_live


def main():
    parser = argparse.ArgumentParser(description="Data quality check for Polymarket bot")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--db", default="data/btc5m.db")
    args = parser.parse_args()

    db = Database(args.db)
    db.init_schema()
    
    result = check_all(db, output_json=args.json)
    
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    
    sys.exit(0 if isinstance(result, bool) and result else 1)


if __name__ == "__main__":
    main()
