"""Lightweight HTTP API for the Polymarket Agent dashboard.

Serves the dashboard shell and exposes read-only state plus a few local control
actions. This module intentionally uses only Python's standard HTTP server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time  # for state.json age check
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4


SRC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SRC_DIR.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import Config
    from logger import TradeLogger
    import live_trader
    import market_data
    import order_draft
    import risk_engine
    from tracker import generate_daily_report
except Exception as exc:  # pragma: no cover - import failure is fatal at startup.
    raise RuntimeError(f"dashboard_api import failed: {exc}") from exc


DEFAULT_AUTO_REFRESH_SECONDS = 15
DEFAULT_RECENT_LIMIT = 50
LOGGER = logging.getLogger("dashboard_api")

ACTIVE_PAPER_TRADER: Any | None = None
_PENDING_DRAFTS: dict[str, dict[str, Any]] = {}
_DRAFT_LOCK = threading.Lock()
_ORIGINAL_CREATE_ORDER_DRAFT: Callable[..., dict] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: Any) -> float:
    return round(_as_float(value), 2)


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _json_default(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def _safe_call(default: Any, label: str, func: Callable[[], Any]) -> Any:
    try:
        return func()
    except Exception:
        LOGGER.exception("%s failed", label)
        return default


def _read_jsonl_tail(path: Path, limit: int = DEFAULT_RECENT_LIMIT) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    lines.append(line)
    except OSError:
        LOGGER.exception("failed reading %s", path)
        return []

    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                entries.append(item)
        except json.JSONDecodeError:
            LOGGER.warning("skipping invalid JSONL row in %s", path)
    return entries


def _read_jsonl_all(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl_tail(path, limit=1_000_000)


def set_active_paper_trader(trader: Any | None) -> None:
    """Register the active in-process PaperTrader session, if one exists."""
    global ACTIVE_PAPER_TRADER
    ACTIVE_PAPER_TRADER = trader


def _normalize_draft(draft: dict[str, Any]) -> dict[str, Any]:
    if "draft_id" not in draft:
        draft["draft_id"] = str(uuid4())
    draft.setdefault("status", "pending")
    draft.setdefault("created_at", _now_iso())
    draft.setdefault("human_confirmed", False)
    return draft


def register_order_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Add an order draft to the in-memory confirmation queue."""
    normalized = _normalize_draft(draft)
    with _DRAFT_LOCK:
        _PENDING_DRAFTS[str(normalized["draft_id"])] = normalized
    return normalized


