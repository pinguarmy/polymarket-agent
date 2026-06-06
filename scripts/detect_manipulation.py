#!/usr/bin/env python3
"""Find ALL manipulated markets with perfect hindsight.

Uses full window data (cheats by looking at future) to identify:
1. Was there genuine price divergence between BTC and Up token?
2. Was it strong/manipulated or just normal noise?
3. Did our bot detect it? Did it miss any?
"""

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# Our bot's thresholds for comparison
OUR_MIN_BTC = 7.0   # Minimum BTC move to consider (abs)
OUR_YES_LOW = 0.25   # YES ≤ this → BUY_NO signal
OUR_YES_HIGH = 0.45  # YES ≥ this → BUY_YES signal
OUR_ENTRY_DELAY = 20.0

# Perfect-hindsight thresholds (generous — catch everything)
MIN_BTC_MOVE = 3.0      # Any BTC move > $3
MIN_YES_MOVE = 0.03     # Any YES token move > 0.03
DIVERGENCE_MIN = 0.05   # BTC and YES must diverge by at least this much


def load_window_data(db, slug, window_start):
    """Load full window data: BTC ticks + price snapshots."""
    window_end = window_start + 300
    start_str = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_str = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = db.cursor()

    # BTC ticks
    cur.execute("SELECT timestamp, price FROM binance_btc_ticks WHERE timestamp >= ? AND timestamp <= ? ORDER BY rowid",
                (start_str, end_str))
    btc_ticks = [(r[0], float(r[1])) for r in cur.fetchall()]

    # YES price snapshots
    cur.execute("SELECT market_id FROM markets WHERE slug=?", (slug,))
    row = cur.fetchone()
    yes_snapshots = []
    no_snapshots = []
    if row:
        mkt_id = row[0]
        cur.execute(
            "SELECT timestamp, midpoint, best_bid, best_ask FROM polymarket_price_snapshots "
            "WHERE market_id=? AND timestamp >= ? AND timestamp <= ? ORDER BY rowid",
            (mkt_id, start_str, end_str),
        )
        for r in cur.fetchall():
            yes_snapshots.append((r[0], float(r[1]) if r[1] else 0, float(r[2]) if r[2] else 0, float(r[3]) if r[3] else 0))

    # Settlement
    cur.execute("SELECT resolution FROM markets WHERE slug=?", (slug,))
    res_row = cur.fetchone()
    settlement = res_row[0] if res_row and res_row[0] else None

    # If not in DB, query Gamma
    if not settlement:
        import urllib.request
        url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list):
                data = data[0] if data else {}
            closed = data.get("closed", False)
            prices_raw = data.get("outcomePrices")
            outcomes_raw = data.get("outcomes")
            if prices_raw and outcomes_raw:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                prices = [float(p) for p in prices]
                if closed and max(prices) >= 0.99:
                    settlement = outcomes[prices.index(max(prices))]
                    try:
                        cur.execute("UPDATE markets SET resolution=? WHERE slug=?", (settlement, slug))
                        db.commit()
                    except Exception:
                        pass
        except Exception:
            pass

    return {
        "btc_ticks": btc_ticks,
        "yes_snapshots": yes_snapshots,
        "settlement": settlement,
    }


