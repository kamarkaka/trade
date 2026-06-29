"""Token-bucket rate limiter for the Schwab HTTP transport (design §8.6).

Throttles outbound requests to stay under the per-minute ceiling. The monotonic
clock and sleep function are injected so tests are deterministic (no wall-clock
sleeping); production uses ``time.monotonic`` / ``time.sleep``. Pure and
httpx-agnostic so it is reusable by the broker client (M5).
"""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    """A simple token bucket: ``rate_per_min`` tokens/min, bursts up to capacity."""

    def __init__(
        self,
        rate_per_min: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be positive")
        self.capacity = float(rate_per_min)
        self.refill_per_sec = rate_per_min / 60.0
        self._tokens = float(rate_per_min)  # start full (allow an initial burst)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()

    def _refill(self) -> None:
        now = self._monotonic()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_sec)

    def acquire(self) -> float:
        """Consume one token, sleeping if the bucket is empty. Returns seconds waited."""
        self._refill()
        waited = 0.0
        if self._tokens < 1.0:
            waited = (1.0 - self._tokens) / self.refill_per_sec
            self._sleep(waited)
            self._refill()  # the (injected) sleep advances the clock → tokens replenish
        self._tokens -= 1.0
        return waited

    @property
    def tokens(self) -> float:
        return self._tokens
