CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    slug TEXT UNIQUE,
    question TEXT,
    condition_id TEXT,
    yes_token_id TEXT,
    no_token_id TEXT,
    open_time TEXT,
    close_time TEXT,
    resolution TEXT,
    volume REAL,
    active INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS token_mapping (
    token_id TEXT PRIMARY KEY,
    market_id TEXT,
    side TEXT CHECK (side IN ('YES', 'NO')),
    FOREIGN KEY (market_id) REFERENCES markets (market_id)
);

CREATE TABLE IF NOT EXISTS polymarket_price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    best_bid REAL,
    best_ask REAL,
    midpoint REAL,
    spread REAL,
    spread_pct REAL,
    last_trade_price REAL,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS polymarket_orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    token_id TEXT,
    side TEXT,
    bid_depth_5 REAL,
    ask_depth_5 REAL,
    raw_bids TEXT,
    raw_asks TEXT,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS polymarket_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    trade_timestamp TEXT,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS binance_btc_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    price REAL,
    bid REAL,
    ask REAL,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    side TEXT,
    action TEXT,
    price REAL,
    size REAL,
    cost REAL,
    pnl REAL,
    reason TEXT,
    signal_type TEXT,
    created_at TEXT,
    closed_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS real_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    side TEXT,
    action TEXT,
    price REAL,
    size REAL,
    cost REAL,
    pnl REAL,
    order_id TEXT,
    reason TEXT,
    created_at TEXT,
    closed_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS provider_backfill_status (
    provider TEXT PRIMARY KEY,
    status TEXT,
    last_checked TEXT,
    markets_found INTEGER,
    data_type TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS bot_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    message TEXT,
    metadata TEXT,
    timestamp TEXT
);

-- Chainlink BTC/USD Data Stream via Polymarket RTDS
-- Settlement truth for BTC Up/Down 5-min markets
CREATE TABLE IF NOT EXISTS chainlink_btc_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'btc/usd',
    value_raw REAL,
    value_normalized REAL,
    source_timestamp_ms INTEGER,
    received_at_ms INTEGER,
    latency_ms INTEGER,
    topic TEXT,
    message_type TEXT,
    raw_payload TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chainlink_ticks_ts ON chainlink_btc_ticks(source_timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_chainlink_ticks_created ON chainlink_btc_ticks(created_at);

-- CLOB orderbook snapshots with full depth
CREATE TABLE IF NOT EXISTS clob_orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug TEXT,
    token_id TEXT,
    outcome_name TEXT,
    midpoint REAL,
    best_bid REAL,
    best_ask REAL,
    spread REAL,
    spread_pct REAL,
    bid_depth_top_1 REAL,
    ask_depth_top_1 REAL,
    bid_depth_top_3 REAL,
    ask_depth_top_3 REAL,
    total_bid_depth REAL,
    total_ask_depth REAL,
    last_trade_price REAL,
    last_trade_side TEXT,
    clob_timestamp_ms INTEGER,
    received_at_ms INTEGER,
    raw_book_payload TEXT,
    raw_midpoint_payload TEXT,
    raw_last_trade_payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_clob_snapshots_slug ON clob_orderbook_snapshots(market_slug);
CREATE INDEX IF NOT EXISTS idx_clob_snapshots_ts ON clob_orderbook_snapshots(clob_timestamp_ms);

-- Trade data snapshots (every signal/entry/exit/settlement)
CREATE TABLE IF NOT EXISTS trade_data_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT,
    event_type TEXT,
    market_slug TEXT,
    event_timestamp_ms INTEGER,
    binance_btc_price REAL,
    binance_event_timestamp_ms INTEGER,
    chainlink_btc_price REAL,
    chainlink_timestamp_ms INTEGER,
    yes_midpoint REAL,
    yes_best_bid REAL,
    yes_best_ask REAL,
    yes_spread REAL,
    yes_depth_top_3 REAL,
    no_midpoint REAL,
    no_best_bid REAL,
    no_best_ask REAL,
    no_spread REAL,
    no_depth_top_3 REAL,
    last_trade_price_yes REAL,
    last_trade_price_no REAL,
    seconds_to_close REAL,
    raw_context_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_trade_snaps_trade_id ON trade_data_snapshots(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_snaps_ts ON trade_data_snapshots(event_timestamp_ms);