def analyze_manipulation(slug, data):
    """With perfect hindsight: was this market manipulated?"""
    btc_ticks = data["btc_ticks"]
    yes_snapshots = data["yes_snapshots"]
    settlement = data["settlement"]

    if len(btc_ticks) < 5 or len(yes_snapshots) < 3:
        return {"slug": slug, "status": "insufficient_data"}

    btc_open = btc_ticks[0][1]
    btc_prices = [t[1] for t in btc_ticks]
    btc_min = min(btc_prices)
    btc_max = max(btc_prices)
    btc_range = btc_max - btc_min
    btc_close = btc_prices[-1]

    yes_prices = [s[1] for s in yes_snapshots if s[1] > 0.001 and s[1] < 0.999]
    if len(yes_prices) < 3:
        return {"slug": slug, "status": "insufficient_yes_data"}

    yes_open = yes_prices[0]
    yes_min = min(yes_prices)
    yes_max = max(yes_prices)
    yes_close = yes_prices[-1]
    yes_range = yes_max - yes_min

    # ── Manipulation Detection (perfect hindsight) ──
    # Find the MAXIMUM divergence between BTC direction and YES price
    # at ANY point in the window (not just at entry time)

    manipulations_found = []

    # Method 1: Peak divergence analysis
    # Sample BTC and YES at aligned timestamps
    # For each BTC tick, find the nearest YES snapshot
    snap_idx = 0
    best_divergence = 0
    best_divergence_type = None
    best_divergence_time = None
    btc_at_divergence = 0
    yes_at_divergence = 0

    for btc_ts_str, btc_price in btc_ticks:
        # Parse time
        if isinstance(btc_ts_str, str):
            tick_dt = datetime.fromisoformat(btc_ts_str.replace("Z", "+00:00"))
        else:
            continue
        elapsed = tick_dt.timestamp() - (int(slug.split("-")[-1]))

        if elapsed < 5 or elapsed > 285:  # Skip first 5s (noise) and last 15s (fake spikes)
            continue

        btc_change = btc_price - btc_open

        # Find nearest YES snapshot
        while snap_idx < len(yes_snapshots) - 1 and yes_snapshots[snap_idx][0] <= btc_ts_str:
            snap_idx += 1
        nearest = yes_snapshots[min(snap_idx, len(yes_snapshots) - 1)]
        yes_mid = nearest[1]
        if yes_mid <= 0.001 or yes_mid >= 0.999:
            continue

        # Divergence: BTC up + YES low = manipulation for NO
        if btc_change > MIN_BTC_MOVE and yes_mid < (0.5 - DIVERGENCE_MIN):
            divergence = abs(btc_change) + (0.5 - yes_mid) * 100  # Combined score
            if divergence > best_divergence:
                best_divergence = divergence
                best_divergence_type = "MANIP_NO"
                best_divergence_time = elapsed
                btc_at_divergence = btc_change
                yes_at_divergence = yes_mid

        # Divergence: BTC down + YES high = manipulation for YES
        elif btc_change < -MIN_BTC_MOVE and yes_mid > (0.5 + DIVERGENCE_MIN):
            divergence = abs(btc_change) + (yes_mid - 0.5) * 100
            if divergence > best_divergence:
                best_divergence = divergence
                best_divergence_type = "MANIP_YES"
                best_divergence_time = elapsed
                btc_at_divergence = btc_change
                yes_at_divergence = yes_mid

    # Method 2: Overall window analysis (EXCLUDE last 15s to avoid settlement distortion)
    # BTC direction vs YES direction
    btc_up_overall = btc_close > btc_open
    # Use mid-window YES price (not last which is distorted by settlement)
    mid_window_yes = [p for p in yes_prices if 0.05 < p < 0.95]
    yes_up_overall = (mid_window_yes[-1] > mid_window_yes[0]) if len(mid_window_yes) >= 2 else (yes_close > yes_open)

    overall_divergence = False
    overall_direction = None
    if btc_up_overall and not yes_up_overall and (btc_max - btc_min) > MIN_BTC_MOVE:
        overall_divergence = True
        overall_direction = "MANIP_NO"
    elif not btc_up_overall and yes_up_overall and (btc_max - btc_min) > MIN_BTC_MOVE:
        overall_divergence = True
        overall_direction = "MANIP_YES"

    # Method 3: Was the settlement winner the "wrong" one?
    if settlement:
        if settlement == "Up":
            actual_winner = "Up"
            manip_would_have_bought = "NO"  # manipulator would lose if buying NO
        else:
            actual_winner = "Down"
            manip_would_have_bought = "YES"

    # Determine final verdict
    # Require actual peak divergence > 0 (exclude settlement-only detections)
    is_manipulated = (best_divergence > 5) or (overall_divergence and best_divergence > 0)

    # If manipulated, which direction was correct?
    manip_direction = best_divergence_type or overall_direction
    if manip_direction == "MANIP_NO":
        manip_win = settlement == "Down"  # manip bought NO, NO won
    elif manip_direction == "MANIP_YES":
        manip_win = settlement == "Up"    # manip bought YES, YES won
    else:
        manip_win = None

    result = {
        "slug": slug,
        "status": "analyzed",
        "btc_open": round(btc_open, 2),
        "btc_range": round(btc_range, 2),
        "btc_close": round(btc_close, 2),
        "yes_open": round(yes_open, 3),
        "yes_range": round(yes_range, 3),
        "yes_close": round(yes_close, 3),
        "settlement": settlement or "?",
        "is_manipulated": is_manipulated,
        "manip_direction": manip_direction,
        "peak_divergence": round(best_divergence, 2) if best_divergence else 0,
        "peak_btc": round(btc_at_divergence, 1) if btc_at_divergence else 0,
        "peak_yes": round(yes_at_divergence, 3) if yes_at_divergence else 0,
        "peak_time": round(best_divergence_time, 0) if best_divergence_time else 0,
        "overall_divergence": overall_divergence,
        "overall_direction": overall_direction,
        "manip_win": manip_win,
        "confident": best_divergence > 15 or overall_divergence,  # high confidence
    }

    return result


def check_our_bot_detected(slug, result):
    """Check if our bot would have detected this manipulation."""
    # This simulates our bot's detection logic
    peak_btc = result.get("peak_btc", 0)
    peak_yes = result.get("peak_yes", 0)
    direction = result.get("manip_direction")

    if not direction:
        return False

    if direction == "MANIP_NO":
        return peak_btc > OUR_MIN_BTC and peak_yes <= OUR_YES_LOW
    elif direction == "MANIP_YES":
        return peak_btc < -OUR_MIN_BTC and peak_yes >= OUR_YES_HIGH

    return False


