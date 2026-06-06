# Polymarket Agent — Safety-First Prediction Market Research Lab

A **human-in-the-loop** Polymarket research and paper-trading assistant for exploring prediction-market data, BTC 5-minute markets, risk controls, and transparent trading workflows.

Think of it as a small research cockpit: collectors pull market and price data, strategy scripts test ideas, dashboards make the system observable, and the risk engine keeps the project grounded in safe defaults before anything can approach real trading.

## Project Goal

Build a controllable, observable Polymarket system that progresses through defined safety stages:

```
Read-Only Market Data  →  Paper Trading  →  Human-Confirmed Trading  →  (future)
```

No stage unlocks until the previous stage is stable and verified. The final stage (live trading) requires human confirmation on every order.

## Phase Roadmap

| Phase | Name | Status | Description |
|-------|------|--------|-------------|
| 0 | Planning & Docs | Active | Requirements, architecture, risk rules defined here |
| 1 | Read-Only Market Data | Next | Fetch markets, order books, prices, trades — no auth needed |
| 2 | Paper Trading | Planned | Simulate trades with theoretical PnL, no real funds |
| 3 | Trading Signals | Planned | Price anomalies, spread alerts, event triggers |
| 4 | Human-Confirmed Orders | Planned | Generate order drafts, ask before submitting |
| 5 | Live Trading (capped) | Future | Real orders with hard risk limits, dry_run=true by default |

## Safety Principles

1. **Human-in-the-loop**: Every real order requires explicit human CONFIRM
2. **Default dry_run**: All trading functions default to dry-run mode
3. **Hard limits enforced in code**: Max order size, max daily loss, whitelist
4. **No market orders**: Limit orders only
5. **Kill switch**: Emergency stop accessible at any time
6. **Full audit trail**: Every signal, trade decision, and order logged
7. **No auto-compounding**: No automatic position scaling
8. **No chasing**: No retry loops on failed orders
9. **Sandboxed credentials**: Secrets never logged, never committed

## Directory Structure

```
polymarket-agent/
├── README.md                # This file
├── REQUIREMENTS.md          # Credentials & environment setup
├── RISK_RULES.md            # Risk control rules
├── PAPER_TRADING_PLAN.md    # Paper trading data structures
├── CODING_TASKS.md          # Implementation tasks for Codex
└── .env                     # Secrets (gitignored)
```

Additional files created as implementation progresses:

```
├── src/
│   ├── __init__.py
│   ├── config.py            # Configuration loader
│   ├── market_data.py       # Read-only market data (Phase 1)
│   ├── paper_trader.py      # Paper trading engine (Phase 2)
│   ├── signals.py           # Trading signal detection (Phase 3)
│   ├── order_draft.py       # Human-confirmed order flow (Phase 4)
│   ├── risk_engine.py       # Risk rule validation
│   ├── logger.py            # Structured logging
│   └── tracker.py           # PnL & position tracker
├── logs/                    # All trading logs
├── data/                    # Market snapshots, paper trades, configs
├── scripts/                 # One-shot utility scripts
└── tests/                   # Unit tests
```

## Usage

### Phase 1: Read-Only Market Data

```bash
# Fetch a market by slug
python src/market_data.py --slug will-donald-trump-win-the-2028-presidential-election

# Fetch order book
python src/market_data.py --slug <slug> --orderbook

# Generate market summary
python src/market_data.py --slug <slug> --summary
```

### Phase 2: Paper Trading

```bash
# Run paper trading session
python src/paper_trader.py --capital 255 --dry-run

# Generate daily report
python src/paper_trader.py --report
```

### Phase 4: Confirmed Trading (future)

```bash
# Generate order draft for review
python src/order_draft.py --market <slug> --side YES --action BUY --size 5

# Confirm and submit (only with explicit CONFIRM)
# (This will never auto-submit without human approval)
```

## Related Projects

- See the [Poly Trade](https://github.com/nousresearch/poly-trade) project for the reference trading bot architecture this is based on.
- Polymarket CLOB API: [docs.polymarket.com](https://docs.polymarket.com)

## Disclaimer

This project is for research and educational purposes only. It is not financial advice. Do not use it with real funds without independent review, explicit human confirmation, and strong risk controls.

## Publication status

This repository is prepared for public review as a sanitized architecture/code snapshot. Runtime data, logs, local databases, private API keys, wallet keys, wallet addresses, and one-off live-order scripts are intentionally excluded.

## License

MIT License. See [LICENSE](LICENSE).
