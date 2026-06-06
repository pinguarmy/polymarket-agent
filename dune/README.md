# Dune Historical Backfill

Free-plan backfill pipeline for Polymarket BTC 5-minute markets using Dune Analytics.

## How it works

```
Dune (historical trades) → CSV export → dune_import.py → SQLite → dune_backtest.py → PnL
                                                          ↑
                                         Binance (BTC prices, free)
```

## Steps

### 1. Export trades from Dune

1. Go to https://dune.com
2. Create a new query
3. Paste the SQL from `dune/btc_5min_trades.sql`
4. Run → wait for results
5. Export as CSV → save anywhere, e.g. `~/Downloads/dune_export.csv`

### 2. Import into local database

```bash
cd ~/Projects/polymarket-agent
python3 dune/dune_import.py --csv ~/Downloads/dune_export.csv
```

This will:
- Read the CSV
- Insert markets, tokens, and trades into `data/btc5m.db`
- Download BTC/USDT prices from Binance (free, no API key)
- Deduplicate against existing data

### 3. Run historical backtest

```bash
python3 dune/dune_backtest.py --db data/btc5m.db --output logs/dune_pnl.json
```

This runs the FOLLOW strategy against ALL BTC 5-min markets in the database.

### 4. View dashboard

```bash
python3 src/dashboard_api.py --port 8765
```

Open http://localhost:8765 — the PnL chart will include Dune data.

## Dune free plan limits

- Unlimited CSV exports (no credits used)
- SQL query editor is free
- No API key required for CSV workflow
- Max ~250K rows per query result (use date filters for more)

## Files

- `dune/btc_5min_trades.sql` — Dune SQL query
- `dune/dune_import.py` — CSV import + BTC download
- `dune/dune_backtest.py` — Historical backtest engine
