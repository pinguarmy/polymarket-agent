"""Generate a daily Polymarket Agent morning report.

Reads local JSONL trade and signal logs, computes daily performance metrics,
writes a JSON report artifact, and prints either a human-readable or JSON view.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any


SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import Config
except ImportError:  # pragma: no cover - supports package-style invocation.
    from src.config import Config  # type: ignore

try:
    from tracker import generate_daily_report as tracker_generate_daily_report
except ImportError:  # pragma: no cover - fallback is exercised only if tracker is absent.
    tracker_generate_daily_report = None  # type: ignore[assignment]


DEFAULT_INITIAL_CAPITAL = 255.0
SPREAD_ALERT_THRESHOLD_PCT = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _money(value: Any) -> float:
    return round(_as_float(value), 2)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _date_bounds(date_str: str) -> tuple[datetime, datetime]:
    report_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    start = datetime.combine(report_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(report_date, time.max, tzinfo=timezone.utc)
    return start, end


def _is_on_date(item: dict[str, Any], date_str: str) -> bool:
    return str(item.get("timestamp", "")).startswith(date_str)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                print(f"warning: skipping invalid JSON in {path}:{line_no}", file=sys.stderr)
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def _latest_session_capital(logs_dir: Path) -> tuple[float, float]:
    sessions = _read_jsonl(logs_dir / "paper_sessions.jsonl")
    if not sessions:
        return DEFAULT_INITIAL_CAPITAL, DEFAULT_INITIAL_CAPITAL
    latest = sessions[-1]
    initial = _money(latest.get("initial_capital", DEFAULT_INITIAL_CAPITAL))
    current = _money(latest.get("current_capital", initial))
    return initial, current


def _replay_positions_until(trades: list[dict[str, Any]], end: datetime) -> list[dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}

    for trade in trades:
        timestamp = _as_datetime(trade.get("timestamp"))
        if timestamp and timestamp > end:
            continue

        slug = str(trade.get("market_slug") or trade.get("slug") or "")
        if not slug:
            continue

        action = str(trade.get("action", "")).upper()
        status = str(trade.get("status", "")).lower()
        if action == "BUY" and status == "open":
            entry_price = _as_float(trade.get("entry_price") or trade.get("simulated_price"))
            size = _as_float(trade.get("simulated_size") or trade.get("size"))
            cost = _money(trade.get("simulated_cost") or entry_price * size)
            positions[slug] = {
                "slug": slug,
                "market_slug": slug,
                "side": trade.get("side", ""),
                "entry_price": entry_price,
                "size": size,
                "cost": cost,
                "opened_at": trade.get("timestamp", ""),
                "unrealized_pnl": 0.0,
            }
        elif action == "SELL" or status == "closed":
            positions.pop(slug, None)

    return list(positions.values())


def _replay_positions(trades: list[dict[str, Any]], through_date: str) -> list[dict[str, Any]]:
    _, end = _date_bounds(through_date)
    return _replay_positions_until(trades, end)


def _latest_signal_prices(signals: list[dict[str, Any]], through_date: str) -> dict[str, float]:
    _, end = _date_bounds(through_date)
    prices: dict[str, float] = {}
    for signal in signals:
        timestamp = _as_datetime(signal.get("timestamp"))
        if timestamp and timestamp > end:
            continue
        slug = str(signal.get("market_slug") or "")
        price = signal.get("current_price")
        if slug and price is not None:
            prices[slug] = _as_float(price)
    return prices


def _mark_unrealized(positions: list[dict[str, Any]], signals: list[dict[str, Any]], date_str: str) -> list[dict[str, Any]]:
    prices = _latest_signal_prices(signals, date_str)
    marked: list[dict[str, Any]] = []
    for position in positions:
        item = dict(position)
        mark_price = prices.get(str(item.get("market_slug") or item.get("slug") or ""))
        if mark_price is not None:
            size = _as_float(item.get("size"))
            value = _money(mark_price * size)
            item["mark_price"] = mark_price
            item["unrealized_pnl"] = _money(value - _as_float(item.get("cost")))
        else:
            item["mark_price"] = None
            item["unrealized_pnl"] = _money(item.get("unrealized_pnl", 0.0))
        marked.append(item)
    return marked


def _compute_capital(
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    date_str: str,
    initial_capital: float,
) -> tuple[float, float]:
    start, end = _date_bounds(date_str)
    cash_at_start = _money(initial_capital)
    cash_current = _money(initial_capital)

    for trade in trades:
        timestamp = _as_datetime(trade.get("timestamp"))
        if timestamp and timestamp > end:
            continue

        action = str(trade.get("action", "")).upper()
        status = str(trade.get("status", "")).lower()
        cost = _money(trade.get("simulated_cost", 0.0))
        if action == "BUY" and status == "open":
            cash_current = _money(cash_current - cost)
        elif action == "SELL" or status == "closed":
            cash_current = _money(cash_current + cost)

        if timestamp and timestamp < start:
            cash_at_start = cash_current

    if not trades:
        cash_current = _money(initial_capital)
        cash_at_start = _money(initial_capital)

    starting_positions = _replay_positions_until(trades, start)
    starting_open_cost = _money(sum(_as_float(position.get("cost")) for position in starting_positions))
    open_cost = _money(sum(_as_float(position.get("cost")) for position in positions))
    current_equity = _money(cash_current + open_cost + sum(_as_float(p.get("unrealized_pnl")) for p in positions))
    starting_equity = _money(cash_at_start + starting_open_cost)
    return starting_equity, current_equity


def _fallback_report(
    trades_today: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    capital_current: float,
    signals_today: int,
    date_str: str,
    capital_starting: float,
) -> dict[str, Any]:
    closed_trades = [
        trade
        for trade in trades_today
        if str(trade.get("status", "")).lower() == "closed"
        and trade.get("theoretical_pnl") is not None
    ]
    wins = sum(1 for trade in closed_trades if _as_float(trade.get("theoretical_pnl")) > 0)
    losses = sum(1 for trade in closed_trades if _as_float(trade.get("theoretical_pnl")) <= 0)
    realized_pnl = _money(sum(_as_float(trade.get("theoretical_pnl")) for trade in closed_trades))
    unrealized_pnl = _money(sum(_as_float(position.get("unrealized_pnl")) for position in positions))
    total_pnl = _money(realized_pnl + unrealized_pnl)
    change = _money(capital_current - capital_starting)

    return {
        "date": date_str,
        "report_generated_at": _now_iso(),
        "capital": {
            "initial": _money(capital_starting),
            "current": _money(capital_current),
            "change": change,
            "change_pct": round((change / capital_starting) * 100, 4) if capital_starting else 0.0,
        },
        "trading_summary": {
            "total_trades_today": len(trades_today),
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
        },
        "open_positions": positions,
        "signals_today": signals_today,
    }


def _base_report(
    trades_today: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    capital_current: float,
    signals_today: int,
    date_str: str,
    capital_starting: float,
) -> dict[str, Any]:
    if tracker_generate_daily_report is None:
        return _fallback_report(
            trades_today,
            positions,
            capital_current,
            signals_today,
            date_str,
            capital_starting,
        )

    report = tracker_generate_daily_report(trades_today, positions, capital_current, signals_today)
    report["date"] = date_str
    change = _money(capital_current - capital_starting)
    report["capital"] = {
        "initial": _money(capital_starting),
        "current": _money(capital_current),
        "change": change,
        "change_pct": round((change / capital_starting) * 100, 4) if capital_starting else 0.0,
    }
    report["open_positions"] = positions
    report["signals_today"] = signals_today
    report.setdefault("pnl", {})
    report["pnl"]["unrealized_pnl"] = _money(sum(_as_float(p.get("unrealized_pnl")) for p in positions))
    report["pnl"]["total_pnl"] = _money(
        _as_float(report["pnl"].get("realized_pnl")) + _as_float(report["pnl"].get("unrealized_pnl"))
    )
    return report


def _top_signals(signals_today: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    actionable = [
        signal
        for signal in signals_today
        if signal.get("signal_type") not in {"risk_validation", "order_dry_run"}
    ]
    ranked = sorted(actionable, key=lambda item: _as_float(item.get("confidence")), reverse=True)
    return [
        {
            "timestamp": signal.get("timestamp"),
            "signal_type": signal.get("signal_type"),
            "market_slug": signal.get("market_slug", ""),
            "side": signal.get("side"),
            "confidence": _as_float(signal.get("confidence")),
            "current_price": signal.get("current_price"),
            "suggested_action": signal.get("suggested_action"),
            "trigger_reason": signal.get("trigger_reason"),
        }
        for signal in ranked[:limit]
    ]


def _extract_spread_pct(text: str) -> float | None:
    match = re.search(r"spread\s+([0-9]+(?:\.[0-9]+)?)%", text, re.IGNORECASE)
    return _as_float(match.group(1)) if match else None


def _risk_alerts(signals_today: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for signal in signals_today:
        signal_type = str(signal.get("signal_type") or "")
        slug = str(signal.get("market_slug") or "")
        text_parts = [
            str(signal.get("trigger_reason") or ""),
            str(signal.get("risks") or ""),
            " ".join(str(v) for v in signal.get("violations", []) if v),
        ]
        text = " ".join(part for part in text_parts if part)

        if signal_type == "wide_spread" or "spread" in text.lower():
            spread_pct = _extract_spread_pct(text)
            if spread_pct is None or spread_pct > SPREAD_ALERT_THRESHOLD_PCT:
                key = ("wide_spread", slug, text)
                if key not in seen:
                    alerts.append({
                        "type": "wide_spread",
                        "market_slug": slug,
                        "severity": "warning",
                        "message": text or "Spread is above 5%.",
                        "spread_pct": spread_pct,
                    })
                    seen.add(key)

        if signal_type == "approaching_expiry":
            key = ("approaching_expiry", slug, text)
            if key not in seen:
                alerts.append({
                    "type": "approaching_expiry",
                    "market_slug": slug,
                    "severity": "warning",
                    "message": text or "Market is approaching expiry.",
                })
                seen.add(key)

    return alerts


def _action_items(positions: list[dict[str, Any]], risk_alerts: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    expiring_slugs = {
        str(alert.get("market_slug"))
        for alert in risk_alerts
        if alert.get("type") == "approaching_expiry" and alert.get("market_slug")
    }
    for position in positions:
        slug = str(position.get("market_slug") or position.get("slug") or "")
        if slug in expiring_slugs:
            items.append(f"Check position near resolution: {slug}")
    if positions and not items:
        items.append("Review open positions for resolution timing and exit levels.")
    return items


def generate_report(date_str: str | None = None) -> dict[str, Any]:
    report_date = date_str or _today()
    datetime.strptime(report_date, "%Y-%m-%d")

    config = Config()
    logs_dir = config.logs_dir
    trades = _read_jsonl(logs_dir / "paper_trades" / "trades.jsonl")
    signals = _read_jsonl(logs_dir / "signals.jsonl")

    trades_today = [trade for trade in trades if _is_on_date(trade, report_date)]
    signals_today = [signal for signal in signals if _is_on_date(signal, report_date)]
    positions = _mark_unrealized(_replay_positions(trades, report_date), signals, report_date)
    initial_capital, _latest_current_capital = _latest_session_capital(logs_dir)
    capital_starting, capital_current = _compute_capital(
        trades,
        positions,
        report_date,
        initial_capital,
    )

    report = _base_report(
        trades_today,
        positions,
        capital_current,
        len(signals_today),
        report_date,
        capital_starting,
    )
    alerts = _risk_alerts(signals_today)
    report["top_signals_today"] = _top_signals(signals_today)
    report["risk_alerts"] = alerts
    report["action_items"] = _action_items(positions, alerts)
    report["timestamp"] = _now_iso()
    return report


def save_report(report: dict[str, Any]) -> Path:
    config = Config()
    reports_dir = config.logs_dir / "daily_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{report['date']}_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path


def _format_money(value: Any) -> str:
    return f"${_as_float(value):,.2f}"


def _print_human(report: dict[str, Any], path: Path) -> None:
    cap = report.get("capital", {})
    pnl = report.get("pnl", {})
    summary = report.get("trading_summary", {})

    print()
    print(f"Polymarket Agent Morning Report - {report['date']}")
    print("-" * 64)
    print(
        "Capital: "
        f"{_format_money(cap.get('initial'))} -> {_format_money(cap.get('current'))} "
        f"({_as_float(cap.get('change')):+.2f} / {_as_float(cap.get('change_pct')):+.2f}%)"
    )
    print(
        "PnL: "
        f"realized={_format_money(pnl.get('realized_pnl'))} "
        f"unrealized={_format_money(pnl.get('unrealized_pnl'))} "
        f"total={_format_money(pnl.get('total_pnl'))}"
    )
    print(
        "Trades: "
        f"{int(summary.get('total_trades_today', 0))} total, "
        f"{int(summary.get('wins', 0))} wins, "
        f"{int(summary.get('losses', 0))} losses, "
        f"{_as_float(summary.get('win_rate')):.1%} win rate"
    )

    print()
    print("Open positions")
    positions = report.get("open_positions", [])
    if not positions:
        print("  None")
    for position in positions:
        slug = position.get("market_slug") or position.get("slug")
        mark = position.get("mark_price")
        mark_text = f" mark={mark:.4f}" if isinstance(mark, (int, float)) else ""
        print(
            f"  - {slug}: {position.get('side', '')} "
            f"size={_as_float(position.get('size')):.2f} "
            f"entry={_as_float(position.get('entry_price')):.4f}"
            f"{mark_text} unrealized={_format_money(position.get('unrealized_pnl'))}"
        )

    print()
    print("Top signals today")
    top_signals = report.get("top_signals_today", [])
    if not top_signals:
        print("  None")
    for signal in top_signals:
        print(
            f"  - {signal.get('signal_type')} "
            f"{signal.get('market_slug', '')} "
            f"confidence={_as_float(signal.get('confidence')):.2f}"
        )

    print()
    print("Risk alerts")
    alerts = report.get("risk_alerts", [])
    if not alerts:
        print("  None")
    for alert in alerts:
        print(f"  - {alert.get('type')}: {alert.get('market_slug', '')} - {alert.get('message', '')}")

    print()
    print("Action items")
    action_items = report.get("action_items", [])
    if not action_items:
        print("  None")
    for item in action_items:
        print(f"  - {item}")

    print()
    print(f"Saved: {path}")


def _send_report(report: dict[str, Any]) -> str:
    try:
        from dispatcher import send_message  # type: ignore
    except Exception:
        return "send_message unavailable; report was not sent"

    send_message(report)  # type: ignore[misc]
    return "sent"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Polymarket Agent morning report.")
    parser.add_argument("--date", help="Report date in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--send", action="store_true", help="Send report via send_message when available.")
    args = parser.parse_args()

    report = generate_report(args.date)
    if args.send:
        report["send_status"] = _send_report(report)
    path = save_report(report)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        _print_human(report, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
