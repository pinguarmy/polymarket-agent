# Hyperliquid HYPE/USDC Depth Analysis

## Summary
- **HYPE current mid price**: ~$44.55 (from Hyperliquid allMids + order book BBO)
- **Budget to push price DOWN $0.001**: ~$554 USDC (by buying from asks)
- **Alternative (selling into bids)**: ~$379 USDC

## Methodology
1. Query https://api.hyperliquid.xyz/info with {"type": "l2Book", "coin": "HYPE"}
2. To move mid price DOWN by $0.001, consume liquidity on the ask side (buy HYPE)
3. After buying, new mid = (best_bid + new_ask) / 2 = $44.55 - $0.001 = $44.549

## Current Order Book Snapshot

### Top Bid Levels
| Level | Price     | Size (HYPE) | USDC Value | Cumulative |
|-------|-----------|-------------|------------|------------|
| 1     | $44.550   | 8.50        | $378.67    | $378.67    |
| 2     | $44.549   | 4.28        | $190.58    | $569.25    |
| 3     | $44.548   | 344.27      | $15,329.65 | $15,898.90 |
| 4     | $44.547   | 228.98      | $10,195.79 | $26,094.69 |
| 5     | $44.545   | 5.00        | $222.62    | $26,317.31  |

### Top Ask Levels
| Level | Price     | Size (HYPE) | USDC Value | Cumulative |
|-------|-----------|-------------|------------|------------|
| 1     | $44.557   | 12.44       | $554.29    | $554.29    |
| 2     | $44.558   | 44.88       | $1,999.74  | $2,554.03  |
| 3     | $44.559   | 180.52      | $8,038.67  | $10,592.70 |
| 4     | $44.561   | 56.47       | $2,515.84  | $13,108.54 |
| 5     | $44.562   | 21.00       | $935.38    | $14,043.92 |

## Calculation

Target: Move mid price from $44.55 to $44.549 (-$0.001)

Mechanism: Buy HYPE from ask side, raising the ask price, which lowers the mid.

New ask price needed: new_mid = (best_bid + new_ask) / 2
$44.549 = ($44.55 + new_ask) / 2
new_ask = $44.548

Answer: Consuming just the best ask level ($44.557) with 12.44 HYPE = $554.29 USDC

## Comparison with XRP on Binance

| Metric              | XRP/Binance | HYPE/Hyperliquid |
|---------------------|-------------|------------------|
| Price               | $1.4165     | $44.55           |
| Push down $0.001    | ~$619,000   | ~$554            |
| Depth ratio         | Very deep   | Shallow          |

HYPE is much more susceptible to price manipulation due to shallow order book depth.

## Polymarket HYPE Markets
- Result: No active Polymarket markets found for HYPE or Hyperliquid token price
- Polymarket CLOB API returned 403 errors for GraphQL endpoint
- Standard REST API accessible but search returned unrelated markets

## Notes
- Hyperliquid API endpoint spotL2Book returns 422 error; correct endpoint is l2Book
- HYPE token has index 150 in the Hyperliquid token universe
- HYPE is NOT a canonical token (isCanonical: false) - likely a bridge token
- The order book has only 20 bid + 20 ask levels visible (likely truncated)
