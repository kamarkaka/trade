"""Fired-slot ledger — exactly-once per (slot_date, strategy_id, slot_id) (design §7.5).

A durable claim/do/done repository over SQLite. The UNIQUE constraint (not the
scheduler) is the real exactly-once guarantee: ``claim`` does an INSERT under
``BEGIN IMMEDIATE`` and returns ``False`` if the row already exists, so a crash or a
double-schedule can never fire the same slot twice.

Crash-recovery policy (the orphaned-'claimed' case): a row left ``claimed`` by a
crash mid-cycle stays claimed and continues to BLOCK re-fire — we never auto-reopen
it, because the original cycle may have actually placed orders (re-firing could
double-trade). ``stale_claims`` surfaces such rows so the daemon can ALERT an
operator (M3.11) rather than silently retry.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StaleClaim:
    """An orphaned 'claimed' row (claimed but never finished) past the grace window."""

    slot_date: str
    strategy_id: str
    slot_id: str
    claimed_at: str


class FiredSlotLedger:
    """Crash-safe exactly-once ledger for scheduled slot fires."""

    def __init__(self, conn: sqlite3.Connection, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._conn = conn
        self._now = now

    def claim(
        self,
        slot_date: date,
        strategy_id: str,
        slot_id: str,
        planned_fire_ts: datetime,
        drift_seconds: int,
        seed: int | None,
    ) -> bool:
        """Atomically claim a slot. Returns False if already claimed/done/failed."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO fired_slot (slot_date, strategy_id, slot_id, status, "
                "planned_fire_ts, drift_seconds, seed, claimed_at) "
                "VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?)",
                (
                    slot_date.isoformat(),
                    strategy_id,
                    slot_id,
                    planned_fire_ts.isoformat(),
                    drift_seconds,
                    seed,
                    self._now().isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK")
            return False
        self._conn.execute("COMMIT")
        return True

    def mark_done(self, slot_date: date, strategy_id: str, slot_id: str) -> None:
        self._set_status(slot_date, strategy_id, slot_id, "done", error=None)

    def mark_failed(self, slot_date: date, strategy_id: str, slot_id: str, error: str) -> None:
        self._set_status(slot_date, strategy_id, slot_id, "failed", error=error)

    def was_fired(self, slot_date: date, strategy_id: str, slot_id: str) -> str | None:
        """Return the slot's status ('claimed'/'done'/'failed') or None if never claimed."""
        row = self._conn.execute(
            "SELECT status FROM fired_slot WHERE slot_date = ? AND strategy_id = ? AND slot_id = ?",
            (slot_date.isoformat(), strategy_id, slot_id),
        ).fetchone()
        return cast("str", row[0]) if row is not None else None

    def stale_claims(self, grace_seconds: int) -> list[StaleClaim]:
        """Orphaned 'claimed' rows older than ``grace_seconds`` (for alerting)."""
        cutoff = (self._now() - timedelta(seconds=grace_seconds)).isoformat()
        rows = self._conn.execute(
            "SELECT slot_date, strategy_id, slot_id, claimed_at FROM fired_slot "
            "WHERE status = 'claimed' AND claimed_at < ?",
            (cutoff,),
        ).fetchall()
        return [StaleClaim(*row) for row in rows]

    def _set_status(
        self, slot_date: date, strategy_id: str, slot_id: str, status: str, *, error: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE fired_slot SET status = ?, finished_at = ?, error = ? "
            "WHERE slot_date = ? AND strategy_id = ? AND slot_id = ?",
            (status, self._now().isoformat(), error, slot_date.isoformat(), strategy_id, slot_id),
        )
