"""Polymarket Agent — Phase 3: Trading Signals.

Detects actionable market conditions and generates structured signal
entries. ALL signals require human confirmation by default. No auto-trading.

Each signal is a structured dict with:
  - market, token, current_price, trigger_reason
  - suggested_action, confidence, max_suggested_size
  - risks, human_confirmation_required (always True)

Usage:
    from signals import run_all_signals
    signals = run_all_signals("will-the-fed-cut-rates-in-2026")
    for s in signals:
        print(s["trigger_reason"])
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from market_data import (
    get_market_summary,
    get_market_by_slug,
    get_price_history,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _signal_template(
    signal_type: str,
    market_slug: str,
    token_id: str,
    side: str,
    current_price: float,
    trigger_reason: str,
    suggested_action: str,
    suggested_size: float = 5.0,
    confidence: float = 0.5,
    risks: str = "",
) -> dict:
    """Create a standardized signal dict."""
    return {
        "timestamp": _now_iso(),
        "signal_type": signal_type,
        "market_slug": market_slug,
        "token_id": token_id,
        "side": side,
        "current_price": current_price,
        "trigger_reason": trigger_reason,
        "suggested_action": suggested_action,
        "suggested_size": suggested_size,
        "confidence": confidence,
        "expected_edge": None,
        "risks": risks,
        "human_confirmation_required": True,
        "acted_upon": False,
        "paper_trade_ref": None,
    }


# ── Task 3.1: Price Anomaly Detector ───────────────────────────────

def detect_price_anomaly(
    slug: str,
    threshold_pct: float = 5.0,
    lookback_minutes: int = 60,
) -> list[dict]:
    """Detect significant price movements in a market.

    Compares current price to historical prices from the CLOB price
    history endpoint. Triggers if the price has moved more than
    threshold_pct in the last lookback_minutes.

    Args:
        slug: Market slug
        threshold_pct: Percentage change threshold (default 5.0 = 5%)
        lookback_minutes: How far back to look (default 60)

    Returns:
        list of signal dicts (empty if no anomaly detected)
    """
    market = get_market_by_slug(slug)
    if market is None:
        return []

    # Get recent price history
    history = get_price_history(
        market["condition_id"],
        interval="1d",
        fidelity=60,  # 60 data points for ~1 point per minute in a 1d window
    )

    if not history or len(history) < 2:
        return []

    current_price = float(history[-1]["p"])
    # Find price from lookback_minutes ago
    # history is sorted by time ascending
    lookback_ts = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    lookback_ts_unix = lookback_ts.timestamp()

    # Find closest historical data point
    prev_price = None
    for point in history:
        if point["t"] <= lookback_ts_unix:
            prev_price = float(point["p"])
        else:
            break

    if prev_price is None:
        # Use earliest available if lookback exceeds history
        prev_price = float(history[0]["p"])

    change_pct = ((current_price - prev_price) / prev_price) * 100 if prev_price else 0
    abs_change = abs(change_pct)

    if abs_change < threshold_pct:
        return []

    direction = "up" if change_pct > 0 else "down"
    yes_token = market["yes_token_id"]

    # Trade against the move (mean reversion assumption)
    if change_pct > 0:
        suggested_action = "SELL YES"
        side = "YES"
    else:
        suggested_action = "BUY YES"
        side = "YES"

    # Confidence scales with magnitude of the move
    confidence = min(0.3 + abs_change / 20, 0.8)

    signal = _signal_template(
        signal_type="price_anomaly",
        market_slug=slug,
        token_id=yes_token,
        side=side,
        current_price=current_price,
        trigger_reason=(
            f"Price moved {direction} {abs_change:.1f}% in "
            f"{lookback_minutes} minutes (threshold: {threshold_pct}%). "
            f"From {prev_price:.4f} to {current_price:.4f}."
        ),
        suggested_action=suggested_action,
        suggested_size=5.0,
        confidence=confidence,
        risks="Mean reversion may not occur if price move is information-driven, not noise.",
    )

    return [signal]


# ── Task 3.2: Spread Detector ──────────────────────────────────────

def detect_wide_spread(
    slug: str,
    threshold_pct: float = 5.0,
) -> list[dict]:
    """Detect abnormally wide bid-ask spreads.

    Triggers if the spread on either the YES or NO token exceeds
    threshold_pct.

    Args:
        slug: Market slug
        threshold_pct: Spread threshold as percentage (default 5.0 = 5%)

    Returns:
        list of signal dicts (one per token with wide spread)
    """
    summary = get_market_summary(slug)
    if summary is None:
        return []

    signals = []

    # Check YES token spread
    yes_spread = summary.get("yes_spread_pct")
    if yes_spread is not None and yes_spread > threshold_pct:
        signals.append(_signal_template(
            signal_type="wide_spread",
            market_slug=slug,
            token_id=summary["yes_token_id"],
            side="YES",
            current_price=summary["yes_price"] or 0,
            trigger_reason=(
                f"YES token spread is {yes_spread:.1f}% "
                f"(threshold: {threshold_pct}%). "
                f"Bid: {summary['yes_best_bid']}, Ask: {summary['yes_best_ask']}."
            ),
            suggested_action="CAUTION — do not trade",
            suggested_size=0,
            confidence=0.8,
            risks="Wide spreads indicate illiquidity. Fills may be poor even with limit orders.",
        ))

    # Check NO token spread
    no_spread = summary.get("no_spread_pct")
    if no_spread is not None and no_spread > threshold_pct:
        signals.append(_signal_template(
            signal_type="wide_spread",
            market_slug=slug,
            token_id=summary["no_token_id"],
            side="NO",
            current_price=summary["no_price"] or 0,
            trigger_reason=(
                f"NO token spread is {no_spread:.1f}% "
                f"(threshold: {threshold_pct}%). "
                f"Bid: {summary['no_best_bid']}, Ask: {summary['no_best_ask']}."
            ),
            suggested_action="CAUTION — do not trade",
            suggested_size=0,
            confidence=0.8,
            risks="Wide spreads indicate illiquidity. Fills may be poor even with limit orders.",
        ))

    return signals


# ── Task 3.3: Liquidity Change Detector ────────────────────────────

def detect_liquidity_change(
    slug: str,
    volume_threshold: float = 0.5,
    snapshot_dir: str = "logs/market_snapshots",
) -> list[dict]:
    """Detect significant changes in market volume/liquidity.

    Compares current volume to the most recent snapshot for this market.
    Triggers if volume has changed by more than volume_threshold (0.5 = 50%).

    Args:
        slug: Market slug
        volume_threshold: Relative change threshold (default 0.5)
        snapshot_dir: Path to stored market snapshots

    Returns:
        list of signal dicts
    """
    summary = get_market_summary(slug)
    if summary is None:
        return []

    current_volume = summary.get("volume", 0)

    # Try to find a previous snapshot for this market
    snapshot_path = Path(snapshot_dir)
    if not snapshot_path.exists():
        # No previous snapshots to compare against — no signal
        return []

    # Find most recent snapshot for this slug
    safe_slug = slug.replace("/", "_").replace(" ", "_")
    snapshots = sorted(
        snapshot_path.glob(f"**/{safe_slug}_*.json"),
        reverse=True,
    )

    if not snapshots:
        return []

    try:
        with open(snapshots[0]) as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    prev_volume = prev.get("volume", 0)
    if prev_volume == 0:
        return []

    change_ratio = (current_volume - prev_volume) / prev_volume

    if abs(change_ratio) < volume_threshold:
        return []

    direction = "increased" if change_ratio > 0 else "decreased"

    signal = _signal_template(
        signal_type="liquidity_change",
        market_slug=slug,
        token_id=summary["yes_token_id"],
        side="YES",
        current_price=summary.get("yes_price") or 0,
        trigger_reason=(
            f"Volume {direction} by {abs(change_ratio)*100:.0f}% "
            f"since last snapshot (from ${prev_volume:,.0f} to "
            f"${current_volume:,.0f}). "
            f"Current liquidity: ${summary.get('liquidity', 0):,.0f}."
        ),
        suggested_action="MONITOR — increased volume may signal information event",
        suggested_size=0,
        confidence=0.6,
        risks="Volume spikes can precede large price moves. Observe before acting.",
    )

    return [signal]


# ── Task 3.4: Expiry Reminder ──────────────────────────────────────

def detect_approaching_expiry(
    slug: str,
    days_before: int = 7,
) -> list[dict]:
    """Warn when a market is approaching its resolution date.

    Args:
        slug: Market slug
        days_before: Trigger warning this many days before resolution

    Returns:
        list of signal dicts
    """
    market = get_market_by_slug(slug)
    if market is None:
        return []

    end_date_str = market.get("end_date", "")
    if not end_date_str:
        return []

    try:
        # Parse ISO 8601 end date
        end_date = datetime.fromisoformat(
            end_date_str.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return []

    now = datetime.now(timezone.utc)
    remaining = end_date - now

    if remaining.total_seconds() < 0:
        # Already past resolution — market should be closed
        return []

    remaining_days = remaining.total_seconds() / 86400

    if remaining_days > days_before:
        return []

    signal = _signal_template(
        signal_type="approaching_expiry",
        market_slug=slug,
        token_id=market["yes_token_id"],
        side="YES",
        current_price=market.get("yes_price") or 0,
        trigger_reason=(
            f"Market resolves in {remaining_days:.1f} days "
            f"(end date: {end_date_str}). "
            f"Current YES price: {market.get('yes_price')}. "
            f"Review open positions before resolution."
        ),
        suggested_action="CHECK open positions — consider exiting before resolution",
        suggested_size=0,
        confidence=0.9,
        risks="Holding positions through resolution means you accept the settlement outcome.",
    )

    return [signal]


# ── Task 3.5: Arbitrage Checker ────────────────────────────────────

def check_arbitrage(
    slug: str,
    threshold: float = 0.05,
) -> list[dict]:
    """Check if Yes + No prices sum is significantly different from 1.0.

    In a perfect market, Yes + No = 1.0. Deviations may indicate
    arbitrage opportunities or market inefficiencies.

    Args:
        slug: Market slug
        threshold: Maximum acceptable deviation from 1.0 (default 0.05)

    Returns:
        list of signal dicts
    """
    market = get_market_by_slug(slug)
    if market is None:
        return []

    yes_price = market.get("yes_price")
    no_price = market.get("no_price")

    if yes_price is None or no_price is None:
        return []

    price_sum = yes_price + no_price
    deviation = price_sum - 1.0

    if abs(deviation) < threshold:
        return []

    if deviation > 0:
        # Market overpriced — Yes + No > 1.0
        suggested = "SELL both YES and NO (market overpriced)"
    else:
        # Market underpriced — Yes + No < 1.0
        suggested = "BUY both YES and NO (market underpriced)"

    signal = _signal_template(
        signal_type="arbitrage_inefficiency",
        market_slug=slug,
        token_id=market["yes_token_id"],
        side="YES",
        current_price=yes_price,
        trigger_reason=(
            f"YES ({yes_price:.4f}) + NO ({no_price:.4f}) = {price_sum:.4f}. "
            f"Deviation from 1.0: {deviation:+.4f} (threshold: ±{threshold})."
        ),
        suggested_action=suggested,
        suggested_size=0,
        confidence=0.5,
        risks=(
            "Arbitrage on Polymarket is competitive. Small deviations "
            "may be due to fees, spread, or token-specific factors. "
            "Cross-venue arbitrage has settlement risk."
        ),
    )

    return [signal]


# ── Task 3.6: Signal Aggregator ────────────────────────────────────

def run_all_signals(
    slug: str,
    config: Optional[dict] = None,
) -> list[dict]:
    """Run all signal detectors on a single market.

    Args:
        slug: Market slug to analyze
        config: Optional config overrides:
            - price_threshold_pct: float (default 5.0)
            - spread_threshold_pct: float (default 5.0)
            - volume_threshold: float (default 0.5)
            - expiry_days_before: int (default 7)
            - arb_threshold: float (default 0.05)
            - snapshot_dir: str (default "logs/market_snapshots")

    Returns:
        list of all triggered signal dicts, sorted by confidence descending.
    """
    if config is None:
        config = {}

    all_signals = []

    # 3.1 Price anomaly
    all_signals.extend(
        detect_price_anomaly(
            slug,
            threshold_pct=config.get("price_threshold_pct", 5.0),
            lookback_minutes=config.get("lookback_minutes", 60),
        )
    )

    # 3.2 Wide spread
    all_signals.extend(
        detect_wide_spread(
            slug,
            threshold_pct=config.get("spread_threshold_pct", 5.0),
        )
    )

    # 3.3 Liquidity change
    all_signals.extend(
        detect_liquidity_change(
            slug,
            volume_threshold=config.get("volume_threshold", 0.5),
            snapshot_dir=config.get("snapshot_dir", "logs/market_snapshots"),
        )
    )

    # 3.4 Expiry
    all_signals.extend(
        detect_approaching_expiry(
            slug,
            days_before=config.get("expiry_days_before", 7),
        )
    )

    # 3.5 Arbitrage
    all_signals.extend(
        check_arbitrage(
            slug,
            threshold=config.get("arb_threshold", 0.05),
        )
    )

    # Sort by confidence descending
    all_signals.sort(key=lambda s: s.get("confidence", 0), reverse=True)

    return all_signals


def run_signals_on_multiple(
    slugs: list[str],
    config: Optional[dict] = None,
) -> dict[str, list[dict]]:
    """Run all signal detectors on multiple markets.

    Args:
        slugs: List of market slugs
        config: Optional config overrides

    Returns:
        dict mapping slug -> list of triggered signals
    """
    results = {}
    for slug in slugs:
        try:
            results[slug] = run_all_signals(slug, config)
        except Exception as e:
            results[slug] = [
                _signal_template(
                    signal_type="error",
                    market_slug=slug,
                    token_id="",
                    side="",
                    current_price=0,
                    trigger_reason=f"Signal detection failed: {e}",
                    suggested_action="SKIP",
                    confidence=0,
                    risks="Data unavailable.",
                )
            ]
    return results


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    """Run all signal detectors on a market and print results."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Polymarket Agent — Signal Detection"
    )
    parser.add_argument("slug", nargs="?", help="Market slug to analyze")
    parser.add_argument(
        "--multi", nargs="+", help="Multiple slugs (space-separated)"
    )
    parser.add_argument("--price-threshold", type=float, default=5.0,
                        help="Price anomaly threshold %% (default: 5.0)")
    parser.add_argument("--spread-threshold", type=float, default=5.0,
                        help="Spread threshold %% (default: 5.0)")
    parser.add_argument("--volume-threshold", type=float, default=0.5,
                        help="Volume change ratio threshold (default: 0.5)")
    parser.add_argument("--expiry-days", type=int, default=7,
                        help="Warn N days before resolution (default: 7)")
    parser.add_argument("--arb-threshold", type=float, default=0.05,
                        help="Arbitrage deviation threshold (default: 0.05)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")

    args = parser.parse_args()

    config = {
        "price_threshold_pct": args.price_threshold,
        "spread_threshold_pct": args.spread_threshold,
        "volume_threshold": args.volume_threshold,
        "expiry_days_before": args.expiry_days,
        "arb_threshold": args.arb_threshold,
    }

    if args.multi:
        results = run_signals_on_multiple(args.multi, config)
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        else:
            for slug, signals in results.items():
                print(f"\n{'='*60}")
                print(f"  {slug}")
                print(f"{'='*60}")
                if not signals:
                    print("  No signals triggered.")
                for s in signals:
                    _print_signal(s)
    elif args.slug:
        signals = run_all_signals(args.slug, config)
        if args.json:
            print(json.dumps(signals, indent=2, ensure_ascii=False, default=str))
        else:
            if not signals:
                print(f"No signals triggered for {args.slug}")
            for s in signals:
                _print_signal(s)
    else:
        parser.print_help()


def _print_signal(signal: dict) -> None:
    """Pretty-print a signal."""
    print(f"""
  [{signal['signal_type'].upper()}]  confidence: {signal['confidence']:.0%}
  Price:     ${signal['current_price']:.4f}
  Action:    {signal['suggested_action']}
  Size:      {signal['suggested_size']:.0f} shares
  Reason:    {signal['trigger_reason']}
  Risks:     {signal.get('risks', 'N/A')}
  Confirm:   {'YES' if signal['human_confirmation_required'] else 'NO'}
""")


if __name__ == "__main__":
    main()
