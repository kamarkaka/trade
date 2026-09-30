-- A broker order id belongs to exactly one local order (LR3). Adopting an id that is
-- already bound to another order during reconciliation must fail loudly rather than
-- attribute one broker order (and its fills) to two intents.
CREATE UNIQUE INDEX idx_orders_broker_order_id ON orders (broker_order_id)
    WHERE broker_order_id IS NOT NULL;
