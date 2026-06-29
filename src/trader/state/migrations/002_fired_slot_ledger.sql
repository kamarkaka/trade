-- Fired-slot ledger (design §7.5 / §12): exactly-once per (date, strategy, slot).
-- The UNIQUE constraint is the real guarantee — a duplicate claim fails the INSERT.
-- planned_fire_ts is ISO-8601 UTC; realized drift_seconds + seed are kept for replay.

CREATE TABLE fired_slot (
    slot_date       TEXT NOT NULL,                  -- ISO date (session day)
    strategy_id     TEXT NOT NULL,
    slot_id         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('claimed', 'done', 'failed')),
    planned_fire_ts TEXT,                           -- ISO-8601 UTC
    drift_seconds   INTEGER,
    seed            INTEGER,
    claimed_at      TEXT,                           -- ISO-8601 UTC
    finished_at     TEXT,                           -- ISO-8601 UTC
    error           TEXT,
    UNIQUE(slot_date, strategy_id, slot_id)
);
