"""Persisted kill switch (design §10).

A durable, single-row flag (the ``kill_switch`` table from M0.7) that halts all new orders.
It is **persisted** so a trip survives restarts, **checked at the start of every cycle and
immediately before every submit** (the gate's ``kill_switch`` rule enforces the pre-submit
check), flippable by the operator (``trader kill --on/--off``), and **auto-trips** on
dangerous conditions (daily-loss breach, repeated broker errors, reconciliation mismatch,
stale data). On a trip it halts new orders and alerts; **auto-flatten is OFF by default**
(forcing exits in a disorderly market is itself risky), so existing positions are left as-is.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import DayState
from trader.observability.alerting import Alerter, AlertEvent, AlertKind
from trader.observability.logging import get_logger


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    reason: str | None
    source: str | None
    updated_at: str | None


class KillSwitch:
    """Read/flip the persisted kill switch (singleton row id=1)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now: Callable[[], datetime] = _utcnow,
        alerter: Alerter | None = None,
    ) -> None:
        self._conn = conn
        self._now = now
        self._alerter = alerter
        self._log = get_logger("kill_switch")

    def state(self) -> KillSwitchState:
        row = self._conn.execute(
            "SELECT engaged, reason, source, updated_at FROM kill_switch WHERE id = 1"
        ).fetchone()
        if row is None:
            return KillSwitchState(False, None, None, None)
        return KillSwitchState(bool(row[0]), row[1], row[2], row[3])

    def is_engaged(self) -> bool:
        return self.state().engaged

    def engage(self, reason: str, source: str = "manual") -> bool:
        """Engage the kill switch. Returns True if this call newly tripped it (idempotent:
        a second engage doesn't re-alert). Halts new orders + alerts on the trip."""
        if self.is_engaged():
            return False
        self._write(engaged=True, reason=reason, source=source)
        self._log.warning("KILL SWITCH ENGAGED", reason=reason, source=source)
        if self._alerter is not None:
            self._alerter.alert(
                AlertEvent(AlertKind.KILL_SWITCH, f"kill switch engaged ({source}): {reason}")
            )
        return True

    def disengage(self, source: str = "manual") -> None:
        self._write(engaged=False, reason=None, source=source)
        self._log.warning("kill switch released", source=source)

    def maybe_trip_on_daily_loss(self, day_state: DayState, config: RiskConfig) -> bool:
        """Auto-trip if the day's loss has breached the configured limit. Returns True if it
        tripped on this call."""
        limit = (
            day_state.start_of_day_equity * Decimal(str(config.daily_loss_limit_pct)) / Decimal(100)
        )
        if day_state.loss_today >= limit:
            return self.engage(
                f"daily loss {day_state.loss_today} reached limit {limit}", source="auto"
            )
        return False

    def _write(self, *, engaged: bool, reason: str | None, source: str) -> None:
        self._conn.execute(
            "INSERT INTO kill_switch (id, engaged, reason, source, updated_at) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "engaged = excluded.engaged, reason = excluded.reason, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (1 if engaged else 0, reason, source, self._now().astimezone(UTC).isoformat()),
        )


__all__ = ["KillSwitch", "KillSwitchState"]
