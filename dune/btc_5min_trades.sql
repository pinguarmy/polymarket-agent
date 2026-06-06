-- Dune Query: Polymarket BTC 5-Minute Market Trades
-- 
-- Paste into https://dune.com → Run → Export CSV
-- 
-- BTC 5-min markets use slug pattern: btc-updown-5m-{unix_timestamp}
-- Uses CAST for condition_id type compatibility

SELECT
  t.block_time,
  t.condition_id,
  m.slug,
  t.side,
  t.outcome,
  t.price,
  t.size
FROM polymarket_polygon.market_trades t
JOIN polymarket_polygon.market_details m 
  ON CAST(t.condition_id AS VARCHAR) = CAST(m.condition_id AS VARCHAR)
WHERE m.slug LIKE 'btc-updown-5m-%'
ORDER BY t.block_time DESC
LIMIT 200000
