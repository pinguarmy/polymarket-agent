# Coding Tasks — Phase-by-Phase Implementation

## How to Use This Document

Each task is a self-contained unit that can be implemented by Codex CLI or manually. Tasks are grouped by phase. Complete all tasks in a phase before moving to the next.

**Prerequisites**: Python 3.10+, `pip install py-clob-client>=0.30.0 python-dotenv requests pandas`

---

## Phase 0: Project Scaffolding

### Task 0.1 — Project Structure

Create the directory structure and empty module files:

```
polymarket-agent/
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── market_data.py
│   ├── paper_trader.py
│   ├── signals.py
│   ├── order_draft.py
│   ├── risk_engine.py
│   ├── logger.py
│   └── tracker.py
├── logs/
│   ├── paper_trades/
│   └── daily_reports/
├── data/
├── scripts/
├── tests/
├── .env.example
├── .gitignore
└── requirements.txt
```

**Files to create:**
- `src/__init__.py` — empty
- `requirements.txt` — py-clob-client>=0.30.0, python-dotenv, requests, pandas, python-dateutil
- `.env.example` — template without real secrets
- `.gitignore` — ignore .env, logs, __pycache__, *.pyc

**Test**: `python -c "import sys; sys.path.insert(0, 'src'); import config; print('ok')"`

### Task 0.2 — Config Loader

`src/config.py` — load settings from `.env` with sensible defaults.

```python
# Interface:
class Config:
    def __init__(self, env_file: str = ".env"):
        ...

    @property
    def rpc_url(self) -> str: ...
    @property
    def private_key(self) -> str: ...       # Returns "" if not set
    @property
    def wallet_address(self) -> str: ...
    @property
    def api_key(self) -> str: ...
    @property
    def api_secret(self) -> str: ...
    @property
    def api_passphrase(self) -> str: ...
    @property
    def max_order_size(self) -> float: ...  # Default: 5.0
    @property
    def max_daily_loss(self) -> float: ...  # Default: 10.0
    @property
    def max_daily_trades(self) -> int: ...  # Default: 20
    @property
    def dry_run(self) -> bool: ...          # Default: True
    @property
    def live_trading(self) -> bool: ...     # Default: False
    @property
    def allowed_markets(self) -> list: ...  # List of slugs
```

**Test**: Create a `.env.test` with known values, load config, verify all properties.

### Task 0.3 — Structured Logger

`src/logger.py` — JSONL logger for structured trade logs.

```python
class TradeLogger:
    def log_paper_trade(self, entry: dict):
        """Append to logs/paper_trades.jsonl"""
    def log_signal(self, entry: dict):
        """Append to logs/signals.jsonl"""
    def log_session(self, summary: dict):
        """Append to logs/paper_sessions.jsonl"""
    def log_daily_report(self, report: dict):
        """Write to logs/daily_reports/YYYY-MM-DD_report.json"""
```

**Test**: Log 3 sample trade entries, verify JSONL file is valid and parsable.

---

## Phase 1: Read-Only Market Data

### Task 1.1 — Market Lookup by Slug

`src/market_data.py` — add function `get_market_by_slug(slug: str) -> dict`

Fetch from Gamma API: `GET https://gamma-api.polymarket.com/markets?slug={slug}`

Return parsed market data including:
- question
- slug
- conditionId
- clobTokenIds (parsed from JSON string)
- outcomePrices (parsed from JSON string)
- volume
- liquidity
- endDate
- active/closed status

**Test**: Look up `will-donald-trump-win-the-2028-presidential-election` and print market info.

### Task 1.2 — Order Book

`src/market_data.py` — add function `get_order_book(token_id: str) -> dict`

Fetch from CLOB API: `GET https://clob.polymarket.com/book?token_id={token_id}`

Return best bid, best ask, full bid/ask arrays, spread, last trade price.

**Test**: Get order book for a known token ID, print best bid/ask.

### Task 1.3 — Price & Midpoint

`src/market_data.py` — add functions:

```python
def get_price(token_id: str, side: str = "buy") -> float
def get_midpoint(token_id: str) -> float
def get_spread(token_id: str) -> float
```

