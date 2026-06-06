"""Polymarket Agent — Structured Logger.

Writes structured JSONL logs for all trading events.
Thread-safe, append-only. Never logs secrets.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class TradeLogger:
    """Thread-safe JSONL logger for Polymarket agent events.

    Logs are structured JSON objects written one-per-line (JSONL).
    Each log type writes to its own file. Files grow indefinitely —
    use log rotation or external archiving if needed.

    Usage:
        logger = TradeLogger(logs_dir="logs")
        logger.log_signal({...})
        logger.log_paper_trade({...})
        logger.log_session_summary({...})
        logger.log_daily_report({...})
    """

    def __init__(self, logs_dir: str = "logs"):
        self._logs_dir = Path(logs_dir)
        self._lock = threading.Lock()

        # Ensure subdirectories
        (self._logs_dir / "paper_trades").mkdir(parents=True, exist_ok=True)
        (self._logs_dir / "daily_reports").mkdir(parents=True, exist_ok=True)

    def _timestamp(self) -> str:
        """ISO 8601 UTC timestamp."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _append_jsonl(self, filepath: Path, entry: dict) -> None:
        """Thread-safe append of a single JSON object to a JSONL file."""
        # Ensure timestamp if not provided
        if "timestamp" not in entry:
            entry["timestamp"] = self._timestamp()

        line = json.dumps(entry, ensure_ascii=False, default=str)

        with self._lock:
            with open(filepath, "a") as f:
                f.write(line + "\n")

    # ── signal log ───────────────────────────────────────────────

    def log_signal(self, entry: dict) -> None:
        """Log a trading signal (whether acted on or not).

        Expected fields:
            signal_type, market_slug, token_id, current_price,
            trigger_reason, suggested_action, confidence,
            max_suggested_size, risks, human_confirmation_required,
            acted_upon (bool), paper_trade_ref (optional)
        """
        filepath = self._logs_dir / "signals.jsonl"
        self._append_jsonl(filepath, entry)

    # ── paper trade log ──────────────────────────────────────────

    def log_paper_trade(self, entry: dict) -> None:
        """Log a paper trade.

        Expected fields (see PAPER_TRADING_PLAN.md for full schema):
            session_id, market_slug, condition_id, token_id,
            side, action, simulated_price, simulated_size,
            simulated_cost, reason, signal_type, confidence,
            expected_edge, risk_notes, status
        """
        filepath = self._logs_dir / "paper_trades" / "trades.jsonl"
        self._append_jsonl(filepath, entry)

    # ── session summary ──────────────────────────────────────────

    def log_session_summary(self, summary: dict) -> None:
        """Log a session summary.

        Expected fields:
            session_id, start_time, end_time, initial_capital,
            current_capital, total_trades, total_pnl, win_rate, ...
        """
        filepath = self._logs_dir / "paper_sessions.jsonl"
        self._append_jsonl(filepath, summary)

    # ── daily report ─────────────────────────────────────────────

    def log_daily_report(self, report: dict) -> None:
        """Write a daily report as a standalone JSON file.

        Args:
            report: dict with keys: date, capital, trading_summary,
                    pnl, open_positions, signals_today, risk_alerts, ...
        """
        date_str = report.get(
            "date",
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        filepath = (
            self._logs_dir / "daily_reports" / f"{date_str}_report.json"
        )
        self._append_jsonl(filepath, report)
        # Note: daily reports use JSONL append too, but one per day
        # so each report is effectively a single JSON object in the file.

    # ── market snapshot ──────────────────────────────────────────

    def log_market_snapshot(self, snapshot: dict, slug: str) -> None:
        """Log a market data snapshot for later comparison.

        Args:
            snapshot: Full market summary dict (see get_market_summary)
            slug: Market slug for filename
        """
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        time_str = datetime.now(timezone.utc).strftime("%H%M%S")
        snapshot_dir = self._logs_dir / "market_snapshots" / date_str
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        safe_slug = slug.replace("/", "_").replace(" ", "_")
        filepath = snapshot_dir / f"{safe_slug}_{time_str}.json"
        self._append_jsonl(filepath, snapshot)

    # ── kill event ───────────────────────────────────────────────

    def log_kill_event(self, reason: str, pnl: Optional[float] = None) -> None:
        """Log an emergency kill switch event.

        Args:
            reason: What triggered the kill switch
            pnl: Current PnL at time of kill (optional)
        """
        entry = {
            "event": "kill_switch_engaged",
            "reason": reason,
            "pnl_at_kill": pnl,
        }
        filepath = self._logs_dir / "audit.jsonl"
        self._append_jsonl(filepath, entry)
