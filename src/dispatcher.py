"""Polymarket Agent — Trading Strategy Dispatcher.

Orchestrates the full pipeline: market monitoring → signal detection →
order drafting → paper/live execution → position management.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from config import Config
from logger import TradeLogger
from market_data import get_market_summary
from signals import run_all_signals
from order_draft import create_order_draft
from paper_trader import PaperTrader
from risk_engine import RiskEngine


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(v: float) -> float:
    return round(float(v), 2)


class Dispatcher:
    """Trading strategy orchestrator — signal → draft → execute loop."""

    def __init__(
        self,
        config: Config,
        logger: TradeLogger,
        watchlist_slugs: list[str],
        capital: float = 255.0,
        min_confidence: float = 0.5,
        cycle_seconds: int = 60,
        stop_loss_pct: float = 0.10,
        take_profit_pct: float = 0.05,
        max_hold_minutes: int = 480,
        dry_run: bool = True,
    ):
        self.config = config
        self.logger = logger
        self.watchlist = watchlist_slugs
        self.capital = capital
        self.min_confidence = min_confidence
        self.cycle_seconds = cycle_seconds
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_hold_minutes = max_hold_minutes
        self.dry_run = dry_run

        self.paper = PaperTrader(capital=capital, config=config, logger=logger)
        self.risk = RiskEngine(config, logger)
        self.cycles = 0
        self.signals_total = 0
        self.trades_total = 0
        self.pending_confirmations: list[dict] = []

    # ── Main Loop ────────────────────────────────────────────────

    def run_once(self) -> dict:
        """Run one full cycle. Returns summary dict."""
        self.cycles += 1
        cycle_start = time.time()
        signals_detected = 0
        trades_executed = 0

        print(f"\n{'='*60}")
        print(f"  CYCLE {self.cycles}  |  {_now_iso()}")
        print(f"  Watchlist: {len(self.watchlist)} markets  |  Mode: {'dry-run' if self.dry_run else 'live'}")
        print(f"{'='*60}")

        for slug in self.watchlist:
            try:
                trades_executed += self._process_market(slug)
                signals_detected += 1  # counted per market with signals
            except Exception as e:
                print(f"  ✗ {slug[:50]}: error — {e}")

        # Check exits on open positions
        exits = self._check_exits()

        # Summary
        elapsed = time.time() - cycle_start
        open_pos = len(self.paper.positions)
        summary = self.paper.get_summary()

        print(f"\n  Signals: {signals_detected} | Trades: {trades_executed} | Exits: {exits}")
        print(f"  Positions: {open_pos} open | PnL: ${summary['total_pnl']:+.2f} | Capital: ${self.paper.capital:.2f}")
        print(f"  Queue: {len(self.pending_confirmations)} pending | Cycle: {elapsed:.1f}s")

        self.signals_total += signals_detected
        self.trades_total += trades_executed

        return {
            "cycle": self.cycles,
            "signals_detected": signals_detected,
            "trades_executed": trades_executed,
            "exits": exits,
            "pending_confirmations": len(self.pending_confirmations),
        }

    def run_forever(self) -> None:
        """Run the main loop indefinitely."""
        print(f"\n  DISPATCHER STARTED")
        print(f"  Markets: {', '.join(s[:40] for s in self.watchlist[:5])}")
        print(f"  Capital: ${self.capital:.2f} | Cycle: {self.cycle_seconds}s")
        print(f"  Confidence: ≥{self.min_confidence} | Mode: {'dry-run' if self.dry_run else 'live'}")
        print()

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\n  Shutting down...")
                self.paper.save_session()
                break
            except Exception as e:
                print(f"  Cycle error (continuing): {e}")

            time.sleep(self.cycle_seconds)

    # ── Market Processing ────────────────────────────────────────

    def _process_market(self, slug: str) -> int:
        """Run signals on one market. Returns number of trades executed."""
        trades = 0

        # Fetch market data
        summary = get_market_summary(slug)
        if summary is None:
            return 0

        # Run signals
        signals = run_all_signals(slug, {
            "price_threshold_pct": 3.0,
            "spread_threshold_pct": 5.0,
            "volume_threshold": 0.5,
            "expiry_days_before": 7,
            "arb_threshold": 0.05,
        })

        if not signals:
            print(f"  {slug[:50]}: YES={summary['yes_price']:.4f} — 0 signals")
            return 0

        # Process each signal
        for sig in signals:
            if sig["signal_type"] == "error":
                continue

            conf = sig.get("confidence", 0)
            action = sig.get("suggested_action", "")

            # Log signal
            self.logger.log_signal(sig)

            # Skip low-confidence or non-actionable signals
            if conf < self.min_confidence:
                continue
            if "CAUTION" in action.upper() or "MONITOR" in action.upper() or "CHECK" in action.upper():
                continue

            # Determine trade parameters
            side = sig.get("side", "YES")
            token_id = summary["yes_token_id"] if side == "YES" else summary["no_token_id"]
            current_price = summary["yes_price"] if side == "YES" else summary["no_price"]
            trade_action = "BUY" if "BUY" in action.upper() else "SELL"
            size = min(sig.get("suggested_size", 5.0), self.config.max_order_size / max(current_price, 0.01))

            # Create order draft
            draft = create_order_draft(
                market_slug=slug,
                condition_id=summary["condition_id"],
                side=side,
                token_id=token_id,
                action=trade_action,
                limit_price=current_price,
                size=size,
                reason=sig["trigger_reason"][:200],
                risk_notes=sig.get("risks", ""),
                best_bid=summary.get("yes_best_bid") if side == "YES" else summary.get("no_best_bid"),
                best_ask=summary.get("yes_best_ask") if side == "YES" else summary.get("no_best_ask"),
                spread_pct=summary.get("yes_spread_pct") if side == "YES" else summary.get("no_spread_pct"),
                confidence=conf,
                signal_type=sig["signal_type"],
            )

            # Validate
            validation = self.risk.validate_order(draft, [], 0, 0.0, self.paper.capital)
            if not validation["can_proceed"]:
                print(f"  ✗ {slug[:40]}: {sig['signal_type']} — blocked: {validation['violations']}")
                continue

            # Execute
            if self.dry_run or not self.config.live_trading:
                # Paper execution
                if trade_action == "BUY" and slug not in self.paper.positions:
                    try:
                        trade = self.paper.buy(
                            slug, side, current_price, size,
                            reason=sig["trigger_reason"][:100],
                            signal_type=sig["signal_type"],
                            confidence=conf,
                            expected_edge=0.03,
                            risk_notes=sig.get("risks", ""),
                        )
                        trades += 1
                        print(f"  ✓ {slug[:40]}: BUY {side} {size:.0f} @ ${current_price:.4f} [{sig['signal_type']}]")
                    except ValueError as e:
                        print(f"  ✗ {slug[:40]}: {e}")
                elif trade_action == "SELL" and slug in self.paper.positions:
                    try:
                        trade = self.paper.sell(slug, side, current_price, size, sig["trigger_reason"][:100])
                        trades += 1
                        print(f"  ✓ {slug[:40]}: SELL {side} PnL=${trade['theoretical_pnl']:+.2f} [{sig['signal_type']}]")
                    except ValueError as e:
                        print(f"  ✗ {slug[:40]}: {e}")
            else:
                # Live — queue for confirmation
                draft["draft_id"] = f"draft_{slug}_{int(time.time())}"
                self.pending_confirmations.append(draft)
                print(f"  ⏳ {slug[:40]}: queued for confirmation [{sig['signal_type']}]")

        return trades

    # ── Position Management ──────────────────────────────────────

    def _check_exits(self) -> int:
        """Check open positions for exit conditions. Returns exits executed."""
        exits = 0
        now = datetime.now(timezone.utc)

        for slug, pos in list(self.paper.positions.items()):
            summary = get_market_summary(slug)
            if summary is None:
                continue

            side = pos.side
            current_price = summary["yes_price"] if side == "YES" else summary["no_price"]
            entry = pos.entry_price
            opened = datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
            age_minutes = (now - opened).total_seconds() / 60

            # Stop loss
            if current_price <= entry * (1 - self.stop_loss_pct):
                try:
                    trade = self.paper.sell(slug, side, current_price, pos.size, "stop loss")
                    exits += 1
                    print(f"  🛑 {slug[:40]}: STOP LOSS — ${trade['theoretical_pnl']:+.2f}")
                except ValueError:
                    pass
                continue

            # Take profit
            if current_price >= entry * (1 + self.take_profit_pct):
                try:
                    trade = self.paper.sell(slug, side, current_price, pos.size, "take profit")
                    exits += 1
                    print(f"  ✅ {slug[:40]}: TAKE PROFIT — ${trade['theoretical_pnl']:+.2f}")
                except ValueError:
                    pass
                continue

            # Time decay
            if age_minutes > self.max_hold_minutes:
                try:
                    trade = self.paper.sell(slug, side, current_price, pos.size, "time decay")
                    exits += 1
                    print(f"  ⏰ {slug[:40]}: TIME DECAY ({age_minutes:.0f}m) — ${trade['theoretical_pnl']:+.2f}")
                except ValueError:
                    pass

        return exits

    # ── Confirmation Queue ───────────────────────────────────────

    def confirm_draft(self, draft_id: str) -> dict:
        """Confirm and execute a pending draft. Called by dashboard API."""
        for i, d in enumerate(self.pending_confirmations):
            if d.get("draft_id") == draft_id:
                del self.pending_confirmations[i]

                # Execute as paper trade (or live if enabled)
                slug = d["market_slug"]
                side = d["side"]
                price = d["limit_price"]
                size = d["size"]

                if d["action"] == "BUY" and slug not in self.paper.positions:
                    self.paper.buy(
                        slug, side, price, size,
                        reason=d["reason"], signal_type=d["signal_type"],
                        confidence=d["confidence"], expected_edge=0.03,
                        risk_notes=d.get("risk_notes", ""),
                    )

                return {"status": "confirmed", "draft_id": draft_id}

        return {"status": "not_found", "draft_id": draft_id}

    def cancel_draft(self, draft_id: str) -> dict:
        """Cancel a pending draft."""
        for i, d in enumerate(self.pending_confirmations):
            if d.get("draft_id") == draft_id:
                del self.pending_confirmations[i]
                return {"status": "cancelled", "draft_id": draft_id}
        return {"status": "not_found", "draft_id": draft_id}

    # ── Watchlist ────────────────────────────────────────────────

    def add_to_watchlist(self, slug: str) -> None:
        if slug not in self.watchlist:
            self.watchlist.append(slug)

    def remove_from_watchlist(self, slug: str) -> None:
        if slug in self.watchlist:
            self.watchlist.remove(slug)

    # ── Status ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Full status snapshot for dashboard API."""
        summary = self.paper.get_summary()
        return {
            "cycles": self.cycles,
            "signals_total": self.signals_total,
            "trades_total": self.trades_total,
            "pending_confirmations": len(self.pending_confirmations),
            "open_positions": len(self.paper.positions),
            "capital": self.paper.capital,
            "total_pnl": summary["total_pnl"],
            "total_pnl_pct": summary["total_pnl_pct"],
            "win_rate": summary["win_rate"],
            "watchlist": self.watchlist,
            "mode": "dry_run" if self.dry_run else "live",
            "timestamp": _now_iso(),
        }