**Test**: Query price, midpoint, spread for a known token, verify 0 < price < 1.

### Task 1.4 — Recent Trades

`src/market_data.py` — add function:

```python
def get_recent_trades(condition_id: str, limit: int = 20) -> list
```

Fetch from Data API: `GET https://data-api.polymarket.com/trades?market={condition_id}&limit={limit}`

**Test**: Get 5 recent trades for a known market, print trade data.

### Task 1.5 — Market Summary

`src/market_data.py` — add function:

```python
def get_market_summary(slug: str) -> dict
```

Combines all above into a single summary:

```python
{
    "market_name": "...",
    "market_slug": "...",
    "condition_id": "...",
    "yes_token_id": "...",
    "no_token_id": "...",
    "best_bid": 0.64,
    "best_ask": 0.66,
    "spread": 0.02,
    "spread_pct": 3.08,
    "liquidity": 50000,
    "volume_24h": 150000,
    "last_trade_price": 0.65,
    "data_timestamp": "2026-04-30T14:30:00Z"
}
```

**Test**: Generate summary for a live market. Print it in a readable format.

### Task 1.6 — CLI Entry Point

Add CLI to `src/market_data.py`:

```bash
# Basic lookup
python src/market_data.py --slug <slug>

# Order book
python src/market_data.py --slug <slug> --orderbook

# Summary (default)
python src/market_data.py --slug <slug> --summary

# Recent trades
python src/market_data.py --slug <slug> --trades

# JSON output for programmatic use
python src/market_data.py --slug <slug> --json
```

**Test**: Run each CLI option, verify output format.

---

## Phase 2: Paper Trading

### Task 2.1 — PaperTrader Class

`src/paper_trader.py` — core class:

```python
class PaperTrader:
    def __init__(self, capital: float, config: Config):
        self.capital = capital
        self.positions: dict[str, PaperPosition] = {}
        self.trade_log: list[PaperTrade] = []

    def buy(self, slug: str, side: str, price: float, size: float,
            reason: str, signal_type: str, confidence: float,
            expected_edge: float, risk_notes: str) -> PaperTrade:
        """Record a simulated buy. Deduct cost from capital."""

    def sell(self, slug: str, side: str, price: float, size: float,
             reason: str) -> PaperTrade:
        """Record a simulated sell. Add proceeds to capital. Calculate PnL."""

    def close_position(self, slug: str, price: float, reason: str) -> PaperTrade:
        """Close an existing open position."""

    def get_summary(self) -> dict:
        """Return current session summary (PnL, positions, trades count)."""

    def get_open_positions(self) -> dict:
        """Return all open positions with unrealized PnL."""

    def save_session(self):
        """Save session summary to logs/paper_sessions.jsonl."""
```

**Test**: Buy 10 YES at 0.65, sell at 0.70. Verify PnL is 0.50. Check capital updated.

### Task 2.2 — Position Tracker

`src/tracker.py`:

```python
class PositionTracker:
    def __init__(self, initial_capital: float): ...

    def open_position(self, trade: PaperTrade): ...
    def close_position(self, trade: PaperTrade): ...
    def get_position(self, slug: str) -> PaperPosition | None: ...
    def get_all_positions(self) -> list[PaperPosition]: ...
    def get_daily_pnl(self) -> float: ...
    def get_total_pnl(self) -> float: ...
    def get_capital(self) -> float: ...
    def get_peak_capital(self) -> float: ...
    def get_max_drawdown(self) -> float: ...
```

**Test**: Open 2 positions, close 1, verify tracker state is correct.

### Task 2.3 — Daily Report Generator

`src/tracker.py` — add report generation:

```python
def generate_daily_report(trades: list[PaperTrade],
                          positions: dict,
                          capital: float) -> dict:
    """
    Generate daily report matching format in PAPER_TRADING_PLAN.md.

    Returns:
        dict with date, capital, trading_summary, pnl, open_positions,
        signals_today, risk_alerts, next_actions
    """
```

**Test**: Generate a report from simulated data. Validate all required fields present.

