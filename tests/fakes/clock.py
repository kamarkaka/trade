"""A deterministic Clock test double."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# A fixed, market-hours weekday default (2026-01-02 is a Friday, 15:00 UTC ~ 10:00 ET).
_DEFAULT_NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)


class FakeClock:
    """Implements ``trader.core.protocols.Clock`` with controllable time."""

    def __init__(self, now: datetime | None = None, *, market_open: bool = True) -> None:
        self._now = now or _DEFAULT_NOW
        self._market_open = market_open

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, at: datetime | None = None) -> bool:
        return self._market_open

    # --- test controls ---
    def set(self, instant: datetime) -> None:
        self._now = instant

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set_market_open(self, is_open: bool) -> None:
        self._market_open = is_open
