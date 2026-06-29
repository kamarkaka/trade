"""VirtualClock — a controllable ``Clock`` for backtests (design §5, Appendix B).

This is the backbone of no-lookahead: the engine advances it to each trigger and
every market-data read is bound to ``now()``. Time moves **forward only** —
``advance_to`` rejects moving backward and ``advance`` rejects negative deltas — so
a backtest can never accidentally read the future or replay the past.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

MarketPredicate = Callable[[datetime], bool]


def _to_utc(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")
    return dt.astimezone(UTC)


class VirtualClock:
    """A clock whose current instant is set explicitly by the backtest engine."""

    def __init__(self, start: datetime, *, is_open: MarketPredicate | None = None) -> None:
        self._now = _to_utc(start, "start")
        self._is_open = is_open

    def now(self) -> datetime:
        """The current virtual instant (tz-aware UTC)."""
        return self._now

    def advance_to(self, ts: datetime) -> None:
        """Move the clock to ``ts``. Forward-only: moving backward is an error."""
        target = _to_utc(ts, "ts")
        if target < self._now:
            raise ValueError(
                f"VirtualClock cannot move backward: {target.isoformat()} < {self._now.isoformat()}"
            )
        self._now = target

    def advance(self, delta: timedelta) -> None:
        """Advance the clock by ``delta`` (must be non-negative)."""
        if delta < timedelta(0):
            raise ValueError(f"advance delta must be non-negative, got {delta!r}")
        self._now = self._now + delta

    def is_market_open(self, at: datetime | None = None) -> bool:
        moment = at if at is not None else self._now
        if self._is_open is None:
            return True
        return self._is_open(moment)
