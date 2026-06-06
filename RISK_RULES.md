# Risk Rules — Polymarket Agent

## Overview

Risk rules apply differently depending on the **trading mode**. Read-only mode has zero risk. Paper trading has simulated risk. Live trading enforces all rules in code.

| Mode | Funds at Risk | Rules Enforced |
|------|--------------|---------------|
| **Read-Only** | None | None needed |
| **Paper Trading** | None (simulated) | Same rules as live for realistic simulation |
| **Live Trading** | Real pUSD | All rules below, enforced in code |

---

## Hard Risk Rules (Live Trading)

These rules MUST be enforced in code before any live order is submitted. They are checked by `risk_engine.py` before any order reaches the exchange.

### R1. Maximum Order Size

- **Default**: $25 per order (~10% of capital)
- **Configurable**: Via `MAX_ORDER_SIZE` in `.env`
- **Enforcement**: Reject any order exceeding this value before signing

### R2. Maximum Daily Loss

- **Default**: $50 per day (rolling 24h)
- **Configurable**: Via `MAX_DAILY_LOSS` in `.env`
- **Enforcement**: Track realized + unrealized PnL. If daily loss exceeds limit, reject all new orders until window resets.

### R3. Maximum Daily Trades

- **Default**: 50 trades per day (count includes filled AND cancelled orders)
- **Configurable**: Via `MAX_DAILY_TRADES` in `.env`
- **Enforcement**: Count all order attempts (including drafts submitted for confirmation). Block when limit hit.

### R4. Limit Orders Only

- **No market orders allowed, ever.**
- All orders must specify a `price` and use `POST /order` with `order_type` = `LIMIT`
- Rationale: Market orders on Polymarket can cross the spread and get terrible fills on illiquid markets.

### R5. Whitelist Only

- Only trade markets listed in `ALLOWED_MARKETS` (csv of slugs in `.env`)
- If `ALLOWED_MARKETS` is empty, ALL live orders are rejected
- Paper trading may use any market; live trading is restricted

### R6. No Chasing / No Infinite Retry

- If an order fails (rejected, unfilled, error), do NOT retry automatically
- Log the failure. If a human wants to retry, they must re-confirm.
- Max 1 attempt per order draft. No retry loops.

### R7. Kill Switch

A global emergency stop. Three mechanisms:

```python
# Programmatic kill switch
def emergency_kill_switch():
    """
    Sets LIVE_TRADING = False
    Cancels all open orders
    Blocks any new order submission
    Logs the kill event
    """
    global LIVE_TRADING
    LIVE_TRADING = False
    cancel_all_open_orders()
    log_kill_event()

# CLI kill switch
# python src/order_draft.py --kill

# Config file kill switch
# Set LIVE_TRADING=false in .env and reload
```

The kill switch can be triggered:
- By the user via CLI
- Automatically if daily loss limit is exceeded
- Automatically if more than 3 consecutive order failures
- Automatically if a balance check shows unexpected depletion

Once triggered, the system enters **safe mode** — read-only data still works, but no orders can be submitted until `LIVE_TRADING` is manually reset by the user.

### R8. Price Limits

- **Max price**: Never buy YES above 0.95 (95% probability)
- **Min price**: Never buy NO below 0.05 (5% probability)
- Rationale: Markets near certainty (95%+) have extreme slippage and low expected value. Same for near-impossible events.
- These are hard caps independent of strategy logic.

### R9. Minimum Spread Check

- Do not place an order if the bid-ask spread exceeds 5% (configurable)
- Wide spreads indicate illiquidity and unfavorable fills
- Check both the market's current spread AND the expected fill price vs mid

### R10. Balance Check

- Before any live order, query the authenticated CLOB balance endpoint
- Reject the order if the order cost exceeds available pUSD balance
- Include a buffer: max order size <= 50% of available balance

### R11. Duplicate Order Check

