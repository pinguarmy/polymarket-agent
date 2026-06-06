"""Position and PnL tracking helpers for Polymarket paper trading."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    """Return the current UTC date."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _money(value: float) -> float:
    """Round currency-like values to cents."""
    return round(float(value), 2)


class PositionTracker:
    """Track open positions, capital, realized PnL, and drawdown."""

    def __init__(self, initial_capital: float):
        """Initialize the tracker with starting paper capital."""
        self.initial_capital = _money(initial_capital)
        self.capital = _money(initial_capital)
        self.peak_capital = self.capital
        self.max_drawdown = 0.0
        self.positions: dict[str, dict] = {}
        self.closed_trades: list[dict] = []

    def open_position(self, trade: dict) -> None:
        """Open a position from a BUY trade dict."""
        slug = trade["market_slug"]
        cost = _money(trade["simulated_cost"])
        self.positions[slug] = {
            "slug": slug,
            "market_slug": slug,
            "side": trade["side"],
            "entry_price": trade["entry_price"],
            "size": trade["simulated_size"],
            "cost": cost,
            "opened_at": trade["timestamp"],
            "unrealized_pnl": 0.0,
        }
        self.capital = _money(self.capital - cost)
        self._update_drawdown()

    def close_position(self, trade: dict) -> None:
        """Close a position from a SELL trade dict."""
        slug = trade["market_slug"]
        self.positions.pop(slug, None)
        self.closed_trades.append(trade)
        self.capital = _money(self.capital + trade["simulated_cost"])
        self.peak_capital = max(self.peak_capital, self.capital)
        self._update_drawdown()

    def get_position(self, slug: str) -> Optional[dict]:
        """Return one open position by market slug, if present."""
        position = self.positions.get(slug)
        return dict(position) if position is not None else None

    def get_all_positions(self) -> list[dict]:
        """Return all open positions."""
        return [dict(position) for position in self.positions.values()]

    def get_daily_pnl(self) -> float:
        """Return realized PnL for trades closed today."""
        today = _today()
        return _money(
            sum(
                float(trade.get("theoretical_pnl") or 0.0)
                for trade in self.closed_trades
                if str(trade.get("timestamp", "")).startswith(today)
            )
        )

    def get_total_pnl(self) -> float:
        """Return total realized PnL."""
        return _money(sum(float(trade.get("theoretical_pnl") or 0.0) for trade in self.closed_trades))

    def get_capital(self) -> float:
        """Return current paper capital."""
        return self.capital

    def get_peak_capital(self) -> float:
        """Return highest observed paper capital."""
        return self.peak_capital

    def get_max_drawdown(self) -> float:
        """Return maximum observed drawdown in dollars."""
        return self.max_drawdown

    def _update_drawdown(self) -> None:
        """Update maximum observed drawdown."""
        self.max_drawdown = max(self.max_drawdown, _money(self.peak_capital - self.capital))


def generate_daily_report(
    trades: list[dict],
    positions: list[dict],
    capital: float,
    signals_today: int,
) -> dict:
    """Generate a daily report from trade, position, and capital data."""
    closed_trades = [
        trade
        for trade in trades
        if trade.get("status") == "closed" and trade.get("theoretical_pnl") is not None
    ]
    wins = sum(1 for trade in closed_trades if float(trade.get("theoretical_pnl") or 0.0) > 0)
    losses = sum(1 for trade in closed_trades if float(trade.get("theoretical_pnl") or 0.0) <= 0)
    realized_pnl = _money(sum(float(trade.get("theoretical_pnl") or 0.0) for trade in closed_trades))
    unrealized_pnl = _money(sum(float(position.get("unrealized_pnl") or 0.0) for position in positions))
    total_pnl = _money(realized_pnl + unrealized_pnl)
    initial_capital = _money(float(capital) - realized_pnl)
    change = _money(float(capital) - initial_capital)
    max_drawdown = abs(min(0.0, change))

    return {
        "date": _today(),
        "report_generated_at": _now_iso(),
        "capital": {
            "initial": initial_capital,
            "current": _money(capital),
            "change": change,
            "change_pct": round((change / initial_capital) * 100, 4) if initial_capital else 0.0,
        },
        "trading_summary": {
            "total_trades_today": len(trades),
            "open_positions_count": len(positions),
            "closed_trades": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed_trades), 4) if closed_trades else 0.0,
        },
        "pnl": {
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "max_drawdown": _money(max_drawdown),
        },
        "open_positions": positions,
        "signals_today": int(signals_today),
        "risk_alerts": [],
        "markets_monitored": 0,
        "next_actions": [],
    }