def create_order_draft(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Create and register an order draft for dashboard confirmation."""
    creator = _ORIGINAL_CREATE_ORDER_DRAFT or order_draft.create_order_draft
    return register_order_draft(creator(*args, **kwargs))


def install_order_draft_hook() -> None:
    """Patch order_draft.create_order_draft in this process to track drafts."""
    global _ORIGINAL_CREATE_ORDER_DRAFT
    if _ORIGINAL_CREATE_ORDER_DRAFT is not None:
        return
    _ORIGINAL_CREATE_ORDER_DRAFT = order_draft.create_order_draft
    order_draft.create_order_draft = create_order_draft  # type: ignore[assignment]


def _pending_confirmations() -> list[dict[str, Any]]:
    with _DRAFT_LOCK:
        return [
            dict(draft)
            for draft in _PENDING_DRAFTS.values()
            if draft.get("status", "pending") == "pending"
            and not draft.get("human_confirmed", False)
        ]


def _get_active_positions() -> list[dict[str, Any]]:
    trader = ACTIVE_PAPER_TRADER
    if trader is None:
        return []
    if hasattr(trader, "get_open_positions"):
        positions = trader.get_open_positions()
        return list(positions) if positions else []
    if hasattr(trader, "positions"):
        raw_positions = getattr(trader, "positions") or {}
        positions = raw_positions.values() if isinstance(raw_positions, dict) else raw_positions
        return [dict(getattr(position, "__dict__", position)) for position in positions]
    return []


def _get_active_summary() -> dict[str, Any]:
    trader = ACTIVE_PAPER_TRADER
    if trader is not None and hasattr(trader, "get_summary"):
        summary = trader.get_summary()
        return summary if isinstance(summary, dict) else {}
    return {}


def _positions_from_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for trade in trades:
        slug = str(trade.get("market_slug") or trade.get("slug") or "")
        if not slug:
            continue
        action = str(trade.get("action", "")).upper()
        status = str(trade.get("status", "")).lower()
        if action == "BUY" and status == "open":
            positions[slug] = {
                "slug": slug,
                "market_slug": slug,
                "side": trade.get("side", ""),
                "entry_price": trade.get("entry_price") or trade.get("simulated_price"),
                "size": trade.get("simulated_size") or trade.get("size") or 0.0,
                "cost": trade.get("simulated_cost") or 0.0,
                "opened_at": trade.get("timestamp", ""),
                "unrealized_pnl": 0.0,
            }
        elif action == "SELL" or status == "closed":
            positions.pop(slug, None)
    return list(positions.values())


def _latest_session(logs_dir: Path) -> dict[str, Any]:
    sessions = _read_jsonl_tail(logs_dir / "paper_sessions.jsonl", limit=1)
    return sessions[-1] if sessions else {}


def _balance(config: Config, summary: dict[str, Any], session: dict[str, Any]) -> tuple[float, str]:
    if not config.dry_run:
        live_balance = _safe_call({}, "live balance", lambda: live_trader.get_balance(config))
        if isinstance(live_balance, dict) and "balance_pusd" in live_balance:
            return _money(live_balance.get("balance_pusd")), str(live_balance.get("wallet") or config.wallet_address)

    if summary:
        return _money(summary.get("current_capital", 0.0)), config.wallet_address
    if session:
        return _money(session.get("current_capital", 0.0)), config.wallet_address
    return 0.0, config.wallet_address


def _mode(config: Config) -> str:
    """Return current mode. Checks module-level LIVE_TRADING flag."""
    import risk_engine as re
    if not re.LIVE_TRADING:
        return "killed"
    if config.live_trading:
        return "live"
    return "dry_run"


# ── Paper Trader Live State ──

TRADER_LOG = PROJECT_ROOT / "logs" / "realtime_trader.log"
TRADER_STATE = PROJECT_ROOT / "logs" / "trader_state.json"
SKIPPED_LOG = PROJECT_ROOT / "logs" / "skipped_signals.jsonl"
CHAINLINK_HEALTH = PROJECT_ROOT / "logs" / "chainlink_health.json"


def _get_paper_trader_state() -> dict:
    """Read the trader_state.json written every 0.5s by realtime_trader.py.
    
    Much faster and more accurate than parsing log lines.
    Falls back to log parsing if JSON is not available.
    """
    state = {
        "btc_price": None,
        "btc_change": None,
        "time_remaining": None,
        "yes_bid": None,
        "yes_ask": None,
        "market_slug": None,
        "trades": [],
        "running": False,
        "entries_this_market": 0,
        "data_age_seconds": None,
        "data_updated_at": None,
        "last_trade_price": None,
        "daily_pnl": 0.0,
        "trades_today": 0,
    }

    # Try reading the JSON state file first (0.5s refresh)
    if TRADER_STATE.exists():
        try:
            data = json.loads(TRADER_STATE.read_text())
            age = time.time() - data.get("ts", 0)
            if age < 10:  # Fresh enough
                state["market_slug"] = data.get("slug", "")
                state["btc_price"] = data.get("btc")
                state["btc_change"] = data.get("btc_change")
                state["time_remaining"] = data.get("remaining")
                state["yes_bid"] = data.get("yes_bid")
                state["yes_ask"] = data.get("yes_ask")
                state["entries_this_market"] = data.get("entries", 0)
                state["daily_pnl"] = data.get("daily_pnl", 0.0)
                state["trades_today"] = data.get("trades_today", 0)
                state["running"] = age < 120
                state["data_age_seconds"] = round(age, 1)
                return state
        except Exception:
            pass

    # Fallback: parse log file
    
    try:
        text = TRADER_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return state
    
    lines = text.splitlines()
    
    # Check if process is alive by log age or process list
    try:
        mtime = os.path.getmtime(str(TRADER_LOG))
        state["running"] = (time.time() - mtime) < 120  # updated within 2 min
    except Exception:
        pass
    
    # Also check process list as fallback (status lines use \r, no newlines)
    if not state["running"]:
        try:
            import subprocess as _sp
            result = _sp.run(["ps", "aux"], capture_output=True, text=True, timeout=3)
            state["running"] = "realtime_trader.py" in result.stdout
        except Exception:
            pass
    
    # Extract latest status line (format: "  123s | BTC=$78255 | Δ$+0 | YES=0.010/0.990")
    for line in reversed(lines):
        line = line.strip()
        # Parse status line
        import re as _re
        m = _re.search(r"(\d+)s\s*\|", line)
        if m:
            state["time_remaining"] = int(m.group(1))
        
        m = _re.search(r"BTC=\$?([\d.,]+)", line)
        if m:
            try:
                state["btc_price"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        
        m = _re.search(r"Δ\$([+-]?\d+)", line)
        if m:
            try:
                state["btc_change"] = int(m.group(1))
            except ValueError:
                pass
        
        m = _re.search(r"YES[=≈☆]\s*([\d.]+)", line)
        if m:
            try:
                # Real CTF price from CLOB V2 midpoint
                price = float(m.group(1))
                if 0 < price < 1:
                    state["yes_bid"] = price
                    state["yes_ask"] = price
            except (ValueError, TypeError):
                pass
        
        m = _re.search(r"\[x(\d+)\]", line)
        if m:
            try:
                state["entries_this_market"] = int(m.group(1))
            except ValueError:
                pass
        
        # If this line has BTC data, it's the latest status
        if state["btc_price"] is not None:
            break
    
    # Extract all DRY RUN trades (signal lines)
    for line in lines:
        if "DRY:" in line or "DRY RUN" in line.upper():
            state["trades"].append(line.strip())
        elif "Follow" in line or "FOLLOW" in line:
            state["trades"].append(line.strip())
    
    # YES☆ values are REAL CTF prices from CLOB V2 midpoint endpoint
    if state.get("yes_bid") is not None and state.get("yes_ask") is not None:
        state["ui_yes_bid"] = state["yes_bid"]
        state["ui_yes_ask"] = state["yes_ask"]
    else:
        state["ui_yes_bid"] = None
        state["ui_yes_ask"] = None
    
    # Extract latest market slug
    for line in reversed(lines):
        m = _re.search(r"NEW MARKET:\s*(\S+)", line)
        if m:
            state["market_slug"] = m.group(1)
            break
    
    # Trim trades to last 10
    state["trades"] = state["trades"][-10:]
    
    return state


# ── Skipped Signals ──
def _get_skipped_signals() -> list[dict]:
    if not SKIPPED_LOG.exists():
        return []
    try:
        lines = SKIPPED_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        signals = []
        for line in lines[-20:]:
            line = line.strip()
            if line:
                signals.append(json.loads(line))
        return signals
    except Exception:
        return []


# ── Chainlink Health ──
def _get_chainlink_health() -> dict:
    if not CHAINLINK_HEALTH.exists():
        return {"healthy": False, "error": "no health file"}
    try:
        data = json.loads(CHAINLINK_HEALTH.read_text())
        return {
            "healthy": data.get("is_healthy", False),
            "age_seconds": data.get("age_seconds"),
            "latency_ms": data.get("latency_ms"),
            "latest_price": data.get("latest_price"),
            "messages_per_minute": data.get("messages_per_minute"),
        }
    except Exception:
        return {"healthy": False, "error": "parse error"}


# ── PnL History ──
def _build_pnl_history() -> dict:
    """Build time-series PnL data from BTC paper backtest results."""
    import json as _json
    result = {"strategies": {}, "timestamps": [], "cumulative_pnl": []}

    btc_path = PROJECT_ROOT / "logs" / "dune_pnl.json"
    if not btc_path.exists():
        btc_path = PROJECT_ROOT / "logs" / "btc_paper_pnl_v3.json"
    if not btc_path.exists():
        btc_path = PROJECT_ROOT / "logs" / "btc_paper_pnl_v2.json"
    if btc_path.exists():
        with open(btc_path) as f:
            btc = _json.load(f)
        trades = btc.get("trades", [])
        points = []
        for t in trades:
            points.append({
                "timestamp": t.get("slug", ""),
                "pnl": t.get("pnl", 0),
                "direction": t.get("direction", ""),
                "category": t.get("category", ""),
            })
        # Use strategy name and win_rate from the data if available
        strategy_name = btc.get("strategy", "BTC 5-Min").replace("-", " ").title()
        result["strategies"][strategy_name] = {
            "label": strategy_name,
            "color": "#00ff88",
            "points": points,
            "total_pnl": btc.get("total_pnl", 0),
            "win_rate": btc.get("win_rate", 0),
        }

    return result


def _recent_today(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    today = _today_prefix()
    return [item for item in items if str(item.get("timestamp", "")).startswith(today)]


def _daily_stats(
    trades_today: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    capital: float,
    signals_today: int,
) -> dict[str, Any]:
    report = _safe_call(
        {},
        "daily report",
        lambda: generate_daily_report(trades_today, positions, capital, signals_today),
    )
    trading_summary = report.get("trading_summary", {}) if isinstance(report, dict) else {}
    pnl = report.get("pnl", {}) if isinstance(report, dict) else {}
    pnl_today = _money(pnl.get("total_pnl", 0.0))
    denominator = capital - pnl_today
    return {
        "trades_today": int(trading_summary.get("total_trades_today", len(trades_today)) or 0),
        "signals_today": int(signals_today),
        "win_rate": _as_float(trading_summary.get("win_rate", 0.0)),
        "pnl_today": pnl_today,
        "pnl_today_pct": round((pnl_today / denominator) * 100, 4) if denominator else 0.0,
    }


def _watchlist(slugs: list[str]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for slug in slugs:
        slug = slug.strip()
        if not slug:
            continue
        summary = _safe_call(None, f"watchlist market {slug}", lambda slug=slug: market_data.get_market_summary(slug))
        if isinstance(summary, dict):
            summaries.append(summary)
        else:
            summaries.append({"market_slug": slug, "error": "market summary unavailable", "data_timestamp": _now_iso()})
    return summaries


def build_dashboard_state(config: Config, watchlist_slugs: list[str]) -> dict[str, Any]:
    logs_dir = config.logs_dir
    all_trades = _read_jsonl_all(logs_dir / "paper_trades" / "trades.jsonl")
    recent_trades = all_trades[-DEFAULT_RECENT_LIMIT:]
    all_signals = _read_jsonl_all(logs_dir / "signals.jsonl")
    recent_signals = all_signals[-DEFAULT_RECENT_LIMIT:]
    session = _latest_session(logs_dir)
    summary = _get_active_summary()

    positions = _get_active_positions() or _positions_from_trades(all_trades)
    balance_pusd, wallet = _balance(config, summary, session)
    
    # Read backtest PnL for header display (same source as PnL chart)
    # Try Dune backtest first (more historical data), then v3, then v2
    sources = [
        PROJECT_ROOT / "logs" / "dune_pnl.json",
        PROJECT_ROOT / "logs" / "btc_paper_pnl_v3.json",
        PROJECT_ROOT / "logs" / "btc_paper_pnl_v2.json",
    ]
    backtest_total_pnl = 0.0
    btc_pnl_path = None
    for src in sources:
        if src.exists():
            btc_pnl_path = src
            break
    
    if btc_pnl_path:
        try:
            import json as _json
            with open(btc_pnl_path) as f:
                btc = _json.load(f)
            backtest_total_pnl = btc.get("total_pnl", 0.0)
        except Exception:
            pass
    
    total_pnl = backtest_total_pnl or _money(summary.get("total_pnl", session.get("total_pnl", 0.0)) if summary or session else 0.0)
    total_pnl_pct = round((total_pnl / balance_pusd) * 100, 2) if balance_pusd else 0.0
    trades_today = _recent_today(all_trades)
    signals_today = len(_recent_today(all_signals))

    # Merge live paper trader state from realtime_trader.log
    paper = _get_paper_trader_state()

    return {
        "mode": _mode(config),
        "balance_pusd": balance_pusd,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "wallet": wallet,
        "timestamp": _now_iso(),
        "open_positions": positions,
        "pending_confirmations": _pending_confirmations(),
        "recent_signals": recent_signals,
        "open_orders": (
            []
            if config.dry_run
            else _safe_call([], "open orders", lambda: live_trader.get_open_orders(config=config))
        ),
        "recent_trades": recent_trades,
        "watchlist": _watchlist(watchlist_slugs),
        "daily_stats": _daily_stats(trades_today, positions, balance_pusd, signals_today),
        "auto_refresh_seconds": 3,  # faster refresh for live trader
        # Live paper trader state
        "paper_trader": paper,
    }


def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = max(0, int(raw_length))
    except ValueError:
        length = 0
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        raise ValueError("request body must be a JSON object")


def _rewrite_env_bool(env_path: Path, key: str, value: bool) -> None:
    text_value = "true" if value else "false"
    lines: list[str] = []
    found = False
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            rewritten.append(f"{key}={text_value}")
            found = True
        else:
            rewritten.append(line)
    if not found:
        rewritten.append(f"{key}={text_value}")
    env_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    os.environ[key] = text_value


def toggle_dry_run(config: Config) -> Config:
    env_path = config.project_root / ".env"
    _rewrite_env_bool(env_path, "DRY_RUN", not config.dry_run)
    return Config(str(env_path))


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: "DashboardHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=_json_default, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/":
                self._serve_dashboard()
                return
            if path == "/paper":
                self._serve_paper_trader()
                return
            if path == "/api/state":
                self._send_json(HTTPStatus.OK, build_dashboard_state(self.server.config, self.server.watchlist_slugs))
                return
            if path == "/api/pnl-history":
                self._send_json(HTTPStatus.OK, _build_pnl_history())
                return
            if path == "/api/paper-state":
                self._send_json(HTTPStatus.OK, _get_paper_trader_state())
                return
            if path == "/api/skipped-signals":
                self._send_json(HTTPStatus.OK, _get_skipped_signals())
                return
            if path == "/api/chainlink-health":
                self._send_json(HTTPStatus.OK, _get_chainlink_health())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            LOGGER.exception("GET request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/kill":
                result = live_trader.emergency_kill_switch(self.server.config, self.server.trade_logger)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/api/resume":
                from risk_engine import reset_kill_switch as reset_kill
                reset_kill(self.server.trade_logger)
                self._send_json(HTTPStatus.OK, {"status": "resumed", "mode": _mode(self.server.config)})
                return
            if path == "/api/toggle-mode":
                self.server.config = toggle_dry_run(self.server.config)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "dry_run": self.server.config.dry_run,
                        "live_trading": self.server.config.live_trading,
                        "mode": _mode(self.server.config),
                    },
                )
                return
            if path == "/api/confirm":
                self._confirm_draft()
                return
            if path == "/api/cancel-draft":
                self._cancel_draft()
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            LOGGER.exception("POST request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _serve_dashboard(self) -> None:
        dashboard_path = PROJECT_ROOT / "dashboard.html"
        if not dashboard_path.exists():
            self._send_text(HTTPStatus.NOT_FOUND, "dashboard.html not found")
            return
        body = dashboard_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _serve_paper_trader(self) -> None:
        path = PROJECT_ROOT / "paper_trader.html"
        if not path.exists():
            self._send_text(HTTPStatus.NOT_FOUND, "paper_trader.html not found")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _confirm_draft(self) -> None:
        data = _read_body(self)
        draft_id = str(data.get("draft_id") or "")
        if not draft_id:
            raise ValueError("draft_id is required")
        with _DRAFT_LOCK:
            draft = _PENDING_DRAFTS.get(draft_id)
        if draft is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "draft not found", "draft_id": draft_id})
            return
        result = order_draft.submit_if_confirmed("CONFIRM", draft, self.server.config)
        result_draft = result.get("draft", draft) if isinstance(result, dict) else draft
        result_draft["status"] = result.get("status", "confirmed") if isinstance(result, dict) else "confirmed"
        with _DRAFT_LOCK:
            _PENDING_DRAFTS[draft_id] = result_draft
        self._send_json(HTTPStatus.OK, result)

    def _cancel_draft(self) -> None:
        data = _read_body(self)
        draft_id = str(data.get("draft_id") or "")
        if not draft_id:
            raise ValueError("draft_id is required")
        with _DRAFT_LOCK:
            draft = _PENDING_DRAFTS.get(draft_id)
            if draft is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "draft not found", "draft_id": draft_id})
                return
            draft["status"] = "cancelled"
            draft["cancelled_at"] = _now_iso()
        self._send_json(HTTPStatus.OK, {"status": "cancelled", "draft": draft})


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], config: Config, watchlist_slugs: list[str]):
        super().__init__(server_address, handler_class)
        self.config = config
        self.watchlist_slugs = watchlist_slugs
        self.trade_logger = TradeLogger(str(config.logs_dir))


def start_server(port: int = 8765, watchlist: list[str] | None = None, host: str = "127.0.0.1") -> tuple[DashboardHTTPServer, threading.Thread]:
    """Start the dashboard server in a background thread."""
    install_order_draft_hook()
    config = Config()
    server = DashboardHTTPServer((host, port), DashboardRequestHandler, config, watchlist or [])
    thread = threading.Thread(target=server.serve_forever, name="dashboard-api", daemon=True)
    thread.start()
    return server, thread


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Agent dashboard API")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--watchlist", default="", help="Comma-separated market slugs")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    watchlist = [slug.strip() for slug in args.watchlist.split(",") if slug.strip()]
    server, _thread = start_server(port=args.port, watchlist=watchlist, host=args.host)
    LOGGER.info("dashboard API listening on http://%s:%s", args.host, args.port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        LOGGER.info("shutting down dashboard API")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