# ── CLI ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(prog="dispatcher", description="Trading Strategy Dispatcher")
    parser.add_argument("--watchlist", required=True, help="Comma-separated market slugs")
    parser.add_argument("--capital", type=float, default=255.0, help="Paper capital")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="Min signal confidence")
    parser.add_argument("--cycle", type=int, default=60, help="Seconds between cycles")
    parser.add_argument("--stop-loss", type=float, default=0.10, help="Stop loss % (0.10 = 10%)")
    parser.add_argument("--take-profit", type=float, default=0.05, help="Take profit %")
    parser.add_argument("--max-hold", type=int, default=480, help="Max hold minutes")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Paper trading only")
    parser.add_argument("--live", action="store_true", help="Enable live trading confirmations")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")

    args = parser.parse_args()
    watchlist = [s.strip() for s in args.watchlist.split(",") if s.strip()]
    dry_run = not args.live

    config = Config()
    logger = TradeLogger(str(config.logs_dir))

    dispatcher = Dispatcher(
        config=config,
        logger=logger,
        watchlist_slugs=watchlist,
        capital=args.capital,
        min_confidence=args.min_confidence,
        cycle_seconds=args.cycle,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        max_hold_minutes=args.max_hold,
        dry_run=dry_run,
    )

    if args.once:
        dispatcher.run_once()
        dispatcher.paper.save_session()
    else:
        dispatcher.run_forever()


if __name__ == "__main__":
    main()