### Task 2.4 — Paper Trading CLI

Entry point for paper trading:

```bash
# Start paper trading session (reads from stdin or runs signal loop)
python src/paper_trader.py --capital 255 --dry-run

# Generate daily report from existing logs
python src/paper_trader.py --report --date 2026-04-30

# Show current paper positions
python src/paper_trader.py --positions

# Show paper PnL summary
python src/paper_trader.py --pnl
```

**Test**: Run `--positions` and `--pnl` with simulated data.

---

## Phase 3: Trading Signals

### Task 3.1 — Price Anomaly Detector

`src/signals.py`:

```python
def detect_price_anomaly(slug: str,
                         threshold_pct: float = 5.0,
                         lookback_minutes: int = 60) -> Signal | None:
    """
    Monitor price changes. Trigger if price moves more than threshold_pct
    in lookback_minutes.

    Uses CLOB price history endpoint.
    """
```

**Test**: Mock a 10% price drop, verify signal triggers. Mock a 1% change, verify no signal.

### Task 3.2 — Spread Detector

```python
def detect_wide_spread(slug: str,
                       threshold_pct: float = 5.0) -> Signal | None:
    """
    Trigger if bid-ask spread exceeds threshold_pct.
    Uses CLOB spread endpoint.
    """
```

**Test**: Mock a market with 8% spread, verify signal triggers. 2% spread, no signal.

### Task 3.3 — Liquidity Change Detector

```python
def detect_liquidity_change(slug: str,
                            volume_threshold: float = 0.5) -> Signal | None:
    """
    Trigger if volume or liquidity changes dramatically (>50% change).
    Compares current vs previous snapshot.
    Requires periodic snapshots stored in logs/market_snapshots/.
    """
```

**Test**: Feed a market with $100k volume after previous $50k, verify trigger.

### Task 3.4 — Expiry Reminder

```python
def detect_approaching_expiry(slug: str,
                              days_before: int = 7) -> Signal | None:
    """
    Check market endDate. Trigger warning if resolution is within days_before.
    """
```

**Test**: Mock a market ending in 3 days with days_before=7, verify trigger.

### Task 3.5 — Arbitrage Checker

```python
def check_arbitrage(yes_price: float, no_price: float) -> Signal | None:
    """
    Check if Yes + No prices sum is significantly different from 1.0.
    If sum < 0.95 or sum > 1.05, flag as potential arbitrage/inefficiency.
    """
```

**Test**: Mock Yes=0.60, No=0.30 (sum=0.90), verify trigger for 10% inefficiency.

### Task 3.6 — Signal Aggregator

```python
def run_all_signals(slug: str, config: dict) -> list[Signal]:
    """
    Run all signal detectors on a market.
    Return list of triggered signals.
    Each signal has:
    - market, token, current_price, trigger_reason
    - suggested_action, confidence, max_suggested_size
    - risks, human_confirmation_required (always True)
    """
```

**Test**: Run signal aggregation on a live market, observe output.

---

## Phase 4: Human-Confirmed Order Drafts

### Task 4.1 — Order Draft Generator

`src/order_draft.py`:

```python
class OrderDraft:
    def __init__(self,
                 market_slug: str,
                 condition_id: str,
                 side: str,        # YES or NO
                 token_id: str,
                 action: str,      # BUY or SELL
                 limit_price: float,
                 size: float,
                 max_cost: float,
                 max_loss: float,
                 reason: str,
                 risk_notes: str): ...
```

### Task 4.2 — Validation Against Risk Rules

```python
# In risk_engine.py:

class RiskEngine:
    def validate_order(self, draft: OrderDraft,
                       config: Config,
                       open_positions: list,
                       daily_trades: int,
                       daily_loss: float,
                       balance: float) -> ValidationResult:
        """
        Check ALL risk rules:
        - R1: max order size
        - R2: max daily loss
        - R3: max daily trades
        - R4: limit order only (verify price is set)
        - R5: whitelist check
        - R8: price limits (no buy YES above 0.95)
        - R9: min spread check
        - R10: balance check
        - R11: duplicate check

        Returns ValidationResult:
        {
            "valid": bool,
            "violations": list[str],  # which rules were violated
            "can_proceed": bool       # True only if all rules pass
        }
        """
```

