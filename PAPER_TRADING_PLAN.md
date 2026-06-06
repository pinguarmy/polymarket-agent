# Paper Trading Plan — Data Structures, Logging, PnL

## Overview

Paper trading simulates trades without touching real funds. Every decision is logged. PnL is calculated theoretically. The system records every signal and action for later analysis.

---

## Data Structures

### PaperTrade Entry

Every paper trade is recorded as a structured log entry. Format for JSONL storage:

```json
{
  "timestamp": "2026-04-30T14:30:00Z",
  "session_id": "sess_abc123",
  "market_slug": "will-donald-trump-win-the-2028-presidential-election",
  "condition_id": "0xabc123def456",
  "token_id": "12345",
  "side": "YES",
  "action": "BUY",
  "simulated_price": 0.65,
  "simulated_size": 10,
  "simulated_cost": 6.50,
  "simulated_fill_type": "maker",
  "reason": "Price dropped 5% in 1 hour, reversion expected",
  "signal_type": "price_anomaly",
  "confidence": 0.65,
  "expected_edge": 0.03,
  "risk_notes": "Low liquidity on No side, wide spread 4%",
  "human_confirmed": false,
  "entry_price": null,
  "exit_price": null,
  "theoretical_pnl": null,
  "theoretical_pnl_pct": null,
  "status": "open"
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | ISO 8601 | When the trade was recorded |
| `session_id` | string | Session identifier for grouping |
| `market_slug` | string | Polymarket event slug |
| `condition_id` | string | Hex condition ID |
| `token_id` | string | CLOB token ID for Yes or No |
| `side` | string | YES or NO |
| `action` | string | BUY or SELL |
| `simulated_price` | float | Price at which trade was simulated |
| `simulated_size` | float | Number of shares |
| `simulated_cost` | float | Total cost = price * size |
| `simulated_fill_type` | string | maker (limit order) or taker (crossed spread) |
| `reason` | string | Human-readable explanation |
| `signal_type` | string | Which signal triggered the trade |
| `confidence` | float | 0.0 to 1.0 |
| `expected_edge` | float | Expected positive return (as decimal, e.g. 0.03 = 3%) |
| `risk_notes` | string | Any risks the system identified |
| `human_confirmed` | bool | Whether a human approved this in confirmed mode |
| `entry_price` | float | Filled at simulation time |
| `exit_price` | float | Set when trade is closed |
| `theoretical_pnl` | float | Calculated on exit. In pUSD. |
| `theoretical_pnl_pct` | float | PnL as percentage of cost |
| `status` | string | open, closed, cancelled |

### Session Summary

```json
{
  "session_id": "sess_abc123",
  "start_time": "2026-04-30T08:00:00Z",
  "end_time": "2026-04-30T16:00:00Z",
  "initial_capital": 255.00,
  "current_capital": 258.20,
  "total_trades": 5,
  "open_trades": 2,
  "closed_trades": 3,
  "wins": 2,
  "losses": 1,
  "total_pnl": 3.20,
  "total_pnl_pct": 1.25,
  "win_rate": 0.67,
  "avg_win": 2.10,
  "avg_loss": -1.00,
  "max_drawdown": 0.50,
  "sharpe_ratio": null
}
```

---

## Logging

### Log File Structure

```
logs/
├── paper_trades.jsonl        # Every paper trade (append-only)
├── paper_sessions.jsonl      # Session summaries
├── signals.jsonl             # Every signal generated (even untraded)
├── market_snapshots/         # Periodic market data snapshots
│   └── YYYY-MM-DD/
│       ├── snap_HHMMSS.json
│       └── ...
└── daily_reports/
    └── YYYY-MM-DD_report.json
