"""Tests for the XNYS TradingCalendar wrapper: sessions, half-days, DST, and the
resolve_fire clamp/skip gate (M3.3). Uses fixed historical dates."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.core.enums import OnOvershoot
from trader.core.types import SlotSpec
from trader.scheduler.calendar import TradingCalendar

ET = ZoneInfo("America/New_York")


def _cal(extra: frozenset[date] = frozenset()) -> TradingCalendar:
    return TradingCalendar(extra_closures=extra)


def _slot(at: time, on_overshoot: OnOvershoot = OnOvershoot.CLAMP) -> SlotSpec:
    return SlotSpec(slot_id="s", at=at, drift_max_minutes=0, on_overshoot=on_overshoot)


def test_weekend_holiday_not_session() -> None:
    cal = _cal()
    assert cal.is_session(date(2024, 12, 28)) is False  # Saturday
    assert cal.is_session(date(2024, 12, 25)) is False  # Christmas
    assert cal.is_session(date(2024, 12, 24)) is True  # a regular session


def test_extra_closure() -> None:
    cal = _cal(frozenset({date(2024, 7, 5)}))
    assert cal.is_session(date(2024, 7, 5)) is False  # normally open, forced closed
    assert date(2024, 7, 5) not in cal.sessions(date(2024, 7, 1), date(2024, 7, 8))


def test_half_day_close() -> None:
    assert _cal().session_close(date(2024, 7, 3)) == datetime(2024, 7, 3, 13, 0, tzinfo=ET)


def test_dst_open_close_stable() -> None:
    cal = _cal()
    for d in (date(2024, 3, 11), date(2024, 11, 4)):  # day after spring-forward / fall-back
        assert cal.session_open(d) == datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
        assert cal.session_close(d) == datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)


def test_resolve_fire_clamps_overshoot() -> None:
    cal = _cal()
    fire = cal.localize(date(2024, 7, 3), time(15, 30))  # past the 13:00 half-day close
    resolved = cal.resolve_fire(fire, _slot(time(15, 30), OnOvershoot.CLAMP))
    assert resolved == datetime(2024, 7, 3, 12, 59, 59, tzinfo=ET)


def test_resolve_fire_skip_overshoot() -> None:
    cal = _cal()
    fire = cal.localize(date(2024, 7, 3), time(15, 30))
    assert cal.resolve_fire(fire, _slot(time(15, 30), OnOvershoot.SKIP)) is None


def test_resolve_fire_skip_on_closed() -> None:
    cal = _cal()
    fire = cal.localize(date(2024, 12, 25), time(10, 0))
    assert cal.resolve_fire(fire, _slot(time(10, 0))) is None


def test_resolve_fire_within_window_unchanged() -> None:
    cal = _cal()
    fire = cal.localize(date(2024, 3, 11), time(10, 0))  # inside 09:30-16:00
    assert cal.resolve_fire(fire, _slot(time(10, 0))) == fire


def test_resolve_fire_exactly_at_close() -> None:
    cal = _cal()
    close = cal.session_close(date(2024, 3, 11))
    assert cal.resolve_fire(close, _slot(time(16, 0))) == close  # inclusive of close


def test_resolve_fire_exactly_at_open() -> None:
    cal = _cal()
    open_at = cal.session_open(date(2024, 3, 11))
    assert cal.resolve_fire(open_at, _slot(time(9, 30))) == open_at  # inclusive of open


def test_resolve_fire_before_open_clamps() -> None:
    cal = _cal()
    fire = cal.localize(date(2024, 3, 11), time(9, 0))  # before the 09:30 open
    resolved = cal.resolve_fire(fire, _slot(time(9, 0)))
    assert resolved == cal.session_open(date(2024, 3, 11)) + timedelta(seconds=1)


def test_is_open() -> None:
    cal = _cal()
    assert cal.is_open(cal.localize(date(2024, 3, 11), time(10, 0))) is True
    assert cal.is_open(cal.localize(date(2024, 3, 11), time(8, 0))) is False  # pre-open
    assert cal.is_open(cal.localize(date(2024, 12, 25), time(10, 0))) is False  # holiday


def test_sessions_range_is_end_inclusive() -> None:
    sessions = _cal().sessions(date(2024, 7, 1), date(2024, 7, 8))
    assert date(2024, 7, 8) in sessions  # end-inclusive
    assert date(2024, 7, 1) in sessions


def test_resolve_fire_utc_input_maps_to_correct_session() -> None:
    # a fire given in UTC late at night maps to the prior ET session and clamps there
    cal = _cal()
    fire = datetime(2024, 7, 3, 2, 0, tzinfo=UTC)  # = 2024-07-02 22:00 ET (after close)
    resolved = cal.resolve_fire(fire, _slot(time(15, 30), OnOvershoot.CLAMP))
    assert resolved is not None
    assert resolved.astimezone(ET).date() == date(2024, 7, 2)  # prior session, not 07-03


def test_localize_handles_dst_gap_and_fold() -> None:
    cal = _cal()
    gap = cal.localize(date(2024, 3, 10), time(2, 30))  # spring-forward gap (doesn't exist)
    fold = cal.localize(date(2024, 11, 3), time(1, 30))  # fall-back fold (happens twice)
    # both resolve deterministically to a well-defined UTC instant (PEP 495 fold=0)
    assert gap == datetime(2024, 3, 10, 7, 30, tzinfo=UTC)
    assert fold == datetime(2024, 11, 3, 5, 30, tzinfo=UTC)
