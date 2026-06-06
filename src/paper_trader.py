"""Paper trading engine for simulated Polymarket positions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

try:
    from src.config import Config
    from src.logger import TradeLogger
except ImportError:  # pragma: no cover - supports direct ``sys.path=src`` imports.
    from config import Config
    from logger import TradeLogger


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _money(value: float) -> float:
    """Round currency-like values to cents."""
    return round(float(value), 2)


@dataclass
class PaperPosition:
    """Open paper position tracked by the paper trader."""

    slug: str
    side: str
    entry_price: float
    size: float
    cost: float
    opened_at: str


class PaperTrader:
    """Simulate Polymarket buys and sells without placing live orders."""

    def __init__(self, capital: float, config: Config, logger: TradeLogger):
        """Initialize a paper trading session."""
        self.initial_capital = _money(capital)
        self.capital = _money(capital)
        self.config = config
        self.logger = logger
        self.session_id = str(uuid4())
        self.started_at = _now_iso()
        self.positions: dict[str, PaperPosition] = {}
        self.trades: list[dict] = []
        self.peak_capital = self.capital
        self.max_drawdown = 0.0

    def buy(
        self,
        slug: str,
        side: str,
        price: float,
        size: float,
        reason: str,
        signal_type: str,
        confidence: float,
        expected_edge: float,
        risk_notes: str,
    ) -> dict:
        """Open a simulated position and deduct its cost from capital."""
        side = side.upper()
        if side not in {"YES", "NO"}:
            raise ValueError("side must be 'YES' or 'NO'")
        if slug in self.positions:
            raise ValueError(f"open position already exists for {slug}")

        cost = _money(price * size)
        if cost > self.capital:
            raise ValueError("insufficient paper capital")

        self.capital = _money(self.capital - cost)
        opened_at = _now_iso()
        self.positions[slug] = PaperPosition(
            slug=slug,
            side=side,
            entry_price=float(price),
            size=float(size),
            cost=cost,
            opened_at=opened_at,
        )
        self._update_drawdown()

        trade = self._trade_dict(
            timestamp=opened_at,
            slug=slug,
            side=side,
            action="BUY",
            price=price,
            size=size,
            cost=cost,
            reason=reason,
            signal_type=signal_type,
            confidence=confidence,
            expected_edge=expected_edge,
            risk_notes=risk_notes,
            entry_price=price,
            status="open",
        )
        self.trades.append(trade)
        self.logger.log_paper_trade(trade)
        return trade

    def sell(self, slug: str, side: str, price: float, size: float, reason: str) -> dict:
        """Close an existing simulated position and add proceeds to capital."""
        position = self.positions.get(slug)
        side = side.upper()
        if position is None:
            raise ValueError(f"no open position for {slug}")
        if position.side != side:
            raise ValueError(f"open position for {slug} is {position.side}, not {side}")
        if float(size) != position.size:
            raise ValueError("partial closes are not supported")

        proceeds = _money(price * size)
        theoretical_pnl = _money(proceeds - position.cost)
        theoretical_pnl_pct = (
            round((theoretical_pnl / position.cost) * 100, 4)
            if position.cost
            else 0.0
        )
        self.capital = _money(self.capital + proceeds)
        self.peak_capital = max(self.peak_capital, self.capital)
        del self.positions[slug]

        trade = self._trade_dict(
            timestamp=_now_iso(),
            slug=slug,
            side=side,
            action="SELL",
            price=price,
            size=size,
            cost=proceeds,
            reason=reason,
            signal_type="",
            confidence=0.0,
            expected_edge=None,
            risk_notes="",
            entry_price=position.entry_price,
            exit_price=price,
            theoretical_pnl=theoretical_pnl,
            theoretical_pnl_pct=theoretical_pnl_pct,
            status="closed",
        )
        self.trades.append(trade)
        self.logger.log_paper_trade(trade)
        return trade

    def close_position(self, slug: str, price: float, reason: str) -> dict:
        """Close the open position for a market slug."""
        position = self.positions.get(slug)
        if position is None:
            raise ValueError(f"no open position for {slug}")
        return self.sell(slug, position.side, price, position.size, reason)

    def get_positions(self) -> dict[str, PaperPosition]:
        """Return open positions keyed by market slug."""
        return dict(self.positions)

    def get_summary(self) -> dict:
        """Return current session-level paper trading statistics."""
        closed_trades = [trade for trade in self.trades if trade["status"] == "closed"]
        wins = sum(1 for trade in closed_trades if (trade["theoretical_pnl"] or 0.0) > 0)
        losses = sum(1 for trade in closed_trades if (trade["theoretical_pnl"] or 0.0) <= 0)
        total_pnl = _money(sum(trade["theoretical_pnl"] or 0.0 for trade in closed_trades))
        win_rate = round(wins / len(closed_trades), 4) if closed_trades else 0.0

        return {
            "session_id": self.session_id,
            "start_time": self.started_at,
            "end_time": _now_iso(),
            "initial_capital": self.initial_capital,
            "current_capital": self.capital,
            "total_trades": len(self.trades),
            "open_trades": len(self.positions),
            "closed_trades": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "total_pnl_pct": (
                round((total_pnl / self.initial_capital) * 100, 4)
                if self.initial_capital
                else 0.0
            ),
            "win_rate": win_rate,
            "max_drawdown": self.max_drawdown,
        }

    def get_open_positions(self) -> list[dict]:
        """Return open positions with mark-to-entry unrealized PnL."""
        return [
            {
                "slug": position.slug,
                "side": position.side,
                "entry_price": position.entry_price,
                "size": position.size,
                "cost": position.cost,
                "unrealized_pnl": 0.0,
            }
            for position in self.positions.values()
        ]

    def save_session(self) -> None:
        """Persist the current session summary."""
        self.logger.log_session_summary(self.get_summary())

    def _trade_dict(
        self,
        *,
        timestamp: str,
        slug: str,
        side: str,
        action: str,
        price: float,
        size: float,
        cost: float,
        reason: str,
        signal_type: str,
        confidence: float,
        expected_edge: Optional[float],
        risk_notes: str,
        entry_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        theoretical_pnl: Optional[float] = None,
        theoretical_pnl_pct: Optional[float] = None,
        status: str = "open",
    ) -> dict:
        """Build the required paper trade log schema."""
        return {
            "timestamp": timestamp,
            "session_id": self.session_id,
            "market_slug": slug,
            "condition_id": "",
            "token_id": "",
            "side": side,
            "action": action,
            "simulated_price": float(price),
            "simulated_size": float(size),
            "simulated_cost": _money(cost),
            "simulated_fill_type": "paper",
            "reason": reason,
            "signal_type": signal_type,
            "confidence": float(confidence),
            "expected_edge": expected_edge,
            "risk_notes": risk_notes,
            "human_confirmed": False,
            "entry_price": float(entry_price) if entry_price is not None else None,
            "exit_price": float(exit_price) if exit_price is not None else None,
            "theoretical_pnl": theoretical_pnl,
            "theoretical_pnl_pct": theoretical_pnl_pct,
            "status": status,
        }

    def _update_drawdown(self) -> None:
        """Update maximum observed cash drawdown."""
        drawdown = _money(self.peak_capital - self.capital)
        self.max_drawdown = max(self.max_drawdown, drawdown)


# ── CLI ──────────────────────────────────────────────────────────────


def main():
    """Paper trading CLI entry point."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="paper_trader",
        description="Polymarket Agent — Paper Trading",
    )
    parser.add_argument(
        "--capital", type=float, default=255.0,
        help="Starting paper capital (default: 255.0)"
    )
    parser.add_argument(
        "--positions", action="store_true",
        help="Show current paper positions"
    )
    parser.add_argument(
        "--pnl", action="store_true",
        help="Show paper PnL summary"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Generate daily report"
    )
    parser.add_argument(
        "--date", type=str,
        help="Date for report (YYYY-MM-DD, default: today)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON"
    )

    args = parser.parse_args()

    # Import config and init
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from config import Config
        from logger import TradeLogger
        from tracker import generate_daily_report
    except ImportError:
        from src.config import Config  # type: ignore
        from src.logger import TradeLogger  # type: ignore
        from src.tracker import generate_daily_report  # type: ignore

    config = Config()
    logger = TradeLogger(str(config.logs_dir))
    trader = PaperTrader(capital=args.capital, config=config, logger=logger)

    if args.positions:
        positions = trader.get_open_positions()
        if args.json:
            print(json.dumps(positions, indent=2, default=str))
        elif not positions:
            print("No open positions.")
        else:
            print()
            print("  Open Paper Positions")
            print("  " + "-" * 50)
            for p in positions:
                print(f"  {p['slug']:50s} {p['side']:4s} "
                      f"entry=${p['entry_price']:.4f} size={p['size']:.0f} "
                      f"cost=${p['cost']:.2f}")

    if args.pnl:
        summary = trader.get_summary()
        if args.json:
            print(json.dumps(summary, indent=2, default=str))
        else:
            print()
            print("  Paper PnL Summary")
            print("  " + "-" * 50)
            print(f"  Capital:    ${summary['initial_capital']:.2f} → "
                  f"${summary['current_capital']:.2f} "
                  f"({summary['total_pnl_pct']:+.2f}%)")
            print(f"  Total PnL:  ${summary['total_pnl']:+.2f}")
            print(f"  Trades:     {summary['total_trades']} "
                  f"({summary['open_trades']} open, {summary['closed_trades']} closed)")
            print(f"  Win rate:   {summary['win_rate']:.1%}")
            print(f"  Max DD:     ${summary['max_drawdown']:.2f}")

    if args.report:
        summary = trader.get_summary()
        report = generate_daily_report(
            trader.trades,
            trader.get_open_positions(),
            trader.capital,
            signals_today=0  # Paper trader doesn't track signals directly
        )
        if args.date:
            report["date"] = args.date
        logger.log_daily_report(report)

        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print()
            print(f"  Daily Report — {report['date']}")
            print("  " + "-" * 50)
            cap = report["capital"]
            print(f"  Capital:  ${cap['initial']:.2f} → ${cap['current']:.2f} "
                  f"({cap['change']:+.2f} / {cap['change_pct']:+.2f}%)")
            ts = report["trading_summary"]
            print(f"  Trades:   {ts['total_trades_today']} total, "
                  f"{ts['closed_trades']} closed, {ts['open_positions_count']} open")
            if ts['closed_trades'] > 0:
                print(f"  Wins:     {ts['wins']} / {ts['losses']} losses "
                      f"({ts['win_rate']:.1%})")
            pnl = report["pnl"]
            print(f"  PnL:      realized=${pnl['realized_pnl']:+.2f} "
                  f"| unrealized=${pnl['unrealized_pnl']:+.2f} "
                  f"| total=${pnl['total_pnl']:+.2f}")
            print(f"  DD:       ${pnl['max_drawdown']:.2f}")
            print(f"  Signals:  {report['signals_today']}")

    # Default: show summary if no args
    if not any([args.positions, args.pnl, args.report]):
        summary = trader.get_summary()
        if args.json:
            print(json.dumps(summary, indent=2, default=str))
        else:
            print()
            print("  Paper Trader — Session Summary")
            print("  " + "-" * 50)
            print(f"  Capital:    ${summary['initial_capital']:.2f} → "
                  f"${summary['current_capital']:.2f}")
            print(f"  Total PnL:  ${summary['total_pnl']:+.2f} "
                  f"({summary['total_pnl_pct']:+.2f}%)")
            print(f"  Trades:     {summary['total_trades']} "
                  f"({summary['open_trades']} open, {summary['closed_trades']} closed)")
            if summary['closed_trades'] > 0:
                print(f"  Win rate:   {summary['win_rate']:.1%} "
                      f"({summary['wins']}W / {summary['losses']}L)")
            print(f"  Max DD:     ${summary['max_drawdown']:.2f}")
            print(f"  Session:    {summary['session_id'][:8]}...")
            print()


if __name__ == "__main__":
    from pathlib import Path
    main()
