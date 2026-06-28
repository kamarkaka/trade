-- Initial durable schema (design §12). Money/timestamps are stored as TEXT
-- (Decimal string / ISO-8601 UTC) to avoid binary-float and timezone loss.
-- Per-milestone tables (fired-slot ledger, attributed_position, tokens, ...) are
-- added by their own later migrations.

CREATE TABLE orders (
    client_order_id TEXT PRIMARY KEY,            -- idempotency key
    strategy_id     TEXT NOT NULL,               -- attribution
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     TEXT,
    tif             TEXT NOT NULL,
    status          TEXT NOT NULL,               -- WORKING/FILLED/PARTIAL_FILL/...
    broker_order_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES orders (client_order_id),
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    price           TEXT NOT NULL,
    fees            TEXT NOT NULL,
    ts              TEXT NOT NULL,
    status          TEXT NOT NULL
);
CREATE INDEX idx_fills_client_order_id ON fills (client_order_id);

CREATE TABLE positions (
    symbol       TEXT PRIMARY KEY,
    quantity     INTEGER NOT NULL,               -- signed; negative = short
    avg_price    TEXT NOT NULL,
    market_value TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE equity_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    equity         TEXT NOT NULL,
    cash           TEXT NOT NULL,
    realized_pnl   TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL
);

-- Per-cycle audit trail: inputs -> decision -> risk verdict -> order -> fill.
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    cycle_id    TEXT,
    strategy_id TEXT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL                    -- JSON
);
CREATE INDEX idx_audit_log_ts ON audit_log (ts);

CREATE TABLE daily_counters (
    trading_date        TEXT PRIMARY KEY,        -- ISO date
    trades_today        INTEGER NOT NULL DEFAULT 0,
    loss_today          TEXT NOT NULL DEFAULT '0',
    start_of_day_equity TEXT,
    updated_at          TEXT NOT NULL
);

-- Single-row kill switch (persisted so it survives restarts; design §10).
CREATE TABLE kill_switch (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    engaged    INTEGER NOT NULL DEFAULT 0,       -- 0/1
    reason     TEXT,
    source     TEXT,
    updated_at TEXT NOT NULL
);
