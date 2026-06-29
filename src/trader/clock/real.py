"""RealClock — the wall-clock ``Clock`` used for live trading (design §5, M2.1).

``now()`` returns tz-aware UTC; ``monotonic()`` is for measuring intervals (it is
immune to wall-clock jumps). ``is_market_open`` delegates to an injected calendar
predicate; until the trading calendar lands (M3) it defaults to always-open. The
RealClock is never used in backtests — the engine injects a VirtualClock there.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

MarketPredicate = Callable[[datetime], bool]


class RealClock:
    """Wall-clock time source for live trading."""

    def __init__(self, *, is_open: MarketPredicate | None = None) -> None:
        self._is_open = is_open

    def now(self) -> datetime:
        """Current instant as a tz-aware UTC datetime."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Monotonic seconds for interval timing (not wall-clock; never goes back)."""
        return time.monotonic()

    def is_market_open(self, at: datetime | None = None) -> bool:
        moment = at if at is not None else self.now()
        if self._is_open is None:
            # Until the trading calendar is wired (M3), assume the market is open.
            return True
        return self._is_open(moment)
