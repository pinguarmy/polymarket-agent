#!/usr/bin/env python3
"""Real-time BTC 5-min trader — FOLLOW manipulation strategy.

Listens to:
  - Binance BTC/USDT ticker (WebSocket, free)
  - Polymarket CLOB order book (REST polling, 1s cycle)

Detects: whale manipulation by checking BTC direction vs YES price.
If BTC disagrees with YES price, someone is accumulating expecting reversal.
We follow them.

Usage:
  python3 src/realtime_trader.py --dry-run          # paper trading
  python3 src/realtime_trader.py --live              # LIVE (requires risk checks)
"""

import argparse
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Add project root
SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC.parent))

# WebSocket
try:
    import websocket
except ImportError:
    print("Installing websocket-client...")
    os.system("pip3 install websocket-client -q")
    import websocket

from dotenv import load_dotenv
load_dotenv(SRC.parent / ".env")

# Config + Risk + DB
from config import Config
from risk_engine import RiskEngine
from db import Database
from chainlink_helpers import get_latest_chainlink_price, get_chainlink_price_at_or_before

# CLOB
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OpenOrderParams, OrderArgs, OrderPayload, OrderType

logger = logging.getLogger("realtime_trader")

# DB for BTC tick recording
TRADER_DB = Database(str(SRC.parent / "data" / "btc5m.db"))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@ticker"
# BTC price source: Binance (most liquid, always fresh, free)
BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
POLL_INTERVAL = 0.5      # Poll CLOB midpoint every 0.5s (avoid rate limiting)
BTC_POLL_INTERVAL = 2.0  # Poll Binance every 2s (most liquid source, always fresh)

# ── Strategy thresholds ──
MIN_BTC_MOVE_YES = 3.0       # BUY YES: BTC down $3 triggers
MIN_BTC_MOVE = 10.0          # BUY NO: BTC up $10 triggers (was $5)
ENTRY_DELAY = 20.0            # Wait 20s before first trade
ENTRY_WINDOW_END = 270.0      # Don't enter in last 30s
YES_LOW_THRESHOLD = 0.30      # BTC up + YES <= this → BUY NO (was 0.25)
YES_HIGH_THRESHOLD = 0.45     # BTC down + YES >= this → BUY YES
MAX_ENTRIES_PER_MARKET = 2    # Up to 2 entries per window (was 1 — more data supports 2)
SCALE_SIZE = 8.0              # $ per entry
SIGNAL_PERSIST_SEC = 20.0     # Wait 20s between scaling-in entries

# ── Stop-loss thresholds (asymmetric: BUY_NO tighter than BUY_YES) ──
STOP_LOSS_YES_MOVE = 0.12     # Exit if YES moves this much against entry (kept for fallback)
STOP_LOSS_NO_MOVE = 0.06      # BUY NO: tighter stop

# ── BTC-based stop-loss (primary — replaces YES-based for BUY YES) ──
# Uses entry_btc_change × multiplier. BTC is objective, YES is manipulator's playground.
STOP_LOSS_BTC_MULTIPLIER_YES = 3.0  # BUY YES: exit if BTC keeps falling by 3× entry move (was 4×)
STOP_LOSS_BTC_MULTIPLIER_NO = 3.0   # BUY NO: exit if BTC keeps rising by 3× entry move (was 3×)
STOP_LOSS_BTC_MIN = 10.0         # Never tighter than $10 absolute

# ── Take-profit thresholds (configurable via env) ──
# Sell before settlement to lock in profit.
import os as _os
TAKE_PROFIT_YES = float(_os.getenv("TAKE_PROFIT_YES", "0.85"))
TAKE_PROFIT_NO = float(_os.getenv("TAKE_PROFIT_NO", "0.88"))

# ── Direction-specific limits (BUY NO is higher risk) ──
MAX_ENTRIES_BUY_YES = 1       # BUY YES: max 1 entry per market
MAX_ENTRIES_BUY_NO = 1        # BUY NO: max 1 entry per market
ENTRY_WINDOW_END_NO = 180     # BUY NO: don't enter after 180s (BTC momentum rarely reverses late)


# ── WS Retry State ──
_ws_fail_count = 0
_MAX_WS_RETRIES = 9999  # effectively infinite — keep retrying forever


