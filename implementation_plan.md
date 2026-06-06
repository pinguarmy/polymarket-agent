# Polymarket Agent — Comprehensive Analysis & Implementation Plan

## What the Project Is

A **personal algorithmic trading system** targeting Polymarket's BTC 5-minute up/down markets using a **"FOLLOW the manipulator" strategy**. The core thesis:

> Thin-book BTC 5-min markets attract whale manipulation. When BTC moves one direction but YES price disagrees (too low or too high), a whale is accumulating in the *opposite* direction, expecting BTC to reverse. Following the whale gives a statistically measurable edge.

**Tested edge from backtest:**
- 63.2% win rate across 87 trades, +$120.39 PnL at $10/trade
- Cross-validated across two time folds: both profitable vs random baseline

### Architecture Summary

```
Binance WS (BTC price) ─┐
                          ├──► realtime_trader.py ──► CLOB V2 (orders)
Polymarket CLOB (YES) ───┘         │
                                    ├──► btc5m.db (SQLite)
                                    ├──► logs/paper_trades.jsonl
                                    └──► logs/trade_alert.json
```

**Phase maturity:**
- ✅ Phase 1–4: Market data, paper trading, signals, order drafts — **done**
- ✅ Phase 5 scaffolding: `live_trader.py`, `clob_client.py` — **done**
- 🔴 Phase 5 execution: TP/SL don't submit actual CLOB sell orders — **not done**
- 🔴 Phase 5 fill tracking: orders recorded as success before verifying fill — **not done**

---

## Critical Bugs (Must Fix Before Live Trading)

### 🔴 Bug 1: TP/SL only logs, doesn't submit CLOB sell orders
**File:** `src/realtime_trader.py` → `_close_position()`

`_close_position()` writes to `paper_trades.jsonl` and updates in-memory PnL, but **never submits a CLOB cancel/sell order**. In live mode this means open positions are never actually closed — the market settles against you.

**Fix:** In live mode (`not self.dry_run`), after `_close_position()` records the exit, call `self.clob.cancel_order()` if an order ID is stored, or place a limit SELL at market price.

### 🔴 Bug 2: Entry recorded without verifying fill
**File:** `src/realtime_trader.py` → `_execute_trade()`

```python
if result.get("success"):
    self._record_entry(signal)  # recorded as filled
```

`result["success"]` only means the order was *accepted*, not filled. In live trading you can have unfilled orders with open positions tracked as filled.

**Fix:** After posting the order, poll `client.get_order(order_id)` until status is `MATCHED` or timeout. Only then call `_record_entry()` with the actual fill price.

### 🔴 Bug 3: realtime_trader.py parameters diverge from STRATEGY_REPORT optimal values
**File:** `src/realtime_trader.py` lines 70–98

Current live thresholds (`YES_LOW=0.50`, `YES_HIGH=0.55`, `MIN_BTC_MOVE=7.0`) differ significantly from the backtested optimal values (`YES_LOW=0.25`, `YES_HIGH=0.45`, `MIN_BTC_MOVE` asymmetric per side). This means the actual trading logic has never been tested against the strategy that was validated.

---

## Medium-Priority Issues

### 🟡 Issue 4: Backtest uses midpoint, not bid/ask (slippage underestimated)
All backtests simulate entry/exit at the midpoint price. Real fills happen at the ask (for buys) and bid (for sells). On thin BTC 5-min books this spread can be 2–5%, which can eliminate the edge entirely.

**Fix:** In `honest_backtest.py` and `stoploss_backtest.py`, add a `slippage_model` that uses `best_ask + 0.005` for entries and `best_bid - 0.005` for exits.

### 🟡 Issue 5: No trading cost model in backtests
Polymarket charges ~0.02 on the spread per trade. With 87 trades at $10 each, that's ~$17.40 in unmodelled cost — 14% of the reported +$120 PnL.

### 🟡 Issue 6: YES_LOW threshold direction error (`param_analysis.md`)
Current code: `yes_price <= YES_LOW_THRESHOLD (0.50)` — this means YES=0.23 (the strongest signal) passes, but YES=0.23 with a threshold of 0.40 **does not** pass. The param_analysis correctly identified this.

The optimal values from the 1,225-combination backtest sweep:
- YES_LOW = 0.25 (for BUY NO signal: BTC up + YES below 0.25)
- YES_HIGH = 0.45 (for BUY YES signal: BTC down + YES above 0.45)

