-- The session in which the daily-loss rail last auto-engaged the kill switch (LR7). It trips
-- at most once per session, so an operator's release sticks: the gate's daily-loss rule
-- still refuses new entries for the rest of the session, while exits can go through.
ALTER TABLE kill_switch ADD COLUMN loss_trip_session TEXT;
