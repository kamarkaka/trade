"""Tests for SlotScheduler: merged sort, stable tie-break, disabled/holiday handling,
and reproducibility (M3.4)."""

from datetime import date, time

import pytest

from trader.core.enums import OnOvershoot
from trader.core.protocols import Scheduler
from trader.core.types import SlotSpec, StrategyBinding
from trader.scheduler import triggers as triggers_mod
from trader.scheduler.calendar import TradingCalendar
from trader.scheduler.triggers import SlotScheduler

SESSION = date(2024, 7, 8)  # a Monday
HOLIDAY = date(2024, 12, 25)
SEED = 42


def _slot(
    slot_id: str, at: time, *, minutes: int = 0, on_overshoot: OnOvershoot = OnOvershoot.CLAMP
) -> SlotSpec:
    return SlotSpec(slot_id=slot_id, at=at, drift_max_minutes=minutes, on_overshoot=on_overshoot)


def _binding(
    strategy_id: str, slots: tuple[SlotSpec, ...], *, enabled: bool = True
) -> StrategyBinding:
    return StrategyBinding(
        strategy_id=strategy_id,
        strategy_name="stub",
        params={},
        universe=("AAPL",),
        slots=slots,
        enabled=enabled,
    )


def _scheduler(*bindings: StrategyBinding, seed: int | None = SEED) -> SlotScheduler:
    return SlotScheduler(bindings, TradingCalendar(), seed)


def test_implements_scheduler_protocol() -> None:
    assert isinstance(_scheduler(_binding("a", (_slot("open", time(10, 0)),))), Scheduler)


def test_merged_sorted() -> None:
    a = _binding("alpha", (_slot("open", time(11, 0)),))  # later
    b = _binding("beta", (_slot("open", time(10, 0)),))  # earlier
    triggers = _scheduler(a, b).triggers_for(SESSION)
    assert [t.strategy_id for t in triggers] == ["beta", "alpha"]  # sorted by fire_ts
    assert triggers[0].fire_ts < triggers[1].fire_ts


def test_stable_tiebreak() -> None:
    # same slot time, zero drift -> identical fire_ts -> tie-break by (strategy_id, slot_id)
    a = _binding("alpha", (_slot("open", time(10, 0)),))
    b = _binding("beta", (_slot("open", time(10, 0)),))
    triggers = _scheduler(b, a).triggers_for(SESSION)  # pass in reverse order
    assert triggers[0].fire_ts == triggers[1].fire_ts
    assert [t.strategy_id for t in triggers] == ["alpha", "beta"]


def test_disabled_skipped() -> None:
    triggers = _scheduler(_binding("a", (_slot("open", time(10, 0)),), enabled=False)).triggers_for(
        SESSION
    )
    assert triggers == []


def test_holiday_empty() -> None:
    assert _scheduler(_binding("a", (_slot("open", time(10, 0)),))).triggers_for(HOLIDAY) == []


def test_reproducible() -> None:
    binding = _binding("a", (_slot("open", time(10, 0), minutes=30),))
    s1 = _scheduler(binding)
    s2 = _scheduler(binding)
    assert [t.fire_ts for t in s1.triggers_for(SESSION)] == [
        t.fire_ts for t in s2.triggers_for(SESSION)
    ]


def test_trigger_carries_drift_and_seed() -> None:
    binding = _binding("a", (_slot("open", time(10, 0), minutes=30),))
    trigger = _scheduler(binding).triggers_for(SESSION)[0]
    assert 0 <= trigger.drift_seconds <= 30 * 60
    assert trigger.seed is not None  # realized seed persisted for replay


def test_multi_slot_per_binding_merged_sorted() -> None:
    binding = _binding(
        "a", (_slot("close", time(15, 0)), _slot("open", time(10, 0)), _slot("noon", time(12, 0)))
    )
    triggers = _scheduler(binding).triggers_for(SESSION)
    assert [t.slot_id for t in triggers] == ["open", "noon", "close"]  # sorted by fire_ts


def test_cross_session_drift_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # force a backward drift that lands on the PRIOR session (Fri 07-05 ~12:00 ET),
    # a valid session, so the guard (not the calendar gate) must drop it.
    monkeypatch.setattr(triggers_mod, "compute_drift", lambda *_a, **_k: (-252000, 7))
    sched = _scheduler(_binding("a", (_slot("open", time(10, 0), minutes=9999),)))
    assert sched.triggers_for(SESSION) == []  # not fired on the wrong day
    assert sched.skipped[0].reason == "drift crossed session"


def test_skipped_recorded_on_overshoot() -> None:
    # a 15:30 slot drifting forward on a half-day with SKIP is dropped + recorded
    half_day = date(2024, 7, 3)  # early close 13:00 ET
    sched = _scheduler(_binding("a", (_slot("late", time(15, 30), on_overshoot=OnOvershoot.SKIP),)))
    assert sched.triggers_for(half_day) == []
    assert sched.skipped[0].strategy_id == "a"
    assert sched.skipped[0].slot_id == "late"
