"""Tests for RealClock and VirtualClock (M2.1).

VirtualClock's forward-only contract is the backbone of no-lookahead, so it gets
the most scrutiny. RealClock is checked for tz-aware UTC + monotonic behavior.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from trader.clock import RealClock, VirtualClock
from trader.core.protocols import Clock

START = datetime(2026, 6, 28, 14, 30, tzinfo=UTC)


# --- protocol conformance --------------------------------------------------- #


def test_both_clocks_satisfy_protocol() -> None:
    # NOTE: @runtime_checkable isinstance only verifies method *names*; the full
    # signature/return-type conformance is enforced by mypy --strict.
    assert isinstance(RealClock(), Clock)
    assert isinstance(VirtualClock(START), Clock)


# --- RealClock -------------------------------------------------------------- #


def test_realclock_tz_aware() -> None:
    now = RealClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_real_clock_now_is_recent() -> None:
    before = datetime.now(UTC)
    now = RealClock().now()
    after = datetime.now(UTC)
    assert before <= now <= after


def test_real_clock_monotonic_non_decreasing() -> None:
    clock = RealClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second >= first


def test_real_clock_is_open_defaults_true() -> None:
    assert RealClock().is_market_open() is True


def test_real_clock_delegates_to_predicate() -> None:
    seen: list[datetime] = []

    def closed(at: datetime) -> bool:
        seen.append(at)
        return False

    clock = RealClock(is_open=closed)
    assert clock.is_market_open(START) is False
    assert seen == [START]


# --- VirtualClock ----------------------------------------------------------- #


def test_virtual_now_returns_start() -> None:
    assert VirtualClock(START).now() == START


def test_virtual_advance_to_moves_forward() -> None:
    clock = VirtualClock(START)
    later = START + timedelta(hours=1)
    clock.advance_to(later)
    assert clock.now() == later


def test_virtual_advance_to_same_instant_is_allowed() -> None:
    clock = VirtualClock(START)
    clock.advance_to(START)  # idempotent, not backward
    assert clock.now() == START


def test_virtual_advances_forward_only() -> None:
    clock = VirtualClock(START)
    # forward works
    clock.advance_to(START + timedelta(hours=1))
    # backward is rejected and leaves state unchanged
    with pytest.raises(ValueError, match="cannot move backward"):
        clock.advance_to(START)
    assert clock.now() == START + timedelta(hours=1)


def test_virtual_advance_by_delta() -> None:
    clock = VirtualClock(START)
    clock.advance(timedelta(minutes=5))
    assert clock.now() == START + timedelta(minutes=5)


def test_virtual_advance_negative_delta_raises() -> None:
    clock = VirtualClock(START)
    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(timedelta(seconds=-1))


def test_virtual_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        VirtualClock(datetime(2026, 6, 28, 14, 30))


def test_virtual_normalizes_non_utc_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    clock = VirtualClock(datetime(2026, 6, 28, 10, 30, tzinfo=eastern))
    assert clock.now() == START  # 10:30-04:00 == 14:30Z
    assert clock.now().utcoffset() == timedelta(0)


def test_virtual_advance_to_rejects_naive() -> None:
    clock = VirtualClock(START)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.advance_to(datetime(2026, 6, 28, 15, 0))


def test_virtual_is_open_defaults_true_and_delegates() -> None:
    assert VirtualClock(START).is_market_open() is True
    clock = VirtualClock(START, is_open=lambda at: at == START)
    assert clock.is_market_open() is True
    assert clock.is_market_open(START + timedelta(days=1)) is False
