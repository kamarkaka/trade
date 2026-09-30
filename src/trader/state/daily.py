"""Persisted per-session counters -> the risk gate's real DayState (design §10/§12).

The account-wide daily rails (daily-loss limit, max trades per day) need facts measured per
EXCHANGE session (the exchange-tz calendar date, not UTC):

- **start-of-day equity** — the broker's own start-of-session figure when it reports one
  (``Account.start_of_day_equity``: it includes the opening move a later first cycle would
  miss); otherwise captured at the first cycle of each session (every cycle calls in,
  orders or not). Either way it is persisted in ``daily_counters``;
- **trades today** — orders sent this session, counted from the durable ``orders`` table:
  every write-ahead row except the definitely-not-placed ones (an order whose outcome is
  unknown counts — conservative);
- **loss today** — start-of-day equity minus current equity, floored at zero. It is
  mark-to-market, so it includes realized and unrealized P&L; the split is not tracked
  per session (``DayState.realized_pnl`` / ``unrealized_pnl`` stay 0).

Counters are keyed by (session, **scope**) — the equity source they measure. Live uses the
constant scope ``live`` (its state database serves one account), so the start-of-day equity
survives restarts; paper uses one scope per process, because its SimBroker restarts flat (a
persisted paper start-of-day equity would read a restart as a loss). A non-positive equity
reading is refused (fail closed: the cycle errors and alerts, and no order is sent) rather
than recorded or compared — it would otherwise disable the loss rail for the day or trip it
spuriously. A non-positive broker start-of-day figure is ignored (the capture is used).

It also supplies the pattern-day-trader inputs over the rolling window of ``pdt_window_days``
exchange sessions ending today (the first session comes from the injected exchange calendar):
every order that may have executed — all but the definitely-not-placed and the terminal ones
that filled nothing, so an order of unknown or unresolved fate counts — as (symbol, side,
the session it was sent in), plus the broker's own day-trade count when it reports one.

Every read refreshes the ``daily_counters`` row so the read-only web UI shows current values.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trader.core import Account, DayState
from trader.core.enums import Side

# execution.idempotency.NOT_PLACED / TERMINAL (kept local so state/ doesn't import
# execution/; a test pins them together).
_NOT_PLACED = "not_placed"
_TERMINAL = frozenset({"FILLED", "CANCELED", "REJECTED", "EXPIRED"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DailyCounters:
    """Builds the gate's ``DayState`` from durable state (orders + daily_counters)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        tz: ZoneInfo,
        scope: str,
        now: Callable[[], datetime] = _utcnow,
        sessions: Callable[[date, date], Sequence[date]] | None = None,
        pdt_window_days: int = 5,
    ) -> None:
        if not scope:
            raise ValueError("a counters scope (the equity source) is required")
        if pdt_window_days < 1:
            raise ValueError("pdt_window_days must be at least 1")
        self._conn = conn
        self._tz = tz
        self._scope = scope
        self._now = now
        self._sessions = sessions  # None => no PDT inputs (the rule is not evaluated)
        self._pdt_window_days = pdt_window_days

    def session_of(self, at: datetime) -> date:
        return at.astimezone(self._tz).date()

    def day_state(
        self, account: Account, at: datetime, kill_switch_engaged: bool = False
    ) -> DayState:
        if account.equity <= 0:
            raise ValueError(
                f"implausible account equity {account.equity}; refusing to evaluate the "
                "daily rails (fail closed)"
            )
        session = self.session_of(at)
        start_equity = self._start_of_day_equity(session, account)
        trades = self.trades_on(session)
        loss = max(Decimal(0), start_equity - account.equity)
        window_start = self.pdt_window_start(session)
        executions = self.executions_since(window_start) if window_start is not None else ()
        self._conn.execute(
            "UPDATE daily_counters SET trades_today = ?, loss_today = ?, updated_at = ? "
            "WHERE trading_date = ? AND scope = ?",
            (trades, str(loss), self._stamp(), session.isoformat(), self._scope),
        )
        return DayState(
            trading_date=session,
            start_of_day_equity=start_equity,
            realized_pnl=Decimal(0),
            unrealized_pnl=Decimal(0),
            trades_today=trades,
            loss_today=loss,
            kill_switch_engaged=kill_switch_engaged,
            executions=executions,
            pdt_window_start=window_start,
            broker_day_trades=account.round_trips,
        )

    def pdt_window_start(self, session: date) -> date | None:
        """First exchange session of the rolling PDT window ending at ``session``."""
        if self._sessions is None:
            return None
        lookback = session - timedelta(days=self._pdt_window_days * 2 + 10)  # covers holidays
        recent = [d for d in self._sessions(lookback, session) if d <= session]
        if not recent:
            return session
        return recent[-self._pdt_window_days] if len(recent) >= self._pdt_window_days else recent[0]

    def executions_since(self, window_start: date) -> tuple[tuple[str, Side, date], ...]:
        """(symbol, side, session) of every order sent since the window start that may have
        executed: all but the definitely-not-placed and the terminal ones with no fill row
        (nothing filled). Bucketed by the session the order was sent in — a DAY order
        executes in it — not by when its fill was recorded."""
        start = datetime.combine(window_start, time(0), tzinfo=self._tz)
        terminal = sorted(_TERMINAL)
        rows = self._conn.execute(
            "SELECT o.symbol, o.side, o.created_at FROM orders o "
            "WHERE julianday(o.created_at) >= julianday(?) AND o.status != ? "
            f"AND (o.status NOT IN ({', '.join('?' * len(terminal))}) OR EXISTS ("
            "SELECT 1 FROM fills f WHERE f.client_order_id = o.client_order_id "
            "AND f.quantity > 0)) ORDER BY julianday(o.created_at)",
            (start.isoformat(), _NOT_PLACED, *terminal),
        ).fetchall()
        return tuple(
            (str(sym), Side(side), self.session_of(datetime.fromisoformat(created)))
            for sym, side, created in rows
        )

    def trades_on(self, session: date) -> int:
        """Orders written this session (exchange tz), excluding definitely-not-placed.
        Compared as instants (julianday), so any stored UTC offset buckets correctly."""
        start = datetime.combine(session, time(0), tzinfo=self._tz)
        end = datetime.combine(session + timedelta(days=1), time(0), tzinfo=self._tz)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM orders WHERE julianday(created_at) >= julianday(?) "
            "AND julianday(created_at) < julianday(?) AND status != ?",
            (start.isoformat(), end.isoformat(), _NOT_PLACED),
        ).fetchone()
        return int(row[0])

    def _start_of_day_equity(self, session: date, account: Account) -> Decimal:
        key = (session.isoformat(), self._scope)
        broker_sod = account.start_of_day_equity
        if broker_sod is not None and broker_sod > 0:
            self._conn.execute(
                "INSERT INTO daily_counters "
                "(trading_date, scope, trades_today, loss_today, start_of_day_equity, updated_at) "
                "VALUES (?, ?, 0, '0', ?, ?) ON CONFLICT(trading_date, scope) "
                "DO UPDATE SET start_of_day_equity = excluded.start_of_day_equity",
                (*key, str(broker_sod), self._stamp()),
            )
            return broker_sod
        equity = account.equity
        # First observation of the session wins; later ones (and restarts) keep it.
        self._conn.execute(
            "INSERT OR IGNORE INTO daily_counters "
            "(trading_date, scope, trades_today, loss_today, start_of_day_equity, updated_at) "
            "VALUES (?, ?, 0, '0', ?, ?)",
            (*key, str(equity), self._stamp()),
        )
        self._conn.execute(
            "UPDATE daily_counters SET start_of_day_equity = ? "
            "WHERE trading_date = ? AND scope = ? AND start_of_day_equity IS NULL",
            (str(equity), *key),
        )
        row = self._conn.execute(
            "SELECT start_of_day_equity FROM daily_counters WHERE trading_date = ? AND scope = ?",
            key,
        ).fetchone()
        return Decimal(row[0])

    def _stamp(self) -> str:
        return self._now().astimezone(UTC).isoformat()


__all__ = ["DailyCounters"]
