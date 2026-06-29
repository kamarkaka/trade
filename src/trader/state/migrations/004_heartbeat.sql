-- Liveness heartbeat (design §16.1): a singleton row the daemon touches each tick so
-- `trader status --healthcheck` (the Docker HEALTHCHECK) and the alerter can detect a
-- silent death. last_alive_at is ISO-8601 UTC; scheduler_state is informational.

CREATE TABLE heartbeat (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
    last_alive_at   TEXT NOT NULL,                       -- ISO-8601 UTC
    scheduler_state TEXT NOT NULL DEFAULT 'unknown',     -- e.g. running | stopped
    detail          TEXT                                 -- optional (e.g. job count)
);