### 🟡 Issue 7: No fill-tracking loop for order confirmations
The kill-switch auto-triggers on 3 consecutive order failures (per `RISK_RULES.md`), but `_execute_trade()` has no consecutive-failure counter.

### 🟡 Issue 8: `honest_backtest.py` hardcodes an absolute file path
```python
db = sqlite3.connect('./data/btc5m.db')
```
This breaks on any machine other than yours and is fragile on path changes. Should use `Path(__file__).parent.parent / "data" / "btc5m.db"`.

### 🟡 Issue 9: Only 2 test files covering < 5% of codebase
`tests/` has only 2 files testing market discovery and collectors. Core logic (`realtime_trader`, `risk_engine`, `signals`, `backtest`) has zero unit tests.

---

## Improvement Ideas (Beyond Bug Fixes)

### 1. Walk-forward validation (highest priority analytical gap)
The current 213-window backtest is all from ~2 days of data. The STRATEGY_REPORT itself flags this. You need **at least 1,000 windows** over 10+ days to confirm parameters aren't overfit to a 2-day regime.

**Implement:** Script that runs the strategy on rolling 200-window training sets, fits parameters, then evaluates on the next 50 windows (4-fold minimum).

### 2. Tiered take-profit (partial exit)
Backtested recommendation: sell 50% of position at YES=0.70, hold remaining until 0.85/0.88. This locks in guaranteed profit on the first half while letting the winner run. Current code only has a single TP level.

### 3. Time-based stop (prevent holding through late-window chaos)
Enter at t=60s, hold until t=270s (4.5 min). If by t=180s the position hasn't moved in your favor by at least 0.02, exit. This avoids sitting through the final 90 seconds of erratic pricing. `param_analysis.md` independently arrived at the same recommendation.

### 4. Independent YES/NO cooldown timers
Current code shares `entries_this_market` count for both YES and NO sides. Should track `entries_yes` and `entries_no` independently so a NO position doesn't prevent a valid YES signal from the same window.

### 5. Multi-asset expansion
Same FOLLOW strategy could apply to ETH, SOL up/down 5-min markets. BTC moves correlated with ETH — cross-asset signals could improve frequency without adding per-asset risk.

### 6. Trailing stop
Replace fixed `STOP_LOSS_YES_MOVE=0.12` with a trailing stop: once the position is +0.04 in your favor, never let it fall below break-even. This is especially important for the long tail of winners.

### 7. Dashboard improvements
`dashboard.html` exists (80KB) but doesn't appear to have live WebSocket connection to the trader. A proper real-time dashboard showing:
- Current market window countdown
- BTC move vs YES price chart
- Open positions with unrealized PnL
- Win/loss streak, daily PnL curve

---

## Constraints (Hard Lines to Maintain)

These are non-negotiable regardless of how confident you feel:

| Constraint | Rationale |
|-----------|-----------|
| **Never raise MAX_ORDER_SIZE above $25** until 1,000-window walk-forward passes | Sizing is the multiplier on all your error |
| **No live trading without fill verification** (Bug 2 must be fixed first) | Can have phantom positions |
| **No live trading without CLOB sell on TP/SL** (Bug 1 must be fixed first) | Positions never close |
| **Params in realtime_trader must match STRATEGY_REPORT** before any live test | You'd be testing a different strategy |
| **Kill switch must auto-trigger on 3 consecutive failures** | Per RISK_RULES.md, not yet enforced in code |
| **Data must be collected for ≥10 days before sizing up** | 2-day backtest is statistically insufficient |
| **Always run --dry-run for at least 5 trading sessions** after any parameter change | Prevents surprise behavior in live |

---

## Prioritized Implementation Plan

### Phase A: Pre-live Critical Fixes (Do First — Blocking)

> [!CAUTION]
> Do not attempt live trading until all Phase A items are complete.

#### A1 — Sync realtime_trader.py parameters with STRATEGY_REPORT
**File:** `src/realtime_trader.py` lines 70–98  
Change: `YES_LOW_THRESHOLD = 0.25`, `YES_HIGH_THRESHOLD = 0.45`, asymmetric BTC minimums per the report.  
**Effort:** 30 min | **Risk:** Low (dry-run only)

#### A2 — Implement CLOB sell order on TP/SL exit (live mode only)
**File:** `src/realtime_trader.py` → `_close_position()`  
Change: store `order_id` from `_execute_trade()` in `self.entry_order_ids[]`. In `_close_position()`, if `not self.dry_run`, call `self.clob.cancel_order(entry_order_ids[i])` (cancel unfilled) or post a limit SELL at ask.  
**Effort:** 3–4 hrs | **Risk:** Medium (CLOB auth required)

