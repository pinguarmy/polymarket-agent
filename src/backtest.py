"""Generic backtesting framework for Polymarket trading strategies.

Usage:
    python src/backtest.py --slug <market_slug> --strategy mean_reversion --capital 255
    python src/backtest.py --slugs slug1,slug2 --strategy momentum --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

try:
    from src.market_data import get_market_by_slug, get_price_history
except ImportError:  # pragma: no cover - supports direct execution as ``python src/backtest.py``.
    from market_data import get_market_by_slug, get_price_history


PricePoint = dict[str, Any]
Position = dict[str, Any]
Signal = dict[str, Any]
StrategyFn = Callable[[list[PricePoint], int, Position | None], Signal | None]


def _money(value: float) -> float:
    return round(float(value), 2)


def _price(point: PricePoint) -> float:
    return float(point["p"])


def _timestamp(point: PricePoint) -> int:
    return int(point.get("t", 0))


def _interval_for_lookback(lookback_days: int) -> str:
    if lookback_days <= 1:
        return "1d"
    if lookback_days <= 7:
        return "1w"
    return "1m"


def _safe_signal_price(signal: Signal, history: list[PricePoint], idx: int) -> float:
    price = signal.get("price", _price(history[idx]))
    return max(0.0, min(1.0, float(price)))


def _iso_from_ts(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Backtest:
    """Run historical paper simulations for simple long-only Polymarket strategies."""

    def __init__(self, capital: float = 255.0):
        self.initial_capital = _money(capital)
        self.capital = _money(capital)
        self.trades: list[dict] = []
        self.equity_curve: list[dict] = []

    def run(self, market_slug: str, strategy_fn: StrategyFn, lookback_days: int = 7) -> dict:
        """Fetch price history, simulate strategy signals, and return performance metrics."""
        self._reset()

        market = get_market_by_slug(market_slug)
        if market is None:
            raise ValueError(f"market not found for slug '{market_slug}'")

        condition_id = market.get("condition_id")
        if not condition_id:
            raise ValueError(f"market '{market_slug}' has no condition_id")

        history = get_price_history(
            condition_id,
            interval=_interval_for_lookback(lookback_days),
            fidelity=200,
        )
        history = self._clean_history(history)
        if not history:
            raise ValueError(f"no price history available for slug '{market_slug}'")

        open_position: Position | None = None

        for idx, point in enumerate(history):
            current_price = _price(point)
            self._record_equity(point, open_position, current_price)

            signal = strategy_fn(history, idx, open_position)
            if not signal:
                continue

            action = str(signal.get("action", "")).upper()
            if action == "BUY" and open_position is None:
                open_position = self._open_position(market_slug, history, idx, signal)
            elif action == "SELL" and open_position is not None:
                self._close_position(market_slug, history, idx, signal, open_position)
                open_position = None

        if open_position is not None:
            self._mark_open_position(market_slug, history, open_position)

        return self._results(market_slug=market_slug, condition_id=condition_id)

    def run_multi(self, slugs: list[str], strategy_fn: StrategyFn) -> dict:
        """Run backtest across multiple markets."""
        runs: dict[str, dict] = {}
        all_closed_trades: list[dict] = []
        all_trades: list[dict] = []

        for slug in slugs:
            result = self.run(slug, strategy_fn)
            runs[slug] = result
            all_trades.extend(result["trades"])
            all_closed_trades.extend(result["closed_trades"])

        return {
            "markets": runs,
            "trades": all_trades,
            "closed_trades": all_closed_trades,
            **self._trade_metrics(all_closed_trades),
            "max_drawdown": _money(sum(float(run["max_drawdown"]) for run in runs.values())),
            "max_drawdown_pct": (
                round(
                    sum(float(run["max_drawdown"]) for run in runs.values())
                    / (self.initial_capital * len(runs))
                    * 100,
                    4,
                )
                if runs and self.initial_capital
                else 0.0
            ),
            "sharpe": self._multi_sharpe(runs),
        }

    def _reset(self) -> None:
        self.capital = self.initial_capital
        self.trades = []
        self.equity_curve = []

    def _clean_history(self, history: list[dict]) -> list[PricePoint]:
        points: list[PricePoint] = []
        for point in history:
            try:
                price = float(point["p"])
                ts = int(point.get("t", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if 0.0 <= price <= 1.0:
                points.append({"t": ts, "p": price})
        return sorted(points, key=lambda item: item["t"])

    def _open_position(
        self,
        market_slug: str,
        history: list[PricePoint],
        idx: int,
        signal: Signal,
    ) -> Position | None:
        price = _safe_signal_price(signal, history, idx)
        requested_size = float(signal.get("size", 0.0))
        if price <= 0.0 or requested_size <= 0.0:
            return None

        affordable_size = self.capital / price if price else 0.0
        size = min(requested_size, affordable_size)
        if size <= 0.0:
            return None

        cost = _money(price * size)
        self.capital = _money(self.capital - cost)

        point = history[idx]
        position: Position = {
            "market_slug": market_slug,
            "entry_price": price,
            "entry_idx": idx,
            "entry_ts": _timestamp(point),
            "side": str(signal.get("side", "YES")).upper(),
            "size": size,
            "cost": cost,
            "reason": str(signal.get("reason", "")),
        }
        self.trades.append({
            "market_slug": market_slug,
            "action": "BUY",
            "side": position["side"],
            "price": price,
            "size": size,
            "cost": cost,
            "timestamp": position["entry_ts"],
            "datetime": _iso_from_ts(position["entry_ts"]),
            "idx": idx,
            "reason": position["reason"],
            "status": "open",
        })
        return position

    def _close_position(
        self,
        market_slug: str,
        history: list[PricePoint],
        idx: int,
        signal: Signal,
        position: Position,
    ) -> None:
        price = _safe_signal_price(signal, history, idx)
        proceeds = _money(price * float(position["size"]))
        pnl = _money(proceeds - float(position["cost"]))
        pnl_pct = round((pnl / float(position["cost"])) * 100, 4) if position["cost"] else 0.0
        self.capital = _money(self.capital + proceeds)

        point = history[idx]
        exit_ts = _timestamp(point)
        self.trades.append({
            "market_slug": market_slug,
            "action": "SELL",
            "side": position["side"],
            "entry_price": position["entry_price"],
            "exit_price": price,
            "price": price,
            "size": position["size"],
            "cost": proceeds,
            "entry_idx": position["entry_idx"],
            "exit_idx": idx,
            "entry_ts": position["entry_ts"],
            "exit_ts": exit_ts,
            "timestamp": exit_ts,
            "datetime": _iso_from_ts(exit_ts),
            "holding_period_points": idx - int(position["entry_idx"]),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": str(signal.get("reason", "")),
            "entry_reason": position.get("reason", ""),
            "status": "closed",
        })

    def _mark_open_position(
        self,
        market_slug: str,
        history: list[PricePoint],
        position: Position,
    ) -> None:
        last_idx = len(history) - 1
        last_price = _price(history[last_idx])
        unrealized_pnl = _money((last_price * float(position["size"])) - float(position["cost"]))
        self.trades.append({
            "market_slug": market_slug,
            "action": "MARK",
            "side": position["side"],
            "entry_price": position["entry_price"],
            "mark_price": last_price,
            "size": position["size"],
            "entry_idx": position["entry_idx"],
            "mark_idx": last_idx,
            "entry_ts": position["entry_ts"],
            "mark_ts": _timestamp(history[last_idx]),
            "unrealized_pnl": unrealized_pnl,
            "reason": "open at end of backtest",
            "status": "open",
        })

    def _record_equity(
        self,
        point: PricePoint,
        position: Position | None,
        current_price: float,
    ) -> None:
        position_value = 0.0
        if position is not None:
            position_value = current_price * float(position["size"])
        equity = _money(self.capital + position_value)
        self.equity_curve.append({
            "timestamp": _timestamp(point),
            "datetime": _iso_from_ts(_timestamp(point)),
            "equity": equity,
        })

    def _results(self, market_slug: str, condition_id: str) -> dict:
        closed_trades = [trade for trade in self.trades if trade.get("status") == "closed"]
        return {
            "market_slug": market_slug,
            "condition_id": condition_id,
            "initial_capital": self.initial_capital,
            "ending_capital": self.capital,
            "ending_equity": self.equity_curve[-1]["equity"] if self.equity_curve else self.capital,
            "trades": self.trades,
            "closed_trades": closed_trades,
            "equity_curve": self.equity_curve,
            **self._trade_metrics(closed_trades),
            "max_drawdown": self._max_drawdown()[0],
            "max_drawdown_pct": self._max_drawdown()[1],
            "sharpe": self._sharpe_ratio(),
        }

    def _trade_metrics(self, closed_trades: list[dict]) -> dict:
        pnls = [float(trade.get("pnl", 0.0)) for trade in closed_trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        total_pnl = _money(sum(pnls))

        return {
            "total_pnl": total_pnl,
            "win_rate": round(len(wins) / len(pnls), 4) if pnls else 0.0,
            "profit_factor": (
                round(gross_profit / gross_loss, 4)
                if gross_loss > 0
                else (math.inf if gross_profit > 0 else 0.0)
            ),
            "average_trade_pnl": _money(total_pnl / len(pnls)) if pnls else 0.0,
            "best_trade": _money(max(pnls)) if pnls else 0.0,
            "worst_trade": _money(min(pnls)) if pnls else 0.0,
            "number_of_trades": len(pnls),
            "gross_profit": _money(gross_profit),
            "gross_loss": _money(gross_loss),
        }

    def _max_drawdown(self) -> tuple[float, float]:
        if not self.equity_curve:
            return 0.0, 0.0

        peak = float(self.equity_curve[0]["equity"])
        max_dd = 0.0
        max_dd_pct = 0.0
        for point in self.equity_curve:
            equity = float(point["equity"])
            peak = max(peak, equity)
            drawdown = peak - equity
            drawdown_pct = (drawdown / peak) * 100 if peak else 0.0
            if drawdown > max_dd:
                max_dd = drawdown
                max_dd_pct = drawdown_pct
        return _money(max_dd), round(max_dd_pct, 4)

    def _sharpe_ratio(self) -> float:
        if len(self.equity_curve) < 2:
            return 0.0

        returns: list[float] = []
        for prev, current in zip(self.equity_curve, self.equity_curve[1:]):
            prev_equity = float(prev["equity"])
            current_equity = float(current["equity"])
            if prev_equity > 0:
                returns.append((current_equity - prev_equity) / prev_equity)

        if len(returns) < 2:
            return 0.0

        avg_return = sum(returns) / len(returns)
        variance = sum((ret - avg_return) ** 2 for ret in returns) / (len(returns) - 1)
        std_dev = math.sqrt(variance)
        if std_dev == 0.0:
            return 0.0
        return round((avg_return / std_dev) * math.sqrt(len(returns)), 4)

    def _multi_sharpe(self, runs: dict[str, dict]) -> float:
        sharpes = [
            float(result["sharpe"])
            for result in runs.values()
            if result.get("number_of_trades", 0) > 0
        ]
        if not sharpes:
            return 0.0
        return round(sum(sharpes) / len(sharpes), 4)


def mean_reversion(price_history: list[PricePoint], idx: int, position: Position | None) -> Signal | None:
    """Buy after a >5% drop in 10 points; sell after +3% from entry."""
    if idx < 10:
        return None

    current = _price(price_history[idx])
    lookback = _price(price_history[idx - 10])

    if position is None:
        drop = (lookback - current) / lookback if lookback else 0.0
        if drop > 0.05:
            return {
                "action": "BUY",
                "price": current,
                "size": 10.0,
                "reason": f"price dropped {drop:.2%} over 10 points",
            }
        return None

    entry_price = float(position["entry_price"])
    gain = (current - entry_price) / entry_price if entry_price else 0.0
    if gain >= 0.03:
        return {
            "action": "SELL",
            "price": current,
            "size": position["size"],
            "reason": f"price gained {gain:.2%} from entry",
        }
    return None


def momentum(price_history: list[PricePoint], idx: int, position: Position | None) -> Signal | None:
    """Buy after a 3% rise in 5 points; sell when 5-point momentum reverses."""
    if idx < 5:
        return None

    current = _price(price_history[idx])
    previous = _price(price_history[idx - 5])
    change = (current - previous) / previous if previous else 0.0

    if position is None and change >= 0.03:
        return {
            "action": "BUY",
            "price": current,
            "size": 10.0,
            "reason": f"price rose {change:.2%} over 5 points",
        }

    if position is not None and change < 0.0:
        return {
            "action": "SELL",
            "price": current,
            "size": position["size"],
            "reason": f"5-point momentum reversed to {change:.2%}",
        }

    return None


def arbitrage_check(yes_history: list[PricePoint], no_history: list[PricePoint], idx: int) -> Signal | None:
    """Buy both YES and NO when their combined price is below 0.97."""
    if idx >= len(yes_history) or idx >= len(no_history):
        return None

    yes_price = _price(yes_history[idx])
    no_price = _price(no_history[idx])
    total = yes_price + no_price
    if total < 0.97:
        return {
            "action": "BUY",
            "price": total,
            "size": 10.0,
            "yes_price": yes_price,
            "no_price": no_price,
            "reason": f"YES+NO sum is {total:.4f}",
        }
    return None


STRATEGIES: dict[str, StrategyFn] = {
    "mean_reversion": mean_reversion,
    "momentum": momentum,
}


def _print_summary(result: dict) -> None:
    print()
    if "markets" in result:
        print("  Backtest Results (Multi-Market)")
    else:
        print(f"  Backtest Results — {result['market_slug']}")
    print("  " + "-" * 52)
    print(f"  Total PnL:        ${result['total_pnl']:+.2f}")
    print(f"  Win Rate:         {result['win_rate']:.2%}")
    print(f"  Profit Factor:    {result['profit_factor']}")
    print(f"  Sharpe:           {result['sharpe']:.4f}")
    print(f"  Max Drawdown:     ${result['max_drawdown']:.2f} ({result['max_drawdown_pct']:.2f}%)")
    print(f"  Avg Trade PnL:    ${result['average_trade_pnl']:+.2f}")
    print(f"  Best Trade:       ${result['best_trade']:+.2f}")
    print(f"  Worst Trade:      ${result['worst_trade']:+.2f}")
    print(f"  Number of Trades: {result['number_of_trades']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backtest",
        description="Run Polymarket strategy backtests against historical price data.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--slug", help="Market slug to backtest")
    group.add_argument("--slugs", help="Comma-separated market slugs to backtest")
    parser.add_argument(
        "--strategy",
        choices=sorted(STRATEGIES),
        default="mean_reversion",
        help="Built-in strategy to run",
    )
    parser.add_argument("--capital", type=float, default=255.0, help="Starting paper capital")
    parser.add_argument("--lookback-days", type=int, default=7, help="Historical lookback window")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()
    strategy_fn = STRATEGIES[args.strategy]
    backtest = Backtest(capital=args.capital)

    try:
        if args.slugs:
            slugs = [slug.strip() for slug in args.slugs.split(",") if slug.strip()]
            if not slugs:
                raise ValueError("--slugs did not contain any market slugs")
            result = backtest.run_multi(slugs, strategy_fn)
        else:
            result = backtest.run(args.slug.strip(), strategy_fn, lookback_days=args.lookback_days)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_summary(result)


if __name__ == "__main__":
    main()
