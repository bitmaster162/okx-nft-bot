-- PnL Engine Tables
CREATE TABLE collection_stats (
    collection_address TEXT PRIMARY KEY,
    collection_name TEXT,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    successful_trades INTEGER DEFAULT 0,
    fill_rate REAL DEFAULT 0,
    median_time_to_fill INTEGER,
    cancel_ratio REAL DEFAULT 0,
    pnl_per_live_offer REAL DEFAULT 0,
    pnl_per_usdt_exposure REAL DEFAULT 0,
    exposure_usdt REAL DEFAULT 0,
    inventory_age_hours REAL DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE offers (
    offer_id TEXT PRIMARY KEY,
    collection_address TEXT,
    token_id TEXT,
    maker_address TEXT,
    price REAL,
    currency TEXT,
    status TEXT,
    created_at TIMESTAMP,
    filled_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    pnl REAL,
    fill_time_seconds INTEGER,
    FOREIGN KEY (collection_address) REFERENCES collection_stats(collection_address)
);

CREATE TABLE counterparties (
    address TEXT PRIMARY KEY,
    nickname TEXT,
    total_trades INTEGER DEFAULT 0,
    successful_trades INTEGER DEFAULT 0,
    hit_rate REAL DEFAULT 0,
    avg_fill_time_seconds INTEGER,
    toxic_score REAL DEFAULT 0,
    last_seen TIMESTAMP
);

CREATE TABLE inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_address TEXT,
    token_id TEXT,
    purchase_price REAL,
    purchase_currency TEXT,
    purchase_time TIMESTAMP,
    current_list_price REAL,
    listing_time TIMESTAMP,
    age_hours REAL,
    exit_strategy TEXT,
    FOREIGN KEY (collection_address) REFERENCES collection_stats(collection_address)
);

CREATE TABLE policy_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_address TEXT,
    decision_type TEXT,
    input_params TEXT,
    output_decision TEXT,
    expected_pnl REAL,
    actual_pnl REAL,
    confidence_score REAL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for performance
CREATE INDEX idx_offers_collection ON offers(collection_address);
CREATE INDEX idx_offers_status ON offers(status);
CREATE INDEX idx_inventory_collection ON inventory(collection_address);
CREATE INDEX idx_inventory_age ON inventory(age_hours);
