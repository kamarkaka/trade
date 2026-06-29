"""Tests for the M3 scheduler/strategy core types: defaults, frozen-ness, hashability,
and the per-slot catch_up override (M3.1)."""

import dataclasses
from datetime import UTC, datetime, time

import pytest

from trader.core import SlotSpec, StrategyBinding, TriggerSlot
from trader.core.enums import Distribution, DriftDirection, OnOvershoot


def _slot(**kw: object) -> SlotSpec:
    base: dict[str, object] = {"slot_id": "open", "at": time(10, 0), "drift_max_minutes": 30}
    base.update(kw)
    return SlotSpec(**base)  # type: ignore[arg-type]


def _binding(**kw: object) -> StrategyBinding:
    base: dict[str, object] = {
        "strategy_id": "s1",
        "strategy_name": "threshold",
        "params": {"k": 1},
        "universe": ("AAPL", "MSFT"),
        "slots": (_slot(),),
    }
    base.update(kw)
    return StrategyBinding(**base)  # type: ignore[arg-type]


def _trigger() -> TriggerSlot:
    return TriggerSlot(
        strategy_id="s1",
        slot_id="open",
        fire_ts=datetime(2026, 6, 29, 14, 30, tzinfo=UTC),
        drift_seconds=120,
        seed=42,
    )


def test_slotspec_defaults() -> None:
    slot = _slot()
    assert slot.drift_direction is DriftDirection.FORWARD
    assert slot.distribution is Distribution.UNIFORM
    assert slot.on_overshoot is OnOvershoot.CLAMP
    assert slot.catch_up is None  # inherits the global schedule by default


def test_slotspec_catch_up_override() -> None:
    assert _slot(catch_up=True).catch_up is True
    assert _slot(catch_up=False).catch_up is False
    with pytest.raises(TypeError):
        _slot(catch_up="yes")


def test_dataclasses_frozen() -> None:
    trigger = _trigger()
    binding = _binding()
    with pytest.raises(dataclasses.FrozenInstanceError):
        trigger.drift_seconds = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.enabled = False  # type: ignore[misc]


def test_binding_hashable() -> None:
    # hashable despite the dict `params`/`risk_overrides` (excluded from __hash__)
    binding = _binding(risk_overrides={"max_trades_per_day": 1})
    assert hash(binding)  # does not raise
    assert binding in {binding}


def test_triggerslot_hashable() -> None:
    assert hash(_trigger())
    assert _trigger() == _trigger()  # value equality


def test_binding_equality_includes_params() -> None:
    # params is excluded from hash but still part of equality
    assert _binding(params={"k": 1}) != _binding(params={"k": 2})
