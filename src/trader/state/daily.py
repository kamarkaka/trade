"""Persisted per-session counters -> the risk gate's real DayState (design §10/§12).

The account-wide daily rails (daily-loss limit, max trades per day) need facts that
survive restarts, measured per EXCHANGE session (the exchange-tz calendar date, not UTC):

- **start-of-day equity** — captured at the first observation of each session and persisted
  in ``daily_counters``, so a restart mid-session keeps the morning's value;
- **trades today** — orders sent this session, counted from the durable ``orders`` table:
  every write-ahead row except the definitely-not-placed ones (an order whose outcome is
  unknown counts — conservative);
- **loss today** — start-of-day equity minus current equity, floored at zero. It is
  mark-to-market, so it includes realized and unrealized P&L; the split is not tracked
  per session (``DayState.realized_pnl`` / ``unrealized_pnl`` stay 0).

Every read refreshes the ``daily_counters`` row so the read-only web UI shows current values.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader.core import Account, DayState

_NOT_PLACED = "not_placed"  # execution.idempotency.NOT_PLACED (kept local: no import cycle)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DailyCounters:
    """Builds the gate's ``DayState`` from durable state (orders + daily_counters)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        tz: ZoneInfo,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._conn = conn
        self._tz = tz
        self._now = now

    def session_of(self, at: datetime) -> date:
        return at.astimezone(self._tz).date()

    def day_state(
        self, account: Account, at: datetime, kill_switch_engaged: bool = False
    ) -> DayState:
        session = self.session_of(at)
        start_equity = self._start_of_day_equity(session, account.equity)
        trades = self.trades_on(session)
        loss = max(Decimal(0), start_equity - account.equity)
        self._conn.execute(
            "UPDATE daily_counters SET trades_today = ?, loss_today = ?, updated_at = ? "
            "WHERE trading_date = ?",
            (trades, str(loss), self._now().astimezone(UTC).isoformat(), session.isoformat()),
        )
        return DayState(
            trading_date=session,
            start_of_day_equity=start_equity,
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            trades_today=trades,
            loss_today=loss,
            kill_switch_engaged=kill_switch_engaged,
        )

    def trades_on(self, session: date) -> int:
        """Orders written this session (exchange tz), excluding definitely-not-placed."""
        start = datetime.combine(session, time(0), tzinfo=self._tz).astimezone(UTC)
        end = datetime.combine(session + timedelta(days=1), time(0), tzinfo=self._tz)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at >= ? AND created_at < ? AND status != ?",
            (start.isoformat(), end.astimezone(UTC).isoformat(), _NOT_PLACED),
        ).fetchone()
        return int(row[0])

    def _start_of_day_equity(self, session: date, equity: Decimal) -> Decimal:
        key = session.isoformat()
        ts = self._now().astimezone(UTC).isoformat()
        # First observation of the session wins; later ones (and restarts) keep it.
        self._conn.execute(
            "INSERT OR IGNORE INTO daily_counters "
            "(trading_date, trades_today, loss_today, start_of_day_equity, updated_at) "
            "VALUES (?, 0, '0', ?, ?)",
            (key, str(equity), ts),
        )
        self._conn.execute(
            "UPDATE daily_counters SET start_of_day_equity = ? "
            "WHERE trading_date = ? AND start_of_day_equity IS NULL",
            (str(equity), key),
        )
        row = self._conn.execute(
            "SELECT start_of_day_equity FROM daily_counters WHERE trading_date = ?", (key,)
        ).fetchone()
        return Decimal(row[0])


__all__ = ["DailyCounters"]
