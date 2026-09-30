-- Order-row integrity for idempotent placement (LR3).
-- 1) A broker order id belongs to exactly one local order: adopting an id that is already
--    bound to another order during reconciliation must fail loudly rather than attribute
--    one broker order (and its fills) to two intents.
CREATE UNIQUE INDEX idx_orders_broker_order_id ON orders (broker_order_id)
    WHERE broker_order_id IS NOT NULL;

-- 2) Optimistic-concurrency version, bumped by every order-state write: resolution is a
--    compare-and-swap on it (never on a re-serialized timestamp).
ALTER TABLE orders ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
