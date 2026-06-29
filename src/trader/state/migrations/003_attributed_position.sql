-- Per-strategy attributed sub-positions (design §10 #16: strategies keep strictly
-- separate sub-positions). quantity is signed; avg_price is a Decimal string. The
-- special strategy_id 'unknown' parks any broker delta not attributable to a strategy.

CREATE TABLE attributed_position (
    strategy_id TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    quantity    INTEGER NOT NULL,   -- signed (negative = short)
    avg_price   TEXT NOT NULL,      -- Decimal string
    PRIMARY KEY (strategy_id, symbol)
);
