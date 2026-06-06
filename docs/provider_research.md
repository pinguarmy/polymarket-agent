# Polymarket BTC 5-Minute — Provider Research

Last updated: 2026-04-30

## Summary Table

| Provider | Past Data? | Free? | Data Type | Limits | Status |
|----------|-----------|-------|-----------|--------|--------|
| **Polymarket Official** | Partial | Yes | Metadata (Gamma), prices-history (CLOB), trades (Data API) | prices-history often empty for short-lived markets; Data API trades may work | ACTIVE |
| **Goldsky** | Yes (Subgraph) | Free Starter tier | Order Filled, Orders Matched, Market Open Interest, User Positions, User Balances | Starter: 1M rows/month, 10GB storage, 1 project | RESEARCH |
| **Dune** | Yes | Free credits | Community Polymarket tables, fills, trades | Credit limits per query; free tier sufficient for sampling | RESEARCH |
| **CryptoHouse/ClickHouse** | Unknown | Unknown | Polymarket SQL datasets | May require registration | CHECK |
| **Allium** | Unknown | Trial only | Prediction market data | Enterprise-focused; free trial limited | DEPRIORITIZE |
| **Telonex** | Yes (tick-level) | Free samples | Trades, order books, quotes, on-chain fills | Free tier very limited; sample only | SAMPLE ONLY |
| **Kaggle/GitHub** | Variable | Free | CSV/Parquet datasets | Quality varies; validate before trusting | SECONDARY |
| **Polygon Logs** | Yes | Free (RPC) | On-chain fills only | No order book history; RPC rate limits | FALLBACK |

---

## 1. Polymarket Official APIs

### Gamma API
- Endpoint: `gamma-api.polymarket.com`
- Provides: market metadata, slugs, token IDs, condition IDs, prices
- BTC 5-min markets use predictable slug format: `btc-updown-5m-{unix_ts}`
- Markets disappear from API shortly after closing (1-2 hours)
- **Usable for: live market discovery, not historical**

### CLOB API
- Endpoint: `clob.polymarket.com`
- `/prices-history`: Returns empty for most short-lived BTC 5-min markets
- `/book`: Order book works for active markets
- `/price`, `/midpoint`, `/spread`: Works for active token IDs
- **Usable for: live order book snapshots, not historical prices**

### Data API
- Endpoint: `data-api.polymarket.com`
- `/trades`: Returns trade history for any condition_id, even after market closes
- **KEY FINDING: HTTP 200 with valid data for closed markets — BUT requires condition_id which only comes from Gamma while market is active**
- **Strategy: Store condition_ids during live collection, then query Data API historically**
- **Status: WORKS — our live collector must store condition_ids immediately**

---

## 2. Goldsky

- URL: https://goldsky.com
- Free Starter tier: 1M rows/month, 10GB storage, 1 project
- Polymarket subgraph datasets available:
  - Order Filled events
  - Orders Matched events
  - Market Open Interest
  - User Positions
  - User Balances
- **Potential: Best free option for historical fill data**
- **Next step: Check Starter tier access, query BTC 5-min market fills**

---

## 3. Dune

- URL: https://dune.com
- Free tier with query credits
- Community Polymarket dashboards exist
- Known tables: polymarket.trades, polymarket.orders
- **Potential: Good for sampling historical fills**
- **Next step: Search for Polymarket BTC 5-min dashboards, test a query**

---

## 4. CryptoHouse / ClickHouse

- URL: https://cryptohouse.io
- May have Polymarket SQL datasets
- Access model unclear
- **Status: CHECK availability**

---

## 5. Allium

- URL: https://allium.so
- Enterprise-focused blockchain data
- Free trial may exist
- Prediction market coverage unknown
- **Status: DEPRIORITIZE — enterprise, unlikely free for our scale**

---

## 6. Telonex

- URL: https://telonex.com
- Claims tick-level trades, order books, quotes, on-chain fills
- Free tier: samples only, very limited
- **Potential: Schema validation only, not primary source**
- **Next step: Check if free sample covers any Polymarket BTC data**

---

## 7. Kaggle / GitHub Open Datasets

- Search: "polymarket dataset", "polymarket trades", "polymarket btc"
- Quality varies widely
- **Use only as secondary validation, never as primary source**
- **Next step: Search and catalog available datasets**

---

## 8. Direct Polygon Logs (Fallback)

- Polymarket CTF Exchange contracts on Polygon
- Can reconstruct on-chain fills from event logs
- Requires: RPC endpoint, contract ABIs, event parsing
- Does NOT provide: order book snapshots, cancelled orders
- **Fallback only if all other providers fail**

---

## Recommendation

1. **Start with live collector immediately** — our own data is mandatory
2. **Test Polymarket Data API** for trade history on closed markets
3. **If Data API works** → backfill recent BTC 5-min markets
4. **If Data API fails** → test Goldsky Starter tier
5. **If Goldsky accessible** → query fills for BTC 5-min markets
6. **Dune** as secondary validation source
7. **Polygon logs** as absolute fallback