#### A3 — Verify fill before recording entry
**File:** `src/realtime_trader.py` → `_execute_trade()`  
Change: poll `get_order(order_id)` with 5s timeout, only call `_record_entry()` if status == MATCHED.  
**Effort:** 2 hrs | **Risk:** Low

#### A4 — Consecutive failure counter + auto kill-switch
**File:** `src/realtime_trader.py`  
Add `self._consecutive_failures = 0`. Increment on order failure. Call `emergency_kill_switch()` at 3.  
**Effort:** 1 hr | **Risk:** Low

#### A5 — Fix honest_backtest.py hardcoded path
**File:** `src/honest_backtest.py` line 15  
Change to: `Path(__file__).resolve().parent.parent / "data" / "btc5m.db"`  
**Effort:** 5 min | **Risk:** None

### Phase B: Backtest Integrity (Do Before Sizing Up)

#### B1 — Add bid/ask slippage model to backtests
**Files:** `src/honest_backtest.py`, `src/stoploss_backtest.py`  
Add `slippage=0.005` parameter. Entry at `ask + slippage`, exit at `bid - slippage`.  
**Effort:** 2 hrs

#### B2 — Add trading cost model (0.02 per leg)
Same files. Deduct `0.02 × position_size` on entry and exit.  
**Effort:** 1 hr

#### B3 — Walk-forward validation script
New script: `src/walk_forward_backtest.py`  
Implement 4+ rolling folds over all collected data. Report per-fold win rate and PnL.  
**Effort:** 4 hrs

#### B4 — Collect 10+ days of data before re-running sweep
Not a code task — just time. Run `realtime_trader --dry-run` continuously and let the DB grow.

### Phase C: Strategy Improvements

#### C1 — Tiered take-profit (TP1 at 0.70, TP2 at 0.85)
**File:** `src/realtime_trader.py` → `_check_stop_loss()`  
Add `TAKE_PROFIT_1 = 0.70`. When triggered, close 50% of size. Keep remaining until `TAKE_PROFIT_2`.  
**Effort:** 2 hrs

#### C2 — Time-based stop
Add `self.entry_timestamps[]`. In the main loop, if `time.time() - entry_ts > 120` and position not in profit by 0.02, exit.  
**Effort:** 1.5 hrs

#### C3 — Independent YES/NO cooldown
Replace shared `entries_this_market` with `entries_yes` and `entries_no`.  
**Effort:** 1 hr

#### C4 — Trailing stop
Replace fixed stop with: once P&L > +0.03, stop loss moves to break-even.  
**Effort:** 1.5 hrs

### Phase D: Infrastructure & Testing

#### D1 — Unit tests for risk_engine
**File:** `tests/test_risk_engine.py`  
Test each of R1–R17 with a draft that violates exactly one rule.  
**Effort:** 3 hrs

#### D2 — Unit tests for signal detection
**File:** `tests/test_signals.py`  
Mock price feeds, verify each signal type triggers at correct thresholds.  
**Effort:** 2 hrs

#### D3 — Unit tests for backtest engine
**File:** `tests/test_backtest.py`  
**Effort:** 2 hrs

#### D4 — Dashboard live connection
Connect `dashboard.html` to a WebSocket endpoint in `dashboard_api.py` for real-time state.  
**Effort:** 4 hrs

---

## Recommended Execution Order

```
TODAY:        A1 (param sync) + A5 (path fix)
THIS WEEK:    A2 + A3 + A4 (live-trading blockers)
NEXT WEEK:    B1 + B2 (honest backtests) → re-run backtest with costs
ONGOING:      Collect data. Wait for 1,000+ windows.
THEN:         B3 (walk-forward) → if passes, go live small ($5/trade)
THEN:         C1–C4 (strategy improvements on live data)
```

## Open Questions

1. **Target capital at live launch?** The STRATEGY_REPORT suggests $10/trade initially. Are you comfortable with $8–15/trade sizing?
2. **Which CLOB sell mechanism on TP/SL?** Cancel the original buy order (if still open) or post a new sell? In a 5-min window the original buy is likely already filled, so you need a **new sell order**.
3. **Fill polling timeout?** If an order isn't filled in 5s, should we cancel and re-enter at a new price, or cancel and skip the trade entirely? Recommendation: **cancel and skip** (no-chase rule).
4. **How many days of dry-run after A1–A4 before going live?** Recommendation: **minimum 5 sessions** with ≥2 actual signal triggers per session.
