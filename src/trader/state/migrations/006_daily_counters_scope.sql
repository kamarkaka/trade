-- Per-session counters are scoped by their equity source (LR6): paper's SimBroker restarts
-- flat, so each paper process gets its own scope, while live is scoped by the account (and
-- persists across restarts). Rebuild the table keyed by (trading_date, scope); existing rows
-- keep an empty scope.
CREATE TABLE daily_counters_v2 (
    trading_date        TEXT NOT NULL,           -- ISO date (exchange session)
    scope               TEXT NOT NULL DEFAULT '',
    trades_today        INTEGER NOT NULL DEFAULT 0,
    loss_today          TEXT NOT NULL DEFAULT '0',
    start_of_day_equity TEXT,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (trading_date, scope)
);
INSERT INTO daily_counters_v2
    (trading_date, scope, trades_today, loss_today, start_of_day_equity, updated_at)
    SELECT trading_date, '', trades_today, loss_today, start_of_day_equity, updated_at
    FROM daily_counters;
DROP TABLE daily_counters;
ALTER TABLE daily_counters_v2 RENAME TO daily_counters;
