#!/usr/bin/env python3
"""Validate Chainlink BTC/USD alignment with Polymarket BTC Up/Down 5-min markets.

Reads recently closed markets from Gamma API, queries chainlink_btc_ticks DB table
for start/end prices, and checks whether Chainlink data can correctly predict
the official resolution outcome.

Usage:
    python3 scripts/validate_btc_market_alignment.py
    python3 scripts/validate_btc_market_alignment.py --markets 10
    python3 scripts/validate_btc_market_alignment.py --db data/btc5m.db
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from db import Database
from chainlink_helpers import (
    get_chainlink_price_at_or_before,
    get_chainlink_price_nearest,
    get_latest_chainlink_price,
)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def gamma_get(path: str) -> Optional[dict]:
    """GET from Gamma API with error handling."""
    url = f"{GAMMA_API}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polymarket/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  [GAMMA ERROR] {url}: {e}")
        return None


def find_recent_closed_markets(limit: int = 10) -> list[dict]:
    """Find recently closed BTC Up/Down 5-min markets via Gamma.

    Gamma API doesn't have a clean 'closed' filter, so we:
    1. Query generic markets endpoint
    2. Filter by slug pattern btc-updown-5m-
    3. Filter by outcomePrices being final (0.0 or 1.0)
    4. Sort by close_time descending
    """
    # Query a large range of slugs
    now_ts = int(time.time())
    # Start from ~1 hour ago, go back ~24 hours
    earliest = now_ts - 86400

    closed = []
    # Only query the most recent ~30 minutes to avoid rate limiting
    current = (now_ts // 300) * 300
    # Start from current window and go back max 30 slugs = 2.5 hours
    max_lookback = min(30, (now_ts - earliest) // 300)
    for i in range(max_lookback):
        slug = f"btc-updown-5m-{current}"
        data = gamma_get(f"/markets?slug={slug}")
        time.sleep(0.1)  # rate limit: 10 queries/sec max
        if data and isinstance(data, list) and len(data) > 0:
            m = data[0]
            # Check if resolved
            op = m.get("outcomePrices")
            if isinstance(op, str):
                try:
                    op = json.loads(op)
                except Exception:
                    op = None
            if op and isinstance(op, list) and len(op) >= 2:
                try:
                    yes_price = float(op[0])
                    no_price = float(op[1])
                    # Resolved if outcome is final (0 or 1)
                    if yes_price in (0.0, 1.0) or no_price in (0.0, 1.0):
                        yes_won = yes_price > 0.5
                        closed.append({
                            "slug": slug,
                            "window_start": current,
                            "window_end": current + 300,
                            "open_time": m.get("open_time") or m.get("start_time"),
                            "close_time": m.get("close_time") or m.get("end_time"),
                            "outcome_prices": op,
                            "yes_won": yes_won,
                            "question": m.get("question", ""),
                            "condition_id": m.get("conditionId"),
                            "clob_token_ids": m.get("clobTokenIds"),
                            "volume": m.get("volume"),
                            "raw_outcomePrices_raw": m.get("outcomePrices"),
                        })
                except (ValueError, TypeError):
                    pass

        current -= 300  # Go back 5 min

    # Sort by most recent first
    closed.sort(key=lambda m: m["window_start"], reverse=True)
    return closed[:limit]


def get_official_outcome(slug: str) -> Optional[dict]:
    """Fetch the official market resolution data.

    Checks for:
    - outcomePrices from Gamma (final settlement)
    - Any 'price_to_beat' or 'target_price' in metadata
    """
    data = gamma_get(f"/markets?slug={slug}")
    if not data or not isinstance(data, list) or len(data) == 0:
        return None

    m = data[0]
    op = m.get("outcomePrices")
    if isinstance(op, str):
        op = json.loads(op)

    result = {
        "slug": slug,
        "question": m.get("question", ""),
        "condition_id": m.get("conditionId"),
        "clob_token_ids": m.get("clobTokenIds"),
        "outcome_prices": op,
        "open_time": m.get("open_time") or m.get("start_time"),
        "close_time": m.get("close_time") or m.get("end_time"),
        "volume": m.get("volume"),
        "active": m.get("active"),
        "closed": m.get("closed"),
        "end_date": m.get("endDate"),
        "yes_won": float(op[0]) > 0.5 if op and len(op) >= 2 else None,
        # Additional metadata
        "raw_response_keys": list(m.keys()),
    }
    return result


def analyze_market(db, market: dict) -> dict:
    """Analyze a single market: compare Chainlink prices vs official outcome."""
    ws = market["window_start"]
    we = market["window_end"]
    ws_ms = ws * 1000
    we_ms = we * 1000
    spread_px = 5  # tolerance in seconds

    result = {
        "slug": market["slug"],
        "window_start_utc": datetime.fromtimestamp(ws, tz=timezone.utc).strftime("%H:%M:%S"),
        "window_end_utc": datetime.fromtimestamp(we, tz=timezone.utc).strftime("%H:%M:%S"),
        "official_yes_won": market["yes_won"],
        "outcome_prices": market["outcome_prices"],
        "volume": market.get("volume"),
    }

    # Official outcome
    result["official_outcome"] = "UP (YES)" if market["yes_won"] else "DOWN (NO)"

    # Chainlink start price candidates at various tolerances
    for tol_s in [5, 10, 30]:
        tol_ms = tol_s * 1000
        # at_or_before start
        at_before = get_chainlink_price_at_or_before(db, ws_ms)
        nearest = get_chainlink_price_nearest(db, ws_ms, tolerance_ms=tol_ms)

        result[f"cl_start_at_or_before_value"] = (
            at_before["value_normalized"] if at_before else None
        )
        result[f"cl_start_at_or_before_dev"] = (
            (at_before["source_timestamp_ms"] - ws_ms) / 1000.0
            if at_before and at_before.get("source_timestamp_ms")
            else None
        )
        result[f"cl_start_nearest_{tol_s}s_value"] = (
            nearest["value_normalized"] if nearest else None
        )
        result[f"cl_start_nearest_{tol_s}s_dev"] = (
            nearest.get("deviation_ms", 0) / 1000.0 if nearest else None
        )

        # Chainlink end price candidates at various tolerances
        at_before_end = get_chainlink_price_at_or_before(db, we_ms)
        nearest_end = get_chainlink_price_nearest(db, we_ms, tolerance_ms=tol_ms)

        result[f"cl_end_at_or_before_value"] = (
            at_before_end["value_normalized"] if at_before_end else None
        )
        result[f"cl_end_at_or_before_dev"] = (
            (we_ms - at_before_end["source_timestamp_ms"]) / 1000.0
            if at_before_end and at_before_end.get("source_timestamp_ms")
            else None
        )
        result[f"cl_end_nearest_{tol_s}s_value"] = (
            nearest_end["value_normalized"] if nearest_end else None
        )
        result[f"cl_end_nearest_{tol_s}s_dev"] = (
            nearest_end.get("deviation_ms", 0) / 1000.0 if nearest_end else None
        )

        # Predict outcome using start_at_or_before / end_nearest
        start_px = at_before["value_normalized"] if at_before else None
        end_px = nearest_end["value_normalized"] if nearest_end else None
        if start_px is not None and end_px is not None:
            predicted_yes = end_px > start_px
            result[f"predicted_{tol_s}s"] = "UP (YES)" if predicted_yes else "DOWN (NO)"
            result[f"match_{tol_s}s"] = predicted_yes == market["yes_won"]
            result[f"btc_change_{tol_s}s"] = round(end_px - start_px, 2)
        else:
            result[f"predicted_{tol_s}s"] = None
            result[f"match_{tol_s}s"] = None
            result[f"btc_change_{tol_s}s"] = None

    # Also check: end_nearest_5s + start_nearest_5s
    s5 = get_chainlink_price_nearest(db, ws_ms, tolerance_ms=5000)
    e5 = get_chainlink_price_nearest(db, we_ms, tolerance_ms=5000)
    if s5 and e5:
        result["predicted_nearest_5s"] = "UP (YES)" if e5["value_normalized"] > s5["value_normalized"] else "DOWN (NO)"
        result["match_nearest_5s"] = (e5["value_normalized"] > s5["value_normalized"]) == market["yes_won"]
        result["btc_change_nearest_5s"] = round(e5["value_normalized"] - s5["value_normalized"], 2)
    else:
        result["predicted_nearest_5s"] = None
        result["match_nearest_5s"] = None
        result["btc_change_nearest_5s"] = None

    # Also check: at_or_before start + at_or_before end
    sab = get_chainlink_price_at_or_before(db, ws_ms)
    eab = get_chainlink_price_at_or_before(db, we_ms)
    if sab and eab:
        result["predicted_at_or_before"] = "UP (YES)" if eab["value_normalized"] > sab["value_normalized"] else "DOWN (NO)"
        result["match_at_or_before"] = (eab["value_normalized"] > sab["value_normalized"]) == market["yes_won"]
        result["btc_change_at_or_before"] = round(eab["value_normalized"] - sab["value_normalized"], 2)
    else:
        result["predicted_at_or_before"] = None
        result["match_at_or_before"] = None
        result["btc_change_at_or_before"] = None

    return result


def print_alignment_table(results: list[dict]):
    """Print a clean alignment table."""
    print()
    print(f"{'='*120}")
    print(f"CHAINLINK ↔ POLYMARKET ALIGNMENT TABLE")
    print(f"{'='*120}")
    print(f"{'Market':>25} | {'Window':>12} | {'Official':>10} | {'CL Start':>10} | {'CL End':>10} | {'BTC Δ':>8} | {'Predicted':>10} | {'Match?':>8}")
    print(f"{'-'*25}-+-{'-'*12}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}-+-{'-'*8}-+-{'-'*10}-+-{'-'*8}")

    matches = []
    for r in results:
        slug_short = r["slug"][-7:]
        window = f"{r['window_start_utc']}→{r['window_end_utc']}"
        official = r["official_outcome"]
        cl_start = r.get("cl_start_nearest_5s_value")
        cl_end = r.get("cl_end_nearest_5s_value")
        btc_delta = r.get("btc_change_nearest_5s")
        predicted = r.get("predicted_nearest_5s", "N/A")
        match = r.get("match_nearest_5s")

        start_str = f"${cl_start:.2f}" if cl_start else "N/A"
        end_str = f"${cl_end:.2f}" if cl_end else "N/A"
        delta_str = f"${btc_delta:+.2f}" if btc_delta is not None else "N/A"
        match_str = "✅" if match else "❌" if match is not None else "N/A"

        print(f"{slug_short:>25} | {window:>12} | {official:>10} | {start_str:>10} | {end_str:>10} | {delta_str:>8} | {predicted or 'N/A':>10} | {match_str:>8}")
        if match is not None:
            matches.append(match)

    print(f"{'-'*120}")
    if matches:
        correct = sum(1 for m in matches if m)
        print(f"Accuracy (nearest 5s): {correct}/{len(matches)} = {correct/len(matches)*100:.0f}%")
    print()


def print_detailed_analysis(results: list[dict]):
    """Print detailed analysis for each market, including tolerance comparison."""
    print()
    print(f"{'='*120}")
    print(f"DETAILED ANALYSIS")
    print(f"{'='*120}")

    for r in results:
        print(f"\n--- {r['slug']} ({r['window_start_utc']} → {r['window_end_utc']}) ---")
        print(f"  Official: {r['official_outcome']}  |  Outcome prices: {r['outcome_prices']}  |  Volume: {r.get('volume', 'N/A')}")

        # Chainlink data availability
        has_start = r.get("cl_start_nearest_5s_value") is not None
        has_end = r.get("cl_end_nearest_5s_value") is not None
        print(f"  Chainlink: start={has_start} end={has_end}")

        if has_start and has_end:
            start_v = r["cl_start_nearest_5s_value"]
            end_v = r["cl_end_nearest_5s_value"]
            start_dev = r.get("cl_start_nearest_5s_dev", "N/A")
            end_dev = r.get("cl_end_nearest_5s_dev", "N/A")
            print(f"  CL start: ${start_v:.2f} (dev={start_dev}s)  CL end: ${end_v:.2f} (dev={end_dev}s)")
            print(f"  BTC change: ${r['btc_change_nearest_5s']:+.2f}")
            print(f"  Match (5s): {r.get('match_nearest_5s')}")
        elif has_start:
            print(f"  CL start: ${r['cl_start_nearest_5s_value']:.2f}  CL end: NO DATA")
        elif has_end:
            print(f"  CL start: NO DATA  CL end: ${r['cl_end_nearest_5s_value']:.2f}")
        else:
            print(f"  NO CHAINLINK DATA")

        # Tolerance comparison
        print(f"  Tolerance comparison:")
        for tol in [5, 10, 30]:
            match = r.get(f"match_{tol}s")
            pred = r.get(f"predicted_{tol}s")
            btc = r.get(f"btc_change_{tol}s")
            match_str = "✅" if match else "❌" if match is not None else "N/A"
            btc_str = f"${btc:+.2f}" if btc is not None else "N/A"
            pred_str = str(pred) if pred is not None else "N/A"
            print(f"    {tol}s nearest: {pred_str:>12} | Δ{btc_str:>8} | {match_str}")

        # at_or_before
        ab = r.get("match_at_or_before")
        ab_str = "✅" if ab else "❌" if ab is not None else "N/A"
        ab_pred = r.get("predicted_at_or_before", "N/A")
        ab_btc = r.get("btc_change_at_or_before")
        ab_btc_str = f"${ab_btc:+.2f}" if ab_btc is not None else "N/A"
        print(f"    at_or_before: {ab_pred:>12} | Δ{ab_btc_str:>8} | {ab_str}")


def print_conclusions(results: list[dict]):
    """Print actionable conclusions."""
    print()
    print(f"{'='*120}")
    print(f"CONCLUSIONS")
    print(f"{'='*120}")

    # Count matches by method
    methods = {
        "at_or_before": [],
        "nearest_5s": [],
        "nearest_10s": [],
        "nearest_30s": [],
    }

    for r in results:
        for method in methods:
            key = f"match_{method.replace('nearest_', '')}" if method != "nearest_5s" else "match_nearest_5s"
            if method == "at_or_before":
                key = "match_at_or_before"
            elif method == "nearest_10s":
                key = "match_10s"
            elif method == "nearest_30s":
                key = "match_30s"
            val = r.get(key)
            if val is not None:
                methods[method].append(val)

    print(f"\nPrediction accuracy by method:")
    for method, vals in methods.items():
        if vals:
            correct = sum(1 for v in vals if v)
            print(f"  {method:>20s}: {correct}/{len(vals)} = {correct/len(vals)*100:.0f}%")

    # Determine winner
    best_method = None
    best_acc = 0
    for method, vals in methods.items():
        if vals:
            acc = sum(1 for v in vals if v) / len(vals)
            if acc > best_acc:
                best_acc = acc
                best_method = method

    print(f"\nBest method: {best_method} ({best_acc*100:.0f}%)")

    # Data sufficiency check
    markets_with_chainlink = sum(1 for r in results if r.get("cl_start_nearest_5s_value") is not None and r.get("cl_end_nearest_5s_value") is not None)
    print(f"\nMarkets with sufficient Chainlink data: {markets_with_chainlink}/{len(results)}")
    if markets_with_chainlink < len(results):
        print(f"  ⚠ Some markets lack Chainlink data (RTDS collector started recently)")

    # Final readiness check
    if best_acc >= 90 and markets_with_chainlink >= len(results) * 0.8:
        print(f"\n✅ READY: Chainlink settlement is reliable enough to replace Binance")
    elif best_acc >= 70:
        print(f"\n🟡 CONDITIONAL: Chainlink is usable but may have edge cases")
    else:
        print(f"\n❌ NOT READY: More data needed before switching from Binance")

    print(f"\nRecommended method: {best_method}")
    if best_method == "nearest_5s":
        print(f"  → Use get_chainlink_price_nearest(db, window_end_ms, tolerance_ms=5000)")
    elif best_method == "at_or_before":
        print(f"  → Use get_chainlink_price_at_or_before(db, window_end_ms)")
    elif best_method == "nearest_10s":
        print(f"  → Use get_chainlink_price_nearest(db, window_end_ms, tolerance_ms=10000)")

    print(f"\nFields to add to paper_trades.jsonl:")
    print(f"  - chainlink_start_price (float)")
    print(f"  - chainlink_end_price (float)")
    print(f"  - chainlink_start_timestamp_ms (int)")
    print(f"  - chainlink_end_timestamp_ms (int)")
    print(f"  - chainlink_settlement_method (str: 'nearest_5s' | 'at_or_before' | 'nearest_10s')")
    print(f"  - settlement_source (str: 'chainlink_rtds')")

    print(f"\nNext step: Can switch paper trade settlement from Binance to Chainlink?"),
    if best_acc >= 90:
        print(f"  ✅ YES — proceed to modify paper_trader and backtest settlement")
    else:
        print(f"  🟡 WAIT — collect more data first")


def main():
    parser = argparse.ArgumentParser(description="Validate Chainlink ↔ Polymarket BTC Up/Down alignment")
    parser.add_argument("--db", default="data/btc5m.db", help="SQLite DB path")
    parser.add_argument("--markets", type=int, default=10, help="Number of closed markets to check")
    parser.add_argument("--min-volume", type=float, default=0, help="Minimum volume filter")
    args = parser.parse_args()

    db = Database(args.db)
    db.init_schema()

    # Check if we have Chainlink data
    with db.get_connection() as conn:
        cl_count = conn.execute("SELECT COUNT(*) as c FROM chainlink_btc_ticks").fetchone()["c"]
    if cl_count == 0:
        print("⚠ No Chainlink data in DB. Run validate_chainlink.py first to collect data.")
        print("   python3 scripts/validate_chainlink.py --duration 300")
        sys.exit(1)
    print(f"Chainlink ticks in DB: {cl_count}")

    # Find closed markets
    print(f"\nSearching for recently closed BTC Up/Down markets...")
    markets = find_recent_closed_markets(limit=args.markets)

    if not markets:
        print("❌ No closed markets found. Gamma API may be down or markets expired.")
        print("   Trying to look up specific known slugs...")
        # Fallback: try specific recent slugs
        now_ts = int(time.time())
        for i in range(20):
            ts = (now_ts // 300) * 300 - (i * 300)
            slug = f"btc-updown-5m-{ts}"
            official = get_official_outcome(slug)
            if official and official.get("yes_won") is not None:
                markets.append({
                    "slug": slug,
                    "window_start": ts,
                    "window_end": ts + 300,
                    "yes_won": official["yes_won"],
                    "outcome_prices": official["outcome_prices"],
                    "volume": official.get("volume"),
                })
                if len(markets) >= args.markets:
                    break

    if not markets:
        print("❌ Still no closed markets. Cannot validate.")
        sys.exit(1)

    print(f"Found {len(markets)} closed markets")

    # Analyze each market
    results = []
    for m in markets:
        r = analyze_market(db, m)
        results.append(r)

    # Output
    print_alignment_table(results)
    print_detailed_analysis(results)
    print_conclusions(results)

    # Summary for audit
    print()
    print(f"{'='*120}")
    print(f"AUDIT SUMMARY")
    print(f"{'='*120}")
    print(f"Total Chainlink ticks: {cl_count}")
    print(f"Closed markets analyzed: {len(results)}")
    print(f"Markets with Chainlink data: {sum(1 for r in results if r.get('cl_start_nearest_5s_value') is not None)}")


if __name__ == "__main__":
    main()
