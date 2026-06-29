"""Tests for the token-bucket rate limiter (deterministic; no real sleeping)."""

import pytest

from trader.schwab.rate_limit import TokenBucket


class _FakeTime:
    """A controllable monotonic clock whose sleep() advances the clock."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.t += dt

    def advance(self, dt: float) -> None:
        self.t += dt


def _bucket(rate: int = 60) -> tuple[TokenBucket, _FakeTime]:
    ft = _FakeTime()
    return TokenBucket(rate, monotonic=ft.monotonic, sleep=ft.sleep), ft


def test_rejects_nonpositive_rate() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_burst_up_to_capacity_without_sleeping() -> None:
    bucket, ft = _bucket(60)
    for _ in range(60):  # full bucket → no throttling, clock not advanced
        assert bucket.acquire() == 0.0
    assert ft.sleeps == []


def test_throttles_when_empty() -> None:
    bucket, ft = _bucket(60)  # 1 token/sec
    for _ in range(60):
        bucket.acquire()
    waited = bucket.acquire()  # bucket empty → must wait ~1s (60/rate)
    assert waited == pytest.approx(1.0)
    assert ft.sleeps[-1] == pytest.approx(1.0)


def test_refill_over_time() -> None:
    bucket, ft = _bucket(60)
    for _ in range(60):
        bucket.acquire()  # drain
    ft.advance(30.0)  # 30s → ~30 tokens replenish
    assert bucket.acquire() == 0.0  # no wait
    assert bucket.tokens == pytest.approx(29.0, abs=0.5)


def test_capacity_is_not_exceeded_on_refill() -> None:
    bucket, ft = _bucket(60)
    ft.advance(10_000.0)  # huge gap
    bucket.acquire()
    assert bucket.tokens <= 60.0