- Do not submit an order identical to an existing open order (same market, same side, same action, same price)
- Check open orders before each new submission
- Rationale: Prevents double-submission from signal re-triggers

### R12. Dry_Run Default

- `DRY_RUN = true` by default
- Setting `LIVE_TRADING = true` requires explicit user action
- When `DRY_RUN = true`, `submit_limit_order()` simulates the order without hitting the CLOB API

### R13. Full Audit Logging

Every trade-significant event must be logged:

| Event | Log Fields |
|-------|-----------|
| Signal detected | timestamp, market, price, trigger reason, confidence |
| Order draft created | Full draft parameters (see PAPER_TRADING_PLAN.md) |
| Order submitted | Order ID, timestamp, parameters |
| Order filled | Fill price, size, timestamp |
| Order cancelled | Reason, timestamp |
| Order failed | Error type, error message, parameters |
| Kill switch triggered | Trigger reason, timestamp, pnl at time of kill |
| Balance check | Balance value, timestamp |

### R14. Graceful Failure

- All errors must be caught. No unhandled exceptions in trading code.
- On any trading error:
  1. Log the error with full context
  2. Do not retry
  3. Do not lose track of funds
  4. Surface the error to the user

### R15. Prohibited Behaviors

1. **No wash trading** — buying and selling the same asset without economic purpose
2. **No spoofing** — placing orders with intent to cancel before execution
3. **No self-trading** — matching your own buy/sell orders
4. **No abusive volume** — placing orders solely to inflate activity
5. **No private key exposure** — never log, print, or transmit private keys
6. **No secret leakage to git** — never commit .env, credentials, or private keys
7. **No browser automation for trading** — never script MetaMask or click through Polymarket UI
8. **No chat-triggered trading** — never execute trades from group chat messages

---

## Paper Trading Risk Rules

Paper trading enforces the same rules as live trading, except:
- The "balance" is a configurable pretend amount
- No real pUSD is at risk
- No CLOB API credentials needed
- All order attempts are logged but never submitted

Paper trading exists to:
1. Validate signal quality
2. Test risk rules without real consequences
3. Build confidence in the system
4. Benchmark expected PnL

---

## Kill Switch Design

```
┌─────────────────────────────────────────────┐
│              Kill Switch                     │
├─────────────────────────────────────────────┤
│  Triggers:                                   │
│  • User CLI command: --kill                  │
│  • Daily loss exceeded (automatic)           │
│  • 3+ consecutive order failures (automatic) │
│  • Balance anomaly (automatic)               │
├─────────────────────────────────────────────┤
│  Effects:                                    │
│  • LIVE_TRADING = false                      │
│  • CANCEL_ALL_OPEN_ORDERS()                  │
│  • BLOCK_NEW_ORDERS()                        │
│  • Log kill event to audit trail             │
│  • Print: "⚠ KILL SWITCH ENGAGED — safe mode"│
├─────────────────────────────────────────────┤
│  Recovery:                                   │
│  • User must manually set LIVE_TRADING=true  │
│  • User must verify wallet balance first     │
│  • User must review kill reason              │
└─────────────────────────────────────────────┘
```

---

## Configuration Reference

```env
# === Risk Limits ===
MAX_ORDER_SIZE=25            # Max $ per order (~10% of capital)
MAX_DAILY_LOSS=50            # Max $ loss per rolling 24h (~20% of capital)
MAX_DAILY_TRADES=50          # Max order attempts per day
ALLOWED_MARKETS=             # Comma-separated slugs (empty = block all)

# === Price Limits ===
MAX_BUY_PRICE_YES=0.95       # Never buy YES above this
MIN_BUY_PRICE_NO=0.05        # Never buy NO below this
MIN_SPREAD_PCT=0.05          # Minimum spread to trade (5%)

# === Mode ===
DRY_RUN=true                 # Default to dry-run
LIVE_TRADING=false           # Must be set to true for real orders
```