```

### Signal Log Entry

Every signal, even if not acted on, is logged:

```json
{
  "timestamp": "2026-04-30T14:30:00Z",
  "signal_type": "price_anomaly",
  "market_slug": "will-donald-trump-win-the-2028-presidential-election",
  "token_id": "12345",
  "current_price": 0.65,
  "previous_price": 0.70,
  "change_pct": -7.14,
  "trigger_reason": "Price dropped 7.14% in 1 hour (threshold: 5%)",
  "suggested_action": "BUY YES",
  "suggested_size": 10,
  "confidence": 0.65,
  "expected_edge": 0.03,
  "risks": "Event-specific risk: outcome may have shifted, not just noise",
  "human_confirmation_required": true,
  "acted_upon": false,
  "paper_trade_ref": null
}
```

---

## PnL Calculation

### Entry Cost

```
entry_cost = entry_price * size
```

For BUY YES at 0.65, size 10:
- entry_cost = 0.65 * 10 = 6.50 pUSD

### Exit Value

```
exit_value = exit_price * size
```

For SELL YES at 0.72, size 10:
- exit_value = 0.72 * 10 = 7.20 pUSD

### Realized PnL

```
realized_pnl = exit_value - entry_cost
```

For above example: 7.20 - 6.50 = +0.70 pUSD

### Unrealized PnL

```
unrealized_pnl = (current_mid_price * size) - entry_cost
```

### Total PnL (Session)

```
total_pnl = sum(all realized_pnl) + sum(all unrealized_pnl of open trades)
```

### Win Rate

```
win_rate = number_of_winning_trades / number_of_closed_trades
```

### Max Drawdown

Track peak capital over time. Drawdown at any point:

```
drawdown = (peak_capital - current_capital) / peak_capital
```

Max drawdown = peak value of all drawdowns over the session.

### Settlement at Resolution

For YES tokens that resolve to 1.0:

```
resolution_pnl = (1.0 - entry_price) * size
```

For YES tokens that resolve to 0.0:

```
resolution_pnl = -entry_cost  (= full loss)
```

---

## Daily Report Format

Generated at end of day or on request. Saved to `logs/daily_reports/`.

```json
{
  "date": "2026-04-30",
  "report_generated_at": "2026-04-30T16:00:00Z",
  "capital": {
    "initial": 255.00,
    "current": 258.20,
    "change": 3.20,
    "change_pct": 1.25
  },
  "trading_summary": {
    "total_trades_today": 5,
    "open_positions": 2,
    "closed_trades": 3,
    "wins": 2,
    "losses": 1,
    "win_rate": 0.67,
    "avg_hold_time": "4h 20m"
  },
  "pnl": {
    "realized_pnl": 2.50,
    "unrealized_pnl": 0.70,
    "total_pnl": 3.20,
    "max_drawdown": 0.50,
    "max_drawdown_pct": 0.20
  },
  "open_positions": [
    {
      "market_slug": "will-donald-trump-win-the-2028",
      "side": "YES",
      "entry_price": 0.55,
      "current_price": 0.57,
      "size": 20,
      "cost": 11.00,
      "unrealized_pnl": 0.40,
      "status": "open"
    }
  ],
  "signals_today": 12,
  "signals_acted_upon": 5,
  "largest_win": 1.20,
  "largest_loss": -0.80,
  "risk_alerts": [
    "Spread > 5% on: will-bitcoin-hit-100k-before-june"
  ],
  "markets_monitored": 3,
  "next_actions": [
    "Check resolution date for: will-the-fed-cut-rates-in-2026",
    "Review open position on: will-donald-trump-win-the-2028"
  ]
}
```

---

## Paper Trading Engine Design (Phase 2)

### Core Loop

```
1. Load config (capital, risk params, market whitelist)
2. Poll public market data every N seconds
3. Run signal detectors on latest data
4. If signal triggers:
   a. Log signal
   b. Determine trade parameters (side, size, price)
   c. Validate against paper risk rules
   d. Record paper trade entry
   e. Update capital balance
5. Check open positions for exit signals:
   a. If exit triggered, record close with PnL
   b. Update capital balance
6. Generate summary every N minutes / on demand
```

### Exit Conditions

Paper positions exit when any of:
- **Take-profit**: Price reaches target (entry + expected_edge)
- **Stop-loss**: Price drops below entry * (1 - max_loss_pct)
- **Signal reversal**: Opposite signal detected for same market
- **Market resolution**: Market closes and resolves
- **Manual**: User requests exit
- **Time decay**: Position held past expiration threshold

### Key Design Principles

1. **Deterministic fills**: Paper trades fill at the best bid (for sells) or best ask (for buys) at time of signal. This is a conservative assumption.
2. **No slippage simulation**: Paper trades assume full fill at target price. This is optimistic. Note this in logs.
3. **No latency simulation**: Paper trades execute instantly. Real trades have network + blockchain latency.
4. **Capital tracking**: Paper trading tracks a virtual balance. Every trade reduces it (cost), every exit increases it (proceeds).
5. **No partial fills**: Paper assumes full fills. Real Polymarket orders may partially fill.
6. **Fee simulation**: Optionally deduct 0.1-0.3% maker/taker fees to be more realistic.
