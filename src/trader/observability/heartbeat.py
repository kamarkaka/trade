"""Liveness heartbeat (design §16.1).

The daemon ``touch``es a singleton row each scheduler tick. ``trader status
--healthcheck`` (the Docker HEALTHCHECK) and ``check`` read it back and compare the
age to ``clock.now()``: a fresh beat is alive (exit 0), a stale or missing beat is dead
(non-zero) and fires a CRASH alert — silent-death detection.

The clock is injected so freshness is deterministic in tests; reads are defensive (a
missing DB/table reads as "not alive" rather than raising) so the healthcheck never
crashes the container probe.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from trader.core.protocols import Clock
from trader.observability.alerting import Alerter, AlertEvent, AlertKind
from trader.observability.logging import get_logger


@dataclass(frozen=True)
class HeartbeatRecord:
    last_alive_at: datetime
    scheduler_state: str
    detail: str | None = None


class Heartbeat:
    """Read/write the singleton liveness heartbeat."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Clock,
        max_age_seconds: float,
        alerter: Alerter | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._max_age = max_age_seconds
        self._alert = alerter
        self._log = get_logger("heartbeat")

    def touch(self, scheduler_state: str = "running", detail: str | None = None) -> None:
        """Record that the daemon is alive right now (upsert the singleton row)."""
        ts = self._clock.now().astimezone(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO heartbeat (id, last_alive_at, scheduler_state, detail) "
            "VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "last_alive_at = excluded.last_alive_at, "
            "scheduler_state = excluded.scheduler_state, "
            "detail = excluded.detail",
            (ts, scheduler_state, detail),
        )

    def read(self) -> HeartbeatRecord | None:
        """Return the last heartbeat, or None if never beat / table absent (defensive)."""
        try:
            row = self._conn.execute(
                "SELECT last_alive_at, scheduler_state, detail FROM heartbeat WHERE id = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # no such table => never initialized => not alive
        if row is None:
            return None
        return HeartbeatRecord(datetime.fromisoformat(row[0]), row[1], row[2])

    def age_seconds(self) -> float | None:
        """Seconds since the last beat, or None if there is none."""
        record = self.read()
        if record is None:
            return None
        return (self._clock.now() - record.last_alive_at).total_seconds()

    def is_alive(self, max_age_seconds: float | None = None) -> bool:
        """True iff a beat exists and is within the freshness window."""
        age = self.age_seconds()
        if age is None:
            return False  # never beat (or unreadable) => not alive
        threshold = self._max_age if max_age_seconds is None else max_age_seconds
        return age <= threshold

    def check(self) -> bool:
        """Liveness check that ALERTS (CRASH) on a stale/missing beat — silent-death detection."""
        if self.is_alive():
            return True
        if self._alert is not None:
            age = self.age_seconds()
            detail = "no heartbeat recorded" if age is None else f"stale by {age:.0f}s"
            self._alert.alert(AlertEvent(AlertKind.CRASH, f"daemon heartbeat missed ({detail})"))
        return False


__all__ = ["Heartbeat", "HeartbeatRecord"]
