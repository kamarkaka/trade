"""XNYS trading-calendar wrapper (design §7.3). A thin, deterministic layer over
``exchange_calendars`` exposing sessions, open/close (incl. half-days), an is-open
check, DST-correct localization, and the single ``resolve_fire`` clamp/skip gate
shared by backtest and live.

All open/close times are returned in the exchange timezone (ET). ``resolve_fire`` is
the one place a drifted fire time is gated against the session window, so live and
backtest behave identically. No wall-clock reads — fully deterministic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import exchange_calendars as ec
import pandas as pd

from trader.core.enums import OnOvershoot
from trader.core.types import SlotSpec

_EPSILON = timedelta(seconds=1)


class TradingCalendar:
    """Sessions, open/close, and the resolve_fire gate for one exchange.

    Precondition: queried dates must lie within the bundled ``exchange_calendars``
    horizon (it extends ~1 year past the install date). A date beyond it raises the
    library's out-of-bounds error rather than silently returning "closed" — a
    long-lived daemon must be redeployed (refreshing the calendar) before the horizon
    is reached; M3.11 surfaces such errors via alerting rather than halting silently.
    """

    def __init__(
        self,
        code: str = "XNYS",
        tz: str = "America/New_York",
        extra_closures: frozenset[date] = frozenset(),
    ) -> None:
        self._cal = ec.get_calendar(code)
        self._tz = ZoneInfo(tz)
        self._extra = extra_closures

    def is_session(self, d: date) -> bool:
        """True on a trading day (False on weekends/holidays/extra closures)."""
        if d in self._extra:
            return False
        return bool(self._cal.is_session(pd.Timestamp(d)))

    def sessions(self, start: date, end: date) -> list[date]:
        """Trading days in ``[start, end]`` (extra closures removed)."""
        raw = self._cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        return [ts.date() for ts in raw if ts.date() not in self._extra]

    def session_open(self, d: date) -> datetime:
        """Session open as a tz-aware ET datetime."""
        ts = self._cal.session_open(pd.Timestamp(d)).tz_convert(self._tz).to_pydatetime()
        return cast("datetime", ts)

    def session_close(self, d: date) -> datetime:
        """Session close as a tz-aware ET datetime (early close on half-days)."""
        ts = self._cal.session_close(pd.Timestamp(d)).tz_convert(self._tz).to_pydatetime()
        return cast("datetime", ts)

    def is_open(self, at: datetime) -> bool:
        """True if ``at`` falls within the session window (inclusive of open/close)."""
        d = at.astimezone(self._tz).date()
        if not self.is_session(d):
            return False
        return self.session_open(d) <= at <= self.session_close(d)

    def localize(self, d: date, t: time) -> datetime:
        """Combine a local date+time in the exchange tz and return the UTC instant.

        DST-safe: a spring-forward gap or fall-back fold still yields a well-defined
        UTC instant (zoneinfo resolves the offset deterministically)."""
        return datetime.combine(d, t, tzinfo=self._tz).astimezone(UTC)

    def resolve_fire(self, fire_ts: datetime, slot: SlotSpec) -> datetime | None:
        """Gate a drifted fire time against the session window.

        Returns ``None`` to skip (closed session, or a late overshoot when
        ``on_overshoot=SKIP``); otherwise a fire time inside the window: a late
        overshoot clamps to just before close, an early one to just after open.
        """
        d = fire_ts.astimezone(self._tz).date()
        if not self.is_session(d):
            return None
        open_at = self.session_open(d)
        close_at = self.session_close(d)
        if fire_ts > close_at:
            return close_at - _EPSILON if slot.on_overshoot is OnOvershoot.CLAMP else None
        if fire_ts < open_at:
            return open_at + _EPSILON
        return fire_ts