**Test**: Create drafts that violate each rule, verify each is correctly caught.

### Task 4.3 — Human Confirmation Prompt

```python
def generate_confirmation_prompt(draft: OrderDraft,
                                 best_bid: float,
                                 best_ask: float,
                                 risk_validation: ValidationResult) -> str:
    """
    Generate the confirmation text:

    "我准备创建以下订单草稿，但不会下单，除非你明确回复 CONFIRM：

    Market: <name>
    Side: YES
    Action: BUY
    Token ID: <id>
    Limit price: <price>
    Size: <size>
    Max cost: <cost>
    Max loss: <loss>
    Reason: <reason>
    Risks: <risks>
    Current best bid: <bid>
    Current best ask: <ask>

    请回复 CONFIRM 才能继续。"
    """
```

**Test**: Generate a sample prompt, verify all fields present.

### Task 4.4 — Confirmed Order Flow (dry-run)

```python
def confirmed_order_flow(draft: OrderDraft, config: Config):
    """
    1. Show confirmation prompt
    2. Wait for user input
    3. If user says CONFIRM:
       a. Run risk validation again (fresh check)
       b. If dry_run: log the order, print "DRY RUN — order not submitted"
       c. If live_trading: call submit_limit_order()
    4. If user says anything else: cancel, log, print "Order cancelled."
    """
```

**Test**: Run the flow with dry_run=true, verify it does NOT submit. Verify dry run message.

---

## Phase 5 (Future): Live Trading Interface

These are **design stubs only** — do not implement with real API calls yet.

### Task 5.1 — Interface Design

Document the live trading interface in `src/order_draft.py` as stubs:

```python
def create_order_draft(...) -> OrderDraft:
    """Create draft (phase 4 implemented)"""

def validate_order_against_risk_rules(...) -> ValidationResult:
    """Validate against risk engine (phase 4 implemented)"""

def request_human_confirmation(...) -> bool:
    """Request confirmation (phase 4 implemented)"""

# === LIVE TRADING STUBS (not yet implemented) ===

def submit_limit_order(draft: OrderDraft, config: Config) -> str:
    """Submit limit order to CLOB API.
    Args:
        draft: validated, confirmed order draft
        config: with live_trading=true
    Returns:
        order_id string
    Raises:
        RuntimeError: if dry_run=true or live_trading=false
    """
    raise NotImplementedError("Live trading not yet implemented — see CODING_TASKS.md Phase 5")

def cancel_order(order_id: str, config: Config) -> bool:
    """Cancel open order by ID."""
    raise NotImplementedError("Live trading not yet implemented")

def get_open_orders(config: Config) -> list:
    """Get all open orders for this account."""
    raise NotImplementedError("Live trading not yet implemented")

def get_positions(config: Config) -> list:
    """Get current positions."""
    raise NotImplementedError("Live trading not yet implemented")

def emergency_kill_switch(config: Config) -> bool:
    """Emergency stop all trading."""
    raise NotImplementedError("Live trading not yet implemented")
```

---

## Task Dependency Graph

```
Phase 0 ─► Phase 1 ─► Phase 2 ─► Phase 3 ─► Phase 4 ─► Phase 5 (future)
  │           │           │           │           │
  │           │           │           │           └── Requires: Phase 2, 3
  │           │           │           └── Requires: Phase 1
  │           │           └── Requires: Phase 1
  │           └── Requires: Phase 0
  └── No deps
```

## Implementation Notes

1. **Start with Phase 1 tasks** — they require no credentials and produce immediately useful output.
2. **Test after every task** — each task includes a test you can run to verify.
3. **All URLs are public** — no auth needed for market data endpoints.
4. **Phase 2 paper trading** uses the Phase 1 market data functions.
5. **Phase 3 signals** use Phase 1 data + Phase 2 PnL tracking.
6. **Phase 4** ties everything together with the risk engine.
7. **Phase 5 is stubs only** — do not implement live trading endpoints until explicitly authorized.