class RealtimeTrader:
    def __init__(self, max_cost=5.0, dry_run=True):
        self.max_cost = max_cost
        self.dry_run = dry_run
        self.running = True
        self.lock = threading.Lock()
        self._binance_reconnect_lock = threading.Lock()
        self._binance_reconnecting = False

        # BTC state
        self.btc_price = None
        self.btc_open = None
        self.market_open_ts = None

        # Market state
        self.active_slug = None
        self.active_condition_id = None
        self.yes_token_id = None
        self.no_token_id = None
        self.yes_bid = None
        self.yes_ask = None

        # Scaling-in state
        self.entries_this_market = 0
        self.last_entry_time = 0.0
        self.entry_yes_prices = []  # YES midprice at each entry
        self.entry_prices = []      # actual buy prices
        self.entry_sizes = []       # actual filled share sizes
        self.first_btc_change = None  # BTC change at first entry (for conviction check)

        # BTC price tracking
        self.ws_last_ping = 0.0     # last WS update timestamp (0 = never)
        self.last_rest_price = None
        self.last_rest_time = 0.0
        self.last_btc_time = 0.0

        # Stats
        self.trades_today = 0
        self.last_trade_time = 0
        self.daily_pnl = 0.0  # simulated PnL tracker
        self._consecutive_failures = 0
        self._last_r3_slug = ""  # rate-limit R3 log spam
        self._last_status_ts = 0.0  # rate-limit status line output (every 5s)
        self._last_yes_update = 0.0  # timestamp of last successful YES price fetch

        # CLOB book state
        self._last_clob_data = {}
        self._last_clob_ts = 0.0
        self._last_clob_poll = 0.0

        # Chainlink settlement state
        self._chainlink_entry_btc = None   # BTC price at entry (settlement reference)

        # Init config + risk engine
        self.config = Config()
        self.risk_engine = RiskEngine(self.config)

        # Init CLOB client if live
        if not dry_run:
            self.clob = ClobClient(
                host='https://clob.polymarket.com',
                key=os.getenv('POLY_PK'),
                chain_id=137, signature_type=1,
                funder=os.getenv('POLY_PROXY_ADDRESS'),
            )
            creds = ApiCreds(
                api_key=os.getenv('POLYMARKET_API_KEY'),
                api_secret=os.getenv('POLYMARKET_SECRET'),
                api_passphrase=os.getenv('POLYMARKET_PASSPHRASE'),
            )
            self.clob.set_api_creds(creds)
        else:
            self.clob = None

        print(f"RealtimeTrader: FOLLOW strategy | dry_run={dry_run} | max_cost=${max_cost}")

    # ── Binance WebSocket (with DNS pre-resolution + fallback) ──
    _on_binance_first_msg = True

    def _on_binance_message(self, ws, message):
        if RealtimeTrader._on_binance_first_msg:
            RealtimeTrader._on_binance_first_msg = False
            print("  ✅ Binance WebSocket connected — real-time price feed active")
        try:
            data = json.loads(message)
            price = float(data.get('c', 0))
            with self.lock:
                self.btc_price = price
                self.ws_last_ping = time.time()
        except Exception as e:
            logger.warning("Failed to process Binance message: %s", e)

    def _on_binance_error(self, ws, error):
        global _ws_fail_count
        _ws_fail_count += 1
        err_str = str(error)
        # Suppress repetitive DNS errors — only print every 5th
        if _ws_fail_count % 5 == 1:
            print(f"  Binance WS error (#{_ws_fail_count}): {err_str}")

    def _on_binance_close(self, ws, status, msg):
        global _ws_fail_count
        if _ws_fail_count >= _MAX_WS_RETRIES:
            # Give up on WS, REST will serve
            if _ws_fail_count == _MAX_WS_RETRIES:
                print(f"  Binance WS: giving up after {_MAX_WS_RETRIES} failures (REST fallback active)")
                _ws_fail_count += 1  # prevent re-entry
            return
        if self.running:
            if not self._begin_binance_reconnect():
                return
            try:
                backoff = min(10, 3 * (1 << _ws_fail_count))  # 3, 6, 12, 24s capped at 10
                print(f"  Binance WS: reconnecting in {backoff}s...")
                time.sleep(backoff)
                self._start_binance()
            finally:
                self._end_binance_reconnect()

    def _begin_binance_reconnect(self):
        with self._binance_reconnect_lock:
            if self._binance_reconnecting:
                return False
            self._binance_reconnecting = True
            return True

    def _end_binance_reconnect(self):
        with self._binance_reconnect_lock:
            self._binance_reconnecting = False

    def _start_binance(self):
        """Start Binance WebSocket with DNS pre-resolution."""
        # Pre-resolve DNS to avoid [Errno 8] in the websocket library
        try:
            addrs = socket.getaddrinfo("stream.binance.com", 9443)
            _ = addrs  # DNS resolved OK
        except Exception as e:
            print(f"  Binance DNS pre-resolve failed: {e}")
            if _ws_fail_count >= _MAX_WS_RETRIES:
                print("  WS skipped (max retries) — REST fallback active")
                return

        ws = websocket.WebSocketApp(
            BINANCE_WS,
            on_message=self._on_binance_message,
            on_error=self._on_binance_error,
            on_close=self._on_binance_close,
        )
        thread = threading.Thread(
            target=lambda: ws.run_forever(reconnect=3, ping_interval=30, ping_timeout=10),
            daemon=True,
        )
        thread.start()
        # Don't print "connected" until we actually get a message
        if _ws_fail_count == 0:
            print("  Binance WebSocket starting... (real-time price feed)")

    # ── Market Discovery ──
    def _discover_market(self):
        """Find current BTC 5-min market via Gamma API."""
        now = datetime.now(timezone.utc)
        now_ts = int(now.timestamp())

        window_start = (now_ts // 300) * 300
        slug = f"btc-updown-5m-{window_start}"

        # Same market still active. If crash recovery restored only the slug
        # from an older checkpoint, continue discovery so token IDs are rebuilt.
        if self.active_slug == slug:
            if self.market_open_ts and time.time() > self.market_open_ts + 300:
                print(f"\n  Market closed: {slug}")
                self.active_slug = None
                self._reset_market_state()
                return
            if self.active_condition_id and self.yes_token_id and self.no_token_id:
                return
            logger.warning("Active market %s missing token IDs; rediscovering", slug)

        # Query Gamma (no &limit=1 — causes intermittent empty responses)
        url = f"{GAMMA_API}/markets?slug={slug}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polymarket/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            logger.warning("Failed to discover market %s: %s", slug, e)
            return

        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            return

        m = markets[0]
        condition_id = m.get("conditionId")
        clob_ids = m.get("clobTokenIds", [])
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)

        # Skip if market is already resolved (outcomePrices are final)
        op = m.get("outcomePrices")
        if isinstance(op, str):
            try:
                op = json.loads(op)
            except Exception as e:
                logger.warning("Failed to parse outcomePrices for %s: %s", slug, e)
                op = None
        if op and isinstance(op, list) and len(op) >= 2:
            try:
                if float(op[0]) in (0.0, 1.0) or float(op[1]) in (0.0, 1.0):
                    # Market already settled — skip
                    return
            except (ValueError, TypeError):
                pass

        yes_token = clob_ids[0] if len(clob_ids) > 0 else None
        no_token = clob_ids[1] if len(clob_ids) > 1 else None

        if condition_id and yes_token and no_token:
            if self.active_slug != slug:
                self._reset_market_state()
            self.active_slug = slug
            self.active_condition_id = condition_id
            self.yes_token_id = yes_token
            self.no_token_id = no_token
            self.market_open_ts = window_start

            with self.lock:
                self.btc_open = self.btc_price

            print(f"\n  NEW MARKET: {slug}")
            print(f"  BTC open: ${self.btc_open:.1f}" if self.btc_open else "  BTC open: waiting...")
            print(f"  YES: {yes_token[:20]}... NO: {no_token[:20]}...")

    def _reset_market_state(self):
        """Reset scaling-in state for a new market."""
        self.entries_this_market = 0
        self.last_entry_time = 0.0
        self.entry_yes_prices = []
        self.entry_prices = []
        self.entry_sizes = []
        self.first_btc_change = None
        self._chainlink_entry_btc = None
        # Also reset prices — don't carry stale data across windows
        self.yes_bid = None
        self.yes_ask = None

    # ── Order Book (CLOB V2 Midpoint) ──
    def _poll_orderbook(self):
        """Poll CLOB V2 midpoint API for the real YES/NO price.
        
        Uses the FULL CTF token IDs from Gamma's clobTokenIds.
        Gamma bestBid/bestAsk uses CONDITIONAL tokens which have different
        prices than the CTF tokens (what the Polymarket UI shows).
        
        The /midpoint endpoint returns the actual CTF midpoint price.
        """
        if not self.active_slug:
            return
        if not self.yes_token_id:
            return

        # Poll CLOB V2 midpoint for YES token (Up outcome)
        yes_url = f"https://clob.polymarket.com/midpoint?token_id={self.yes_token_id}"
        no_url = f"https://clob.polymarket.com/midpoint?token_id={self.no_token_id}"
        
        yes_mid = None
        no_mid = None
        
        try:
            req = urllib.request.Request(yes_url, headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            yes_mid = float(data.get("mid", 0))
        except Exception as e:
            logger.warning("Failed to fetch YES midpoint: %s", e)
        
        try:
            req = urllib.request.Request(no_url, headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            no_mid = float(data.get("mid", 0))
        except Exception as e:
            logger.warning("Failed to fetch NO midpoint: %s", e)
        
        # Also poll Gamma for outcomePrices as fallback
        gamma_mid = None
        gamma_url = f"{GAMMA_API}/markets?slug={self.active_slug}"
        try:
            req = urllib.request.Request(gamma_url, headers={"User-Agent": "polymarket/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                gamma_data = json.loads(resp.read())
            m = gamma_data[0] if isinstance(gamma_data, list) else gamma_data
            op = m.get("outcomePrices")
            if op and isinstance(op, str):
                prices = json.loads(op)
                gamma_mid = float(prices[0])  # Up price from Gamma
        except Exception as e:
            logger.warning("Failed to fetch Gamma fallback price for %s: %s", self.active_slug, e)

        with self.lock:
            if yes_mid is not None and 0.001 < yes_mid < 1.0:
                # Use CLOB midpoint — this is the REAL CTF price
                self.yes_bid = yes_mid
                self.yes_ask = yes_mid
                self._last_yes_update = time.time()
            elif no_mid is not None and 0.001 < no_mid < 1.0:
                # Derive YES price from NO midpoint: YES = 1.0 - NO
                derived = round(1.0 - no_mid, 4)
                if 0.001 < derived < 1.0:
                    self.yes_bid = derived
                    self.yes_ask = derived
                    self._last_yes_update = time.time()
            elif gamma_mid is not None and 0.001 < gamma_mid < 1.0:
                # Fallback to Gamma outcomePrices
                self.yes_bid = gamma_mid
                self.yes_ask = gamma_mid
                self._last_yes_update = time.time()
            elif self.yes_bid is not None:
                # Keep last known price — don't set to None
                # _last_yes_update stays stale but bid/ask are usable
                pass
            else:
                self.yes_bid = None
                self.yes_ask = None

    # ── CLOB /book Full Orderbook ──
    def _poll_clob_book(self):
        """Read full CLOB orderbook for both YES and NO tokens.

        Polls /book, /midpoint, and /last-trade-price endpoints for each token.
        Writes snapshots to clob_orderbook_snapshots table.
        """
        if not self.yes_token_id or not self.no_token_id:
            return

        CLOB = "https://clob.polymarket.com"
        result = {
            # YES
            "yes_midpoint": None, "yes_best_bid": None, "yes_best_ask": None,
            "yes_spread": None, "yes_spread_pct": None,
            "yes_bid_depth_top_1": None, "yes_ask_depth_top_1": None,
            "yes_bid_depth_top_3": None, "yes_ask_depth_top_3": None,
            "yes_total_bid_depth": None, "yes_total_ask_depth": None,
            "yes_last_trade_price": None, "yes_last_trade_side": None,
            # NO
            "no_midpoint": None, "no_best_bid": None, "no_best_ask": None,
            "no_spread": None, "no_spread_pct": None,
            "no_bid_depth_top_1": None, "no_ask_depth_top_1": None,
            "no_bid_depth_top_3": None, "no_ask_depth_top_3": None,
            "no_total_bid_depth": None, "no_total_ask_depth": None,
            "no_last_trade_price": None, "no_last_trade_side": None,
            # Metadata
            "clob_timestamp_ms": None, "received_at_ms": int(time.time() * 1000),
            "raw_book_payload_yes": None, "raw_book_payload_no": None,
            "raw_midpoint_payload_yes": None, "raw_midpoint_payload_no": None,
            "raw_last_trade_payload_yes": None, "raw_last_trade_payload_no": None,
        }

        for token_id, prefix in [(self.yes_token_id, "yes"), (self.no_token_id, "no")]:
            try:
                # /book
                req = urllib.request.Request(
                    f"{CLOB}/book?token_id={token_id}",
                    headers={"User-Agent": "curl/7.0"}
                )
                resp = json.loads(urllib.request.urlopen(req, timeout=3).read())
                result[f"raw_book_payload_{prefix}"] = json.dumps(resp)
                bids = resp.get("bids", [])
                asks = resp.get("asks", [])
                result["clob_timestamp_ms"] = resp.get("timestamp")

                if bids:
                    # best_bid = MAX price among bids (highest buy order)
                    best_bid = max(float(b["price"]) for b in bids if isinstance(b, dict))
                    result[f"{prefix}_best_bid"] = best_bid
                    # Depth at best_bid level
                    best_bid_size = max(
                        float(b["size"]) for b in bids if isinstance(b, dict)
                        and abs(float(b["price"]) - best_bid) < 0.0001
                    )
                    result[f"{prefix}_bid_depth_top_1"] = best_bid_size
                    if len(bids) >= 3:
                        # Top 3 bid levels (sorted descending by price)
                        top_bids = sorted(
                            [b for b in bids if isinstance(b, dict)],
                            key=lambda b: float(b["price"]), reverse=True
                        )[:3]
                        result[f"{prefix}_bid_depth_top_3"] = sum(
                            float(b["size"]) for b in top_bids
                        )
                    result[f"{prefix}_total_bid_depth"] = sum(
                        float(b["size"]) for b in bids if isinstance(b, dict)
                    )
                if asks:
                    # best_ask = MIN price among asks (lowest sell order)
                    best_ask = min(float(a["price"]) for a in asks if isinstance(a, dict))
                    result[f"{prefix}_best_ask"] = best_ask
                    best_ask_size = min(
                        float(a["size"]) for a in asks if isinstance(a, dict)
                        and abs(float(a["price"]) - best_ask) < 0.0001
                    )
                    result[f"{prefix}_ask_depth_top_1"] = best_ask_size
                    if len(asks) >= 3:
                        # Top 3 ask levels (sorted ascending by price)
                        top_asks = sorted(
                            [a for a in asks if isinstance(a, dict)],
                            key=lambda a: float(a["price"])
                        )[:3]
                        result[f"{prefix}_ask_depth_top_3"] = sum(
                            float(a["size"]) for a in top_asks
                        )
                    result[f"{prefix}_total_ask_depth"] = sum(
                        float(a["size"]) for a in asks if isinstance(a, dict)
                    )

                # /midpoint
                req = urllib.request.Request(
                    f"{CLOB}/midpoint?token_id={token_id}",
                    headers={"User-Agent": "curl/7.0"}
                )
                mp = json.loads(urllib.request.urlopen(req, timeout=3).read())
                result[f"raw_midpoint_payload_{prefix}"] = json.dumps(mp)
                result[f"{prefix}_midpoint"] = float(mp.get("mid", 0))

                # /last-trade-price
                try:
                    req = urllib.request.Request(
                        f"{CLOB}/last-trade-price?token_id={token_id}",
                        headers={"User-Agent": "curl/7.0"}
                    )
                    ltp = json.loads(urllib.request.urlopen(req, timeout=3).read())
                    result[f"raw_last_trade_payload_{prefix}"] = json.dumps(ltp)
                    result[f"{prefix}_last_trade_price"] = float(ltp.get("price", 0))
                    result[f"{prefix}_last_trade_side"] = ltp.get("side")
                except Exception as e:
                    logger.warning("Failed to fetch %s last trade price: %s", prefix.upper(), e)

            except Exception as e:
                logger.warning("Failed to fetch %s CLOB book: %s", prefix.upper(), e)
                continue

        # Compute spreads
        for prefix in ("yes", "no"):
            bb = result.get(f"{prefix}_best_bid")
            ba = result.get(f"{prefix}_best_ask")
            mp = result.get(f"{prefix}_midpoint")
            if bb is not None and ba is not None and mp is not None and mp > 0:
                result[f"{prefix}_spread"] = ba - bb
                result[f"{prefix}_spread_pct"] = (ba - bb) / mp * 100

        self._last_clob_data = result
        self._last_clob_ts = time.time()
        self._write_clob_snapshot(result)

    def _write_clob_snapshot(self, data):
        """Write a CLOB orderbook snapshot to the clob_orderbook_snapshots table."""
        try:
            for token_id, prefix, outcome_name in [
                (self.yes_token_id, "yes", "YES"),
                (self.no_token_id, "no", "NO"),
            ]:
                ts_iso = datetime.fromtimestamp(
                    data.get("clob_timestamp_ms", 0) / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ") if data.get("clob_timestamp_ms") else None

                with TRADER_DB.get_connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO clob_orderbook_snapshots (
                            market_slug, token_id, outcome_name,
                            midpoint, best_bid, best_ask, spread, spread_pct,
                            bid_depth_top_1, ask_depth_top_1,
                            bid_depth_top_3, ask_depth_top_3,
                            total_bid_depth, total_ask_depth,
                            last_trade_price, last_trade_side,
                            clob_timestamp_ms, received_at_ms,
                            raw_book_payload, raw_midpoint_payload, raw_last_trade_payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.active_slug,
                            token_id,
                            outcome_name,
                            data.get(f"{prefix}_midpoint"),
                            data.get(f"{prefix}_best_bid"),
                            data.get(f"{prefix}_best_ask"),
                            data.get(f"{prefix}_spread"),
                            data.get(f"{prefix}_spread_pct"),
                            data.get(f"{prefix}_bid_depth_top_1"),
                            data.get(f"{prefix}_ask_depth_top_1"),
                            data.get(f"{prefix}_bid_depth_top_3"),
                            data.get(f"{prefix}_ask_depth_top_3"),
                            data.get(f"{prefix}_total_bid_depth"),
                            data.get(f"{prefix}_total_ask_depth"),
                            data.get(f"{prefix}_last_trade_price"),
                            data.get(f"{prefix}_last_trade_side"),
                            data.get("clob_timestamp_ms"),
                            data.get("received_at_ms"),
                            data.get(f"raw_book_payload_{prefix}"),
                            data.get(f"raw_midpoint_payload_{prefix}"),
                            data.get(f"raw_last_trade_payload_{prefix}"),
                        ),
                    )
                    conn.commit()
        except Exception as e:
            logger.error("Failed to write CLOB snapshot: %s", e)

    def _record_data_snapshot(self, event_type, trade_id=None):
        """Write a trade_data_snapshots record at signal/entry/exit/settlement.

        Captures Binance BTC, Chainlink BTC, and full CLOB state.
        """
        try:
            now_ms = int(time.time() * 1000)
            clob = self._last_clob_data

            # Chainlink price at this moment
            cl_tick = get_latest_chainlink_price(TRADER_DB)
            chainlink_price = cl_tick["value_normalized"] if cl_tick else None
            chainlink_ts_ms = cl_tick["source_timestamp_ms"] if cl_tick else None

            with self.lock:
                btc_price = self.btc_price
                btc_o = self.btc_open
                market_open_ts = self.market_open_ts

            btc_change = (btc_price - btc_o) if (btc_price and btc_o) else None
            elapsed = (now_ms / 1000.0 - market_open_ts) if market_open_ts else None
            seconds_to_close = max(0, 300 - elapsed) if elapsed else None

            raw_context = {
                "entries_this_market": self.entries_this_market,
                "entry_yes_prices": list(self.entry_yes_prices),
                "entry_prices": list(self.entry_prices),
                "entry_sizes": list(self.entry_sizes),
                "first_btc_change": self.first_btc_change,
                "chainlink_entry_btc": self._chainlink_entry_btc,
            }

            with TRADER_DB.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO trade_data_snapshots (
                        trade_id, event_type, market_slug, event_timestamp_ms,
                        binance_btc_price, binance_event_timestamp_ms,
                        chainlink_btc_price, chainlink_timestamp_ms,
                        yes_midpoint, yes_best_bid, yes_best_ask,
                        yes_spread, yes_depth_top_3,
                        no_midpoint, no_best_bid, no_best_ask,
                        no_spread, no_depth_top_3,
                        last_trade_price_yes, last_trade_price_no,
                        seconds_to_close, raw_context_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        event_type,
                        self.active_slug,
                        now_ms,
                        btc_price,
                        None,
                        chainlink_price,
                        chainlink_ts_ms,
                        clob.get("yes_midpoint"),
                        clob.get("yes_best_bid"),
                        clob.get("yes_best_ask"),
                        clob.get("yes_spread"),
                        clob.get("yes_bid_depth_top_3"),
                        clob.get("no_midpoint"),
                        clob.get("no_best_bid"),
                        clob.get("no_best_ask"),
                        clob.get("no_spread"),
                        clob.get("no_bid_depth_top_3"),
                        clob.get("yes_last_trade_price"),
                        clob.get("no_last_trade_price"),
                        seconds_to_close,
                        json.dumps(raw_context),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.error("Failed to record data snapshot %s: %s", event_type, e)

    # ── BTC Price (Binance Primary) ──
    def _poll_btc_price(self):
        """Fetch BTC price from Binance REST API.
        
        Binance is the most liquid exchange with the freshest price data.
        Using the SAME source for open AND current ensures Δ is accurate.
        """
        try:
            req = urllib.request.Request(BINANCE_REST, headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            price = float(data.get("price", 0))
            if 10000 < price < 1000000:  # sanity check
                now = time.time()
                with self.lock:
                    self.btc_price = price
                    self.last_btc_time = now
                self._record_btc_tick(price, now)
                return True
        except Exception as e:
            logger.warning("Failed to poll BTC price: %s", e)
        return False

    # ── BTC Tick DB Recording ──
    _last_tick_db = 0.0

    def _record_btc_tick(self, price, timestamp):
        """Write a BTC price tick to the binance_btc_ticks table.
        
        Deduplicates: only writes if price changed or 30s elapsed.
        """
        try:
            now = timestamp
            if abs(now - self._last_tick_db) < 3.0:
                return  # at most once per 3s (2s poll + buffer)
            self._last_tick_db = now
            ts_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with TRADER_DB.get_connection() as conn:
                conn.execute(
                    "INSERT INTO binance_btc_ticks (price, bid, ask, timestamp) VALUES (?, ?, ?, ?)",
                    (price, None, None, ts_iso),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to record BTC tick: %s", e)

    # ── Position tracking for scaling-in ──
    def _should_add_position(self, yes_mid, elapsed, btc_change):
        """Check if we should add another entry.

        Uses direction-specific limits:
        - BUY NO: MAX_ENTRIES_BUY_NO, BTC momentum must fade
        - BUY YES: MAX_ENTRIES_BUY_YES, BTC downward momentum must fade

        BUY NO conviction is NOT about YES price going lower.
        It's about BTC momentum FADING while YES stays suppressed."""
        if not self.entry_yes_prices:
            return True  # No entries yet — always allow first

        last_yes_price = self.entry_yes_prices[-1]
        direction = "BUY_NO" if last_yes_price <= YES_LOW_THRESHOLD else "BUY_YES"
        max_entries = MAX_ENTRIES_BUY_NO if direction == "BUY_NO" else MAX_ENTRIES_BUY_YES

        if self.entries_this_market >= max_entries:
            return False

        # Minimum time between entries (absolute timestamp, not elapsed)
        if time.time() < self.last_entry_time + SIGNAL_PERSIST_SEC:
            return False

        if direction == "BUY_NO":
            # ── BUY NO conviction: BTC momentum must be fading ──
            # BTC continued上涨 = market is correct = NO conviction is WRONG
            if self.first_btc_change is not None:
                btc_still_building = btc_change > 0 and btc_change >= self.first_btc_change * 0.8
                if btc_still_building:
                    # BTC still grinding higher — don't add to NO, market is winning
                    return False

            # YES must still be suppressed (within 0.03 of last entry)
            if yes_mid > last_yes_price + 0.03:
                return False

            return True

        else:
            # ── BUY YES conviction: BTC momentum fading while YES still high ──
            if self.first_btc_change is not None:
                btc_still_falling = btc_change < 0 and btc_change <= self.first_btc_change * 0.8
                if btc_still_falling:
                    return False

            if yes_mid < last_yes_price - 0.03:
                return False

            return True

    # ── Stop-loss / Take-profit ──
    SETTLEMENT_PROTECT_SEC = 15  # Don't exit in last 15s (fake price spikes on thin book)

    def _check_stop_loss(self):
        """Check if any open positions need to be closed (adverse price move)."""
        if not self.entries_this_market or not self.entry_yes_prices:
            return False
        
        # Skip exit checks near settlement — last-second price spikes are fake
        if self.market_open_ts and time.time() > self.market_open_ts + (300 - SETTLEMENT_PROTECT_SEC):
            return False
        
        with self.lock:
            if not self.yes_bid:
                return False
            current_yes = (self.yes_bid + self.yes_ask) / 2.0 if self.yes_ask else self.yes_bid
            btc = self.btc_price
            btc_o = self.btc_open

        btc_change = (btc - btc_o) if (btc and btc_o) else 0.0

        exited = False
        # Check from newest to oldest (pop in reverse)
        for i in range(len(self.entry_yes_prices) - 1, -1, -1):
            entry_yes = self.entry_yes_prices[i]
            entry_size = self.entry_sizes[i] if i < len(self.entry_sizes) else SCALE_SIZE
            direction = "BUY_YES" if entry_yes >= YES_HIGH_THRESHOLD else "BUY_NO"
            if i < len(self.entry_prices) and self.entry_prices[i]:
                entry_price = self.entry_prices[i]
            else:
                entry_price = entry_yes if direction == "BUY_YES" else 1.0 - entry_yes

            # ── TAKE PROFIT: lock in gains before settlement ──
            if direction == "BUY_YES":
                if current_yes >= TAKE_PROFIT_YES:
                    print(f"\n  🟢 TAKE_PROFIT: YES at {current_yes:.3f} >= {TAKE_PROFIT_YES}, closing #{i}")
                    pnl = (current_yes - entry_price) * entry_size
                    self._close_position(i, current_yes, pnl, "TAKE_PROFIT")
                    exited = True
                    continue
            else:  # BUY_NO
                # NO price = 1 - YES; NO ≥ TAKE_PROFIT_NO means YES ≤ 1 - TAKE_PROFIT_NO
                if current_yes <= 1.0 - TAKE_PROFIT_NO:
                    print(f"\n  🟢 TAKE_PROFIT: NO at {(1.0-current_yes):.3f} >= {TAKE_PROFIT_NO}, closing #{i}")
                    pnl = ((1.0 - current_yes) - entry_price) * entry_size
                    self._close_position(i, current_yes, pnl, "TAKE_PROFIT")
                    exited = True
                    continue

            # ── STOP LOSS ──
            if direction == "BUY_YES":
                # PRIMARY: BTC-based stop (survives manipulator shakeouts)
                # BTC is objective — if it keeps falling, strategy thesis is failing
                if self.first_btc_change is not None and self.first_btc_change < 0:
                    btc_threshold = abs(self.first_btc_change) * STOP_LOSS_BTC_MULTIPLIER_YES
                    btc_threshold = max(btc_threshold, MIN_BTC_MOVE_YES * STOP_LOSS_BTC_MULTIPLIER_YES)
                    btc_threshold = max(btc_threshold, STOP_LOSS_BTC_MIN)
                    if btc_change < -btc_threshold:
                        pnl = (current_yes - entry_price) * entry_size
                        self._close_position(i, current_yes, pnl, "STOP_LOSS_BTC")
                        exited = True
                # FALLBACK: YES-based (only if first_btc_change isn't recorded yet)
                elif current_yes < entry_yes - STOP_LOSS_YES_MOVE:
                    pnl = (current_yes - entry_price) * entry_size
                    self._close_position(i, current_yes, pnl, "STOP_LOSS")
                    exited = True
            else:  # BUY_NO
                # PRIMARY: BTC-based stop (survives manipulator shakeouts)  
                if self.first_btc_change is not None and self.first_btc_change > 0:
                    btc_threshold = abs(self.first_btc_change) * STOP_LOSS_BTC_MULTIPLIER_NO
                    btc_threshold = max(btc_threshold, MIN_BTC_MOVE * STOP_LOSS_BTC_MULTIPLIER_NO)
                    btc_threshold = max(btc_threshold, STOP_LOSS_BTC_MIN)
                    if btc_change > btc_threshold:
                        pnl = ((1.0 - current_yes) - entry_price) * entry_size
                        self._close_position(i, current_yes, pnl, "STOP_LOSS_BTC")
                        exited = True
                # FALLBACK: YES-based
                elif current_yes > entry_yes + STOP_LOSS_NO_MOVE:
                    # NO price = 1 - YES; pnl = (1-current) - (1-entry) = entry - current
                    pnl = ((1.0 - current_yes) - entry_price) * entry_size
                    self._close_position(i, current_yes, pnl, "STOP_LOSS")
                    exited = True

        return exited

    def _close_position(self, entry_idx, current_yes, pnl, reason):
        """Close a position and log the exit."""
        with self.lock:  # protect list + pnl updates from race conditions
            entry_price = self.entry_prices[entry_idx]
            entry_yes = self.entry_yes_prices[entry_idx]
            entry_size = self.entry_sizes[entry_idx] if entry_idx < len(self.entry_sizes) else SCALE_SIZE
            side = "YES" if entry_yes >= YES_HIGH_THRESHOLD else "NO"
            token_id = self.yes_token_id if side == "YES" else self.no_token_id

        marker = "🔴STOP" if pnl < 0 else "🟢EXIT"
        print(f"\n  {marker} {reason}: {side} @ ${entry_price:.3f} → YES☆{current_yes:.3f}")
        print(f"         PnL: ${pnl:.2f} | Size: ${entry_size:.1f}")

        # Log to paper_trades.jsonl as exit
        try:
            log_path = SRC.parent / "logs" / "paper_trades.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # Chainlink exit price
            cl_tick = get_latest_chainlink_price(TRADER_DB)
            chainlink_exit_btc = cl_tick["value_normalized"] if cl_tick else None

            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "slug": self.active_slug,
                "side": side,
                "action": "SELL",
                "entry_price": entry_price,
                "exit_reason": reason,
                "exit_yes_price": round(current_yes, 3),
                "pnl": round(pnl, 2),
                "size": entry_size,
                # Chainlink settlement fields
                "settlement_source": "chainlink",
                "chainlink_exit_btc": chainlink_exit_btc,
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error("Failed to log exit: %s", e)
            print(f"  WARNING: failed to log exit: {e}")

        if not self.dry_run:
            try:
                sell_price = current_yes if side == "YES" else 1.0 - current_yes
                order_args = OrderArgs(
                    token_id=token_id,
                    price=round(sell_price, 3),
                    size=entry_size,
                    side="SELL",
                    expiration="0",
                )
                result = self.clob.create_and_post_order(order_args, order_type=OrderType.GTC)
                if not result.get("success"):
                    print(f"\n  ❌ Exit order failed: {result}")
                    self._record_order_failure()
                    return False

                oid = result.get("orderID", "")
                print(f"         LIVE SELL submitted: {oid[:16]}...")

                # Fill verification — poll 5s for MATCHED, cancel if not filled
                matched = False
                for _ in range(5):
                    order = self.clob.get_order(oid)
                    status = order.get("status") if isinstance(order, dict) else None
                    if status == "MATCHED":
                        matched = True
                        break
                    time.sleep(1)

                if not matched:
                    print(f"         Sell not filled after 5s, canceling: {oid[:16]}...")
                    self.clob.cancel_order(oid)
                    self._record_order_failure()
                    return False

                print(f"         ✅ LIVE SELL filled (MATCHED)")
                self._consecutive_failures = 0
            except Exception as e:
                print(f"\n  ❌ Exit order error: {e}")
                self._record_order_failure()
                return False

        # Write exit alert for WhatsApp
        self._write_trade_alert(entry, "EXIT")

        # Write data snapshot for exit event
        self._record_data_snapshot("EXIT")

        with self.lock:
            self.daily_pnl += pnl
            self.entry_prices.pop(entry_idx)
            self.entry_yes_prices.pop(entry_idx)
            if entry_idx < len(self.entry_sizes):
                self.entry_sizes.pop(entry_idx)
            self.entries_this_market -= 1

        return True

    # ── Signal Detection (FOLLOW strategy) ──
    def _detect_signal(self):
        """Check for FOLLOW manipulation signal.
        
        Uses REAL CTF prices from CLOB V2 /midpoint endpoint.
        yes_bid = yes_ask = midpoint price (the best available).
        No Gamma→UI conversion needed — midpoint IS the real UI price.
        """
        with self.lock:
            if not self.btc_open or not self.btc_price:
                return None
            if not self.active_slug:
                return None
            if not self.yes_bid or not self.yes_ask:
                return None

            # Check YES price freshness — use last known price up to 30s old
            # (CLOB midpoint occasionally fails, but last known price is still valid)
            if time.time() - self._last_yes_update > 30.0:
                return None

            btc_change = self.btc_price - self.btc_open
            elapsed = time.time() - self.market_open_ts
            yes_price = (self.yes_bid + self.yes_ask) / 2.0

        # Wait for manipulation to develop
        if elapsed < ENTRY_DELAY:
            return None

        # FOLLOW strategy signals:
        # Condition A: BTC UP + YES LOW → manipulator buying NO → BUY NO
        #   NO side uses the larger BTC move threshold and tighter entry window.
        if btc_change > MIN_BTC_MOVE and elapsed <= ENTRY_WINDOW_END_NO \
           and yes_price <= YES_LOW_THRESHOLD:
            # Buy NO token: pay NO ask = 1.0 - YES_bid (since YES and NO prices sum to ~1.0)
            buy_price = 1.0 - yes_price  # NO price
            token_id = self.no_token_id
            side = "NO"
            reason = f"FOLLOW BUY NO: BTC+${abs(btc_change):.0f} but YES={yes_price:.3f} (manip buying NO)"

        # Condition B: BTC DOWN + YES HIGH → manipulator buying YES → BUY YES
        #   YES side uses the smaller BTC move threshold and full entry window.
        elif btc_change < -MIN_BTC_MOVE_YES and elapsed <= ENTRY_WINDOW_END \
             and yes_price >= YES_HIGH_THRESHOLD:
            buy_price = yes_price  # YES ask price
            token_id = self.yes_token_id
            side = "YES"
            reason = f"FOLLOW BUY YES: BTC${abs(btc_change):.0f} but YES={yes_price:.3f} (manip buying YES)"

        else:
            return None  # No signal

        # Sanity: price must be reasonable
        if buy_price <= 0.01 or buy_price >= 0.99:
            return None

        # Check scaling-in limits
        if self.entries_this_market > 0:
            if not self._should_add_position(yes_price, elapsed, btc_change):
                return None
        else:
            # First entry: capture BTC baseline for conviction tracking
            self.first_btc_change = btc_change

        # Capture fill price from CLOB book — use best_ask for BUY, best_bid for SELL
        clob = self._last_clob_data
        yes_best_bid = clob.get("yes_best_bid")
        yes_best_ask = clob.get("yes_best_ask")
        no_best_bid = clob.get("no_best_bid")
        no_best_ask = clob.get("no_best_ask")
        yes_mid = clob.get("yes_midpoint")
        no_mid = clob.get("no_midpoint")

        # BUY YES: use midpoint price (fairer than best_ask); BUY NO: use midpoint
        if side == "YES":
            execution_side = "BUY"
            fill_price = yes_mid if yes_mid else (yes_best_ask if yes_best_ask else buy_price)
            fill_method = "midpoint" if yes_mid else ("best_ask" if yes_best_ask else "midpoint")
            midpoint_price = yes_mid
        else:  # NO
            execution_side = "BUY"
            # NO ask = 1 - YES bid
            fill_price = no_mid if no_mid else (no_best_ask if no_best_ask else buy_price)
            fill_method = "midpoint" if no_mid else ("best_ask" if no_best_ask else "midpoint")
            midpoint_price = no_mid

        # Fallback to midpoint-derived price if book data unavailable
        if not fill_price or fill_price <= 0:
            fill_price = buy_price
            fill_method = "midpoint"

        # ── Entry gating: data quality checks before paper entry ──
        clob_ts = clob.get("clob_timestamp_ms")
        now_ms = int(time.time() * 1000)
        book_age_ms = (now_ms - int(clob_ts)) / 1000.0 if clob_ts else None

        # Spread check (use appropriate side's spread)
        if side == "YES" and yes_best_bid and yes_best_ask:
            spread_pct_mid = (yes_best_ask - yes_best_bid) / ((yes_best_bid + yes_best_ask) / 2)
            if spread_pct_mid > 0.05:
                self._record_skipped_signal({
                    "reason": reason,
                    "elapsed": elapsed,
                    "btc_change": btc_change,
                    "yes_price": yes_price,
                    "spread_pct_mid": round(spread_pct_mid, 4),
                    "best_bid": yes_best_bid,
                    "best_ask": yes_best_ask,
                    "book_age_ms": book_age_ms,
                    "seconds_to_close": max(0, 300 - elapsed),
                }, skip_reason="wide_spread")
                return None
        elif side == "NO" and no_best_bid and no_best_ask:
            spread_pct_mid = (no_best_ask - no_best_bid) / ((no_best_bid + no_best_ask) / 2)
            if spread_pct_mid > 0.05:
                self._record_skipped_signal({
                    "reason": reason,
                    "elapsed": elapsed,
                    "btc_change": btc_change,
                    "yes_price": yes_price,
                    "spread_pct_mid": round(spread_pct_mid, 4),
                    "best_bid": no_best_bid,
                    "best_ask": no_best_ask,
                    "book_age_ms": book_age_ms,
                    "seconds_to_close": max(0, 300 - elapsed),
                }, skip_reason="wide_spread")
                return None

        # Book freshness check
        if book_age_ms is not None and book_age_ms > 5.0:
            self._record_skipped_signal({
                "reason": reason,
                "elapsed": elapsed,
                "btc_change": btc_change,
                "yes_price": yes_price,
                "book_age_ms": book_age_ms,
                "seconds_to_close": max(0, 300 - elapsed),
            }, skip_reason="stale_book")
            return None

        # Near-settlement check
        seconds_to_end = max(0, 300 - elapsed)
        if seconds_to_end < 30:
            self._record_skipped_signal({
                "reason": reason,
                "elapsed": elapsed,
                "btc_change": btc_change,
                "yes_price": yes_price,
                "seconds_to_close": seconds_to_end,
            }, skip_reason="near_settlement")
            return None

        return {
            "side": side,
            "token_id": token_id,
            "limit_price": round(buy_price, 3),
            "size": round(SCALE_SIZE, 1),
            "cost": round(buy_price * SCALE_SIZE, 2),
            "max_cost": round(SCALE_SIZE * MAX_ENTRIES_PER_MARKET, 2),
            "max_loss": round(SCALE_SIZE * MAX_ENTRIES_PER_MARKET, 2),
            "spread_pct": 0.0,
            "btc_change": round(btc_change, 1),
            "elapsed": round(elapsed, 0),
            "btc_up": btc_change > 0,
            "market_slug": self.active_slug,
            "reason": reason,
            "signal_type": "follow",
            "entry_price_yes": round(yes_price, 3),
            "entry_number": self.entries_this_market + 1,
            "is_scaling": self.entries_this_market > 0,
            "yes_mid": round(yes_price, 3),
            "yes_bid": round(yes_price, 3),
            "yes_ask": round(yes_price, 3),
            "action": "BUY",
            # CLOB fill price fields
            "execution_side": execution_side,
            "fill_price": round(fill_price, 4),
            "fill_method": fill_method,
            "midpoint_price": round(midpoint_price, 4) if midpoint_price else None,
            "yes_best_bid": round(yes_best_bid, 4) if yes_best_bid else None,
            "yes_best_ask": round(yes_best_ask, 4) if yes_best_ask else None,
            "no_best_bid": round(no_best_bid, 4) if no_best_bid else None,
            "no_best_ask": round(no_best_ask, 4) if no_best_ask else None,
        }

    # ── Risk Validation ──
    def _validate_risk(self, signal):
        """Run risk engine checks. Returns (can_proceed, violations)."""
        # Build open orders list from ALL prior entries
        open_orders = []
        for i in range(len(self.entry_prices)):
            open_orders.append({
                "market_slug": self.active_slug,
                "side": signal["side"],
                "action": signal["action"],
                "limit_price": self.entry_prices[i],
                "size": SCALE_SIZE,
                "cost": self.entry_prices[i] * SCALE_SIZE,
                "max_loss": self.entry_prices[i] * SCALE_SIZE,
            })

        result = self.risk_engine.validate_order(
            draft=signal,
            open_orders=open_orders,
            daily_trades=self.trades_today,
            daily_loss=max(0, -self.daily_pnl),
            balance=self.config.balance,
        )

        if not result["can_proceed"]:
            # Rate-limit repetitive R3 spam: only print once per market
            slug = self.active_slug or ""
            for v in result["violations"]:
                if "R3" in v and slug == self._last_r3_slug:
                    continue  # skip repeat R3 for same market
                print(f"  RISK BLOCKED: {v}")
            if any("R3" in v for v in result["violations"]):
                self._last_r3_slug = slug

        return result["can_proceed"]

    # ── Order Execution ──
    def _record_order_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            print("EMERGENCY: 3 consecutive failures, killing trader")
            self.stop()

    def _execute_trade(self, signal):
        """Place order or log in dry-run mode."""
        # Risk validation
        if not self._validate_risk(signal):
            return False

        if self.dry_run:
            entry_no = signal["entry_number"]
            marker = "🟢" if entry_no == 1 else "🔵ADD"
            print(f"\n  {marker} DRY: {signal['reason']}")
            print(f"         Entry #{entry_no} | {signal['side']} @ {signal['limit_price']} x {signal['size']} "
                  f"= ${signal['cost']:.2f} | YES mid={signal['yes_mid']}")
            self._record_entry(signal)
            return True

        # LIVE order
        try:
            order_args = OrderArgs(
                token_id=signal["token_id"],
                price=signal["limit_price"],
                size=signal["size"],
                side="BUY",
                expiration="0",
            )
            result = self.clob.create_and_post_order(order_args, order_type=OrderType.GTC)

            if result.get("success"):
                oid = result.get("orderID")
                if not oid:
                    print(f"\n  ❌ Order missing ID: {result}")
                    self._record_order_failure()
                    return False

                entry_no = signal["entry_number"]
                print(f"\n  ✅ LIVE #{entry_no}: {signal['reason']}")
                print(f"         {signal['side']} @ {signal['limit_price']} x {signal['size']} "
                      f"= ${signal['cost']:.2f} | ID: {oid[:16]}...")

                matched = False
                for _ in range(5):
                    order = self.clob.get_order(oid)
                    status = order.get("status") if isinstance(order, dict) else None
                    if status == "MATCHED":
                        matched = True
                        break
                    time.sleep(1)

                if not matched:
                    print(f"         Order not filled after 5s, canceling: {oid[:16]}...")
                    self.clob.cancel_order(oid)
                    self._record_order_failure()
                    return False

                self._record_entry(signal)
                self._consecutive_failures = 0
                return True
            else:
                print(f"\n  ❌ Order failed: {result}")
                self._record_order_failure()
                return False
        except Exception as e:
            print(f"\n  ❌ Order error: {e}")
            self._record_order_failure()
            return False

    def _record_entry(self, signal):
        """Record an entry for scaling-in tracking and persistent logging."""
        self.entries_this_market += 1
        self.last_entry_time = time.time()  # absolute timestamp, not elapsed
        self.entry_yes_prices.append(signal["yes_mid"])
        entry_price = signal.get("fill_price") or signal.get("limit_price")
        self.entry_prices.append(entry_price)
        self.entry_sizes.append(signal.get("size", SCALE_SIZE))
        self.trades_today += 1
        self.last_trade_time = time.time()

        # Capture Chainlink BTC price at entry (settlement truth)
        cl_tick = get_latest_chainlink_price(TRADER_DB)
        self._chainlink_entry_btc = cl_tick["value_normalized"] if cl_tick else None

        # Log to paper_trades.jsonl for persistent tracking
        try:
            log_path = SRC.parent / "logs" / "paper_trades.jsonl"
            log_path.parent.mkdir(exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "slug": self.active_slug,
                "side": signal.get("side"),
                "action": signal.get("action"),
                "entry_price": entry_price,
                "fill_price": signal.get("fill_price"),
                "fill_method": signal.get("fill_method"),
                "size": signal.get("size"),
                "cost": signal.get("cost"),
                "btc_change": signal.get("btc_change"),
                "yes_price": signal.get("yes_mid"),
                "reason": signal.get("reason"),
                "entry_no": signal.get("entry_number"),
                "is_scaling": signal.get("is_scaling", False),
                # Chainlink settlement fields
                "settlement_source": "chainlink",
                "chainlink_entry_btc": self._chainlink_entry_btc,
                "midpoint_price": signal.get("midpoint_price"),
                "yes_best_bid": signal.get("yes_best_bid"),
                "yes_best_ask": signal.get("yes_best_ask"),
                "no_best_bid": signal.get("no_best_bid"),
                "no_best_ask": signal.get("no_best_ask"),
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error("Failed to log paper trade entry: %s", e)

        # Write data snapshot for entry event
        self._record_data_snapshot("ENTRY")

        # Write checkpoint for crash recovery
        self._write_checkpoint()

        # Write trade alert for WhatsApp notification
        self._write_trade_alert(signal, "ENTRY")

    def _record_skipped_signal(self, ctx, skip_reason):
        """Log a skipped signal (entry gating blocked) to skipped_signals.jsonl."""
        try:
            log_path = SRC.parent / "logs" / "skipped_signals.jsonl"
            log_path.parent.mkdir(exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_slug": self.active_slug,
                "condition_id": self.active_condition_id or "",
                "skip_reason": skip_reason,
                "reason": ctx.get("reason", ""),
                "btc_change": ctx.get("btc_change"),
                "yes_price": ctx.get("yes_price"),
                "spread_pct_mid": ctx.get("spread_pct_mid"),
                "best_bid": ctx.get("best_bid"),
                "best_ask": ctx.get("best_ask"),
                "book_age_ms": ctx.get("book_age_ms"),
                "seconds_to_close": ctx.get("seconds_to_close"),
                "elapsed": ctx.get("elapsed"),
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning("Failed to record skipped signal: %s", e)

    def _write_trade_alert(self, signal, alert_type):
        """Write a trade alert file for the WhatsApp watcher cron to pick up."""
        try:
            alert_path = SRC.parent / "logs" / "trade_alert.json"
            alert = {
                "ts": time.time(),
                "type": alert_type,
                "slug": self.active_slug,
                "side": signal.get("side"),
                "action": signal.get("action"),
                "entry_price": signal.get("fill_price") or signal.get("entry_price") or signal.get("limit_price"),
                "size": signal.get("size"),
                "cost": signal.get("cost"),
                "btc_change": signal.get("btc_change"),
                "yes_price": signal.get("yes_mid") or signal.get("exit_yes_price") or signal.get("yes_price"),
                "reason": signal.get("reason"),
                "entry_no": signal.get("entry_number"),
                "is_scaling": signal.get("is_scaling", False),
                "pnl": signal.get("pnl"),
                "exit_reason": signal.get("exit_reason"),
                "market_slug": self.active_slug,
                "fill_price": signal.get("fill_price") or signal.get("entry_price"),
                "fill_method": signal.get("fill_method"),
            }
            with open(alert_path, "w") as f:
                json.dump(alert, f)
        except Exception as e:
            logger.error("Failed to write trade alert: %s", e)

    # ── State snapshot for dashboard (written every poll cycle) ──
    def _write_state_snapshot(self):
        """Write current state to a JSON file for the dashboard to consume."""
        with self.lock:
            btc = self.btc_price
            btc_o = self.btc_open
            bid = self.yes_bid
            ask = self.yes_ask
            slug = self.active_slug
            entries = self.entries_this_market
            yes_prices = list(self.entry_yes_prices)

        elapsed = time.time() - self.market_open_ts if self.market_open_ts else 0
        remaining = max(0, 300 - elapsed)
        btc_change = (btc - btc_o) if (btc and btc_o) else 0

        state = {
            "ts": time.time(),
            "slug": slug or "",
            "btc": round(btc, 2) if btc else None,
            "btc_open": round(btc_o, 2) if btc_o else None,
            "btc_change": round(btc_change, 1),
            "remaining": round(remaining, 0),
            "yes_bid": round(bid, 3) if bid else None,
            "yes_ask": round(ask, 3) if ask else None,
            "entries": entries,
            "entry_yes_prices": [round(p, 3) for p in yes_prices],
            "trades_today": self.trades_today,
            "daily_pnl": round(self.daily_pnl, 2),
        }

        try:
            state_path = SRC.parent / "logs" / "trader_state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(state_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning("Failed to write trader state snapshot: %s", e)

    # ── Checkpoint for crash recovery ──
    CHECKPOINT_FILE = SRC.parent / "logs" / "trader_checkpoint.json"

    def _write_checkpoint(self):
        """Save current trading state to disk for crash recovery."""
        try:
            with self.lock:
                state = {
                    "btc_open": self.btc_open,
                    "btc_price": self.btc_price,
                    "entries_this_market": self.entries_this_market,
                    "entry_yes_prices": list(self.entry_yes_prices),
                    "entry_prices": list(self.entry_prices),
                    "first_btc_change": self.first_btc_change,
                    "market_open_ts": self.market_open_ts,
                    "active_slug": self.active_slug,
                    "active_condition_id": self.active_condition_id,
                    "yes_token_id": self.yes_token_id,
                    "no_token_id": self.no_token_id,
                    "trades_today": self.trades_today,
                    "daily_pnl": self.daily_pnl,
                    "ts": time.time(),
                }
            self.CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.CHECKPOINT_FILE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.error("Failed to write checkpoint: %s", e)

    def _load_checkpoint(self):
        """Restore state from checkpoint. Returns True if valid state restored."""
        try:
            if not self.CHECKPOINT_FILE.exists():
                return False
            with open(self.CHECKPOINT_FILE) as f:
                state = json.load(f)
            if time.time() - state.get("ts", 0) > 600:
                return False  # expired after 10min
            with self.lock:
                self.btc_open = state.get("btc_open")
                self.btc_price = state.get("btc_price")
                self.entries_this_market = state.get("entries_this_market", 0)
                self.entry_yes_prices = state.get("entry_yes_prices", [])
                self.entry_prices = state.get("entry_prices", [])
                self.first_btc_change = state.get("first_btc_change")
                self.market_open_ts = state.get("market_open_ts")
                self.active_slug = state.get("active_slug")
                self.active_condition_id = state.get("active_condition_id")
                self.yes_token_id = state.get("yes_token_id")
                self.no_token_id = state.get("no_token_id")
                self.trades_today = state.get("trades_today", 0)
                self.daily_pnl = state.get("daily_pnl", 0.0)
            if self.entries_this_market > 0:
                print(f"  Restored {self.entries_this_market} open position(s) from checkpoint")
            return True
        except Exception as e:
            logger.warning("Failed to load checkpoint: %s", e)
            return False

    # ── Main Loop ──
    def run(self):
        print("=" * 55)
        print("REALTIME BTC 5-MIN TRADER — FOLLOW STRATEGY")
        print("=" * 55)

        # Try restoring from checkpoint
        if self._load_checkpoint():
            print("  Checkpoint restored (crash recovery)")
        else:
            print(f"  Fresh start — {self.trades_today} trades tracked today")

        print("  Fetching BTC price...")
        # Start Binance WebSocket for real-time price updates
        self._start_binance()
        for _ in range(10):
            if self.btc_price:
                break
            self._poll_btc_price()
            time.sleep(1)

        if not self.btc_price:
            print("  Failed to get BTC price")
            return

        print(f"  BTC: ${self.btc_price:.1f}")
        print(f"  Thresholds: BTC YES≥${MIN_BTC_MOVE_YES}/NO≥${MIN_BTC_MOVE}, YES≤{YES_LOW_THRESHOLD}/≥{YES_HIGH_THRESHOLD}, "
              f"enter>{ENTRY_DELAY:.0f}s, max {MAX_ENTRIES_PER_MARKET}x/market")
        if self.dry_run:
            print("  ⚠ DRY RUN — limit orders may not fill in real trading")
        print("  Press Ctrl+C to stop\n")

        last_discover = 0
        last_poll = 0
        last_btc_poll = 0
        last_clob_book_poll = 0
        stop_out = False

        while self.running:
            now = time.time()

            if now - last_discover > 2:
                self._discover_market()
                last_discover = now

            if now - last_poll > POLL_INTERVAL:
                self._poll_orderbook()
                stop_out = self._check_stop_loss()  # stop-loss check after every price update
                last_poll = now

            # CLOB /book full orderbook — every 2s
            if now - last_clob_book_poll > 2.0:
                self._poll_clob_book()
                last_clob_book_poll = now

            # BTC price (every 2s from Binance)
            if now - last_btc_poll > BTC_POLL_INTERVAL:
                self._poll_btc_price()
                last_btc_poll = now

            # Signal detection (not blocked by stop-loss — positions are
            # already removed from tracking, new signals can form)
            signal = self._detect_signal()
            if signal:
                self._execute_trade(signal)

            # Write state snapshot for dashboard (every poll cycle = 0.5s)
            self._write_state_snapshot()

            # Status display
            with self.lock:
                btc = self.btc_price
                btc_o = self.btc_open
                bid = self.yes_bid
                ask = self.yes_ask

            if self.active_slug:
                elapsed = now - self.market_open_ts if self.market_open_ts else 0
                remaining = max(0, 300 - elapsed)
                parts = [f"{remaining:3.0f}s"]
                if btc:
                    parts.append(f"BTC=${btc:.2f}")
                    if btc_o:
                        parts.append(f"Δ${(btc-btc_o):+.0f}")
                    # Show data freshness + WS status
                    btc_age = now - self.last_btc_time
                    ws_status = "⚡" if _ws_fail_count == 0 else "📡"
                    parts.append(ws_status)
                    parts.append("◉" if btc_age < 5 else "○")
                if bid and ask:
                    # REAL CTF price from CLOB V2 midpoint
                    mid = (bid + ask) / 2.0
                    parts.append(f"YES☆{mid:.3f}")
                elif bid or ask:
                    parts.append(f"YES☆{bid:.3f}")
                else:
                    parts.append("NO DATA")
                if self.entries_this_market > 0:
                    parts.append(f"[x{self.entries_this_market}]")
                direction = ""
                if self.entries_this_market > 0 and self.entry_yes_prices:
                    m = self.entry_yes_prices[-1]
                    if m >= YES_HIGH_THRESHOLD:
                        direction = "BUY_YES"
                    elif m <= YES_LOW_THRESHOLD:
                        direction = "BUY_NO"
                if direction:
                    parts.append(f"⬇FOLLOW" if direction == "BUY_NO" else "⬆FOLLOW")
                # Rate-limit status line: only write to log every 2s (balance between freshness and log size)
                if now - self._last_status_ts >= 2 or signal:
                    self._last_status_ts = now
                    print(f"  {' | '.join(parts)}    ")

            time.sleep(0.1)

        print(f"\n\nStopped. Trades today: {self.trades_today}, entries this market: {self.entries_this_market}")

    def stop(self):
        self._write_checkpoint()
        self.running = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-cost", type=float, default=8.0)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", action="store_true", help="Enable live trading (requires risk checks)")
    args = parser.parse_args()

    dry_run = True
    if args.live:
        config = Config()
        missing = []
        if not config.live_trading:
            missing.append("LIVE_TRADING=true")
        if config.dry_run:
            missing.append("DRY_RUN=false")
        if not config.private_key:
            missing.append("POLY_PK")
        if not config.has_credentials():
            missing.append("POLYMARKET_API_KEY/SECRET/PASSPHRASE")

        if missing:
            print("  WARNING: --live requested but live trading is not fully enabled; staying in dry-run.")
            print(f"           Missing or invalid config: {', '.join(missing)}")
            logger.warning("Live trading request blocked; missing config: %s", ", ".join(missing))
        else:
            dry_run = False

    trader = RealtimeTrader(max_cost=args.max_cost, dry_run=dry_run)

    def sig_handler(sig, frame):
        print("\n  Shutting down...")
        trader.stop()

    signal.signal(signal.SIGINT, sig_handler)
    trader.run()


if __name__ == "__main__":
    main()