def main():
    start_ts = 1777766100  # ~00:00 UTC May 3
    end_ts = int(datetime.now().timestamp())

    print(f"Manipulation Analysis from {datetime.fromtimestamp(start_ts, tz=timezone.utc)}")
    print(f"                     to {datetime.fromtimestamp(end_ts, tz=timezone.utc)}")
    print()

    db = sqlite3.connect(str(PROJECT / "data" / "btc5m.db"))

    # Process all windows
    window_start = (start_ts // 300) * 300
    all_results = []

    while window_start + 300 <= end_ts:
        slug = f"btc-updown-5m-{window_start}"
        data = load_window_data(db, slug, window_start)
        result = analyze_manipulation(slug, data)
        if result["status"] == "analyzed":
            result["our_bot_detected"] = check_our_bot_detected(slug, result)
            result["time"] = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%H:%M")
            all_results.append(result)
        window_start += 300

    # ── Summary ──
    manipulated = [r for r in all_results if r["is_manipulated"]]
    not_manipulated = [r for r in all_results if not r["is_manipulated"]]
    our_detected = [r for r in manipulated if r["our_bot_detected"]]
    our_missed = [r for r in manipulated if not r["our_bot_detected"]]
    confident = [r for r in manipulated if r["confident"]]

    print(f"Total windows: {len(all_results)}")
    print(f"Manipulated (any): {len(manipulated)}")
    print(f"  → Confident: {len(confident)}")
    print(f"Not manipulated: {len(not_manipulated)}")
    print()
    print(f"Our bot detected: {len(our_detected)}/{len(manipulated)}")
    print(f"Our bot MISSED:   {len(our_missed)}/{len(manipulated)}")
    print()

    # ── Detail: manipulated markets our bot missed ──
    if our_missed:
        print("=== MISSED MANIPULATIONS ===")
        print(f"{'Time':6s} {'BTC range':>10s} {'YES range':>10s} {'Settle':8s} {'Type':12s} {'Peak BTC':>9s} {'Peak YES':>9s} {'Why missed':>25s}")
        print("-" * 90)
        for r in sorted(our_missed, key=lambda x: x["time"]):
            why = ""
            if r["manip_direction"] == "MANIP_NO":
                if r["peak_btc"] <= OUR_MIN_BTC:
                    why = f"BTC ${r['peak_btc']:.0f} < ${OUR_MIN_BTC:.0f}"
                elif r["peak_yes"] > OUR_YES_LOW:
                    why = f"YES {r['peak_yes']:.3f} > {OUR_YES_LOW:.2f}"
            elif r["manip_direction"] == "MANIP_YES":
                if abs(r["peak_btc"]) <= OUR_MIN_BTC:
                    why = f"BTC ${abs(r['peak_btc']):.0f} < ${OUR_MIN_BTC:.0f}"
                elif r["peak_yes"] < OUR_YES_HIGH:
                    why = f"YES {r['peak_yes']:.3f} < {OUR_YES_HIGH:.2f}"
            marker = "✅" if r["confident"] else "⚠️"
            print(f"{r['time']:6s} ${r['btc_range']:>6.1f}  ${r['yes_range']:>6.3f} {r['settlement']:8s} {r['manip_direction']:12s} ${r['peak_btc']:>+7.1f} {r['peak_yes']:>9.3f} {marker} {why[:25]:>25s}")

    print()

    # ── Detail: confident manipulations ──
    print("=== CONFIDENT MANIPULATIONS ===")
    print(f"{'Time':6s} {'Type':12s} {'Settle':8s} {'Peak BTC':>9s} {'Peak YES':>9s} {'Our bot':>8s} {'Divergence':>11s}")
    print("-" * 70)
    for r in sorted(confident, key=lambda x: x["time"]):
        detected = "✅" if r["our_bot_detected"] else "❌ MISS"
        print(f"{r['time']:6s} {r['manip_direction']:12s} {r['settlement']:8s} ${r['peak_btc']:>+7.1f} {r['peak_yes']:>9.3f} {detected:>8s} {r['peak_divergence']:>10.1f}")

    print()

    # ── Manipulator win rate ──
    manip_wins = [r for r in manipulated if r["manip_win"] is True]
    manip_losses = [r for r in manipulated if r["manip_win"] is False]
    manip_unknown = [r for r in manipulated if r["manip_win"] is None]
    print(f"Manipulator success rate: {len(manip_wins)}W / {len(manip_losses)}L / {len(manip_unknown)}?")
    if manip_wins or manip_losses:
        print(f"  Win rate: {len(manip_wins)/(len(manip_wins)+len(manip_losses))*100:.1f}%")

    db.close()


if __name__ == "__main__":
    main()
