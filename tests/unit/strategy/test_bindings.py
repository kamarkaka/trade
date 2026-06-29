"""Tests for load_bindings: config -> core StrategyBinding mapping, registry name
validation, catch_up inheritance, and the config-layer guards (M3.7)."""

from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

import trader.strategy  # noqa: F401 - registers built-ins
from trader.config import load_config
from trader.core.enums import DriftDirection
from trader.strategy.bindings import load_bindings

# The §11 example: two strategies on different schedules.
_TWO_STRATEGIES = """
mode: backtest
schedule:
  catch_up: true
strategies:
  - id: momentum
    name: threshold
    params: {band: 0.02, lot: 10}
    universe: [AAPL, MSFT]
    slots:
      - {id: morning, time: "09:45"}
  - id: meanrev
    name: zscore_revert
    params: {lookback: 20}
    universe: [SPY]
    slots:
      - {id: open, time: "10:00", catch_up: false}
      - {id: noon, time: "12:00"}
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_loads_two_bindings(tmp_path: Path) -> None:
    schedule, bindings = load_bindings(load_config(_write(tmp_path, _TWO_STRATEGIES)))
    by_id = {b.strategy_id: b for b in bindings}
    assert set(by_id) == {"momentum", "meanrev"}
    assert by_id["momentum"].strategy_name == "threshold"
    assert by_id["momentum"].universe == ("AAPL", "MSFT")
    assert by_id["meanrev"].strategy_name == "zscore_revert"
    assert by_id["momentum"].slots[0].at == time(9, 45)
    assert by_id["momentum"].slots[0].drift_direction is DriftDirection.FORWARD  # default applied
    assert schedule.catch_up is True


def test_slot_inherits_global_catch_up(tmp_path: Path) -> None:
    _schedule, bindings = load_bindings(load_config(_write(tmp_path, _TWO_STRATEGIES)))
    meanrev = next(b for b in bindings if b.strategy_id == "meanrev")
    assert meanrev.slots[0].catch_up is False  # explicit override on the 'open' slot
    assert meanrev.slots[1].catch_up is True  # 'noon' inherits schedule.catch_up=true


def test_params_and_risk_overrides_isolated_and_passed(tmp_path: Path) -> None:
    body = _TWO_STRATEGIES.replace(
        "params: {band: 0.02, lot: 10}",
        "params: {band: 0.02, lot: 10}\n    risk_overrides: {max_trades_per_day: 2}",
    )
    config = load_config(_write(tmp_path, body))
    _schedule, bindings = load_bindings(config)
    momentum = next(b for b in bindings if b.strategy_id == "momentum")
    assert momentum.params == {"band": 0.02, "lot": 10}
    assert momentum.risk_overrides == {"max_trades_per_day": 2}
    # copies, not aliases of the config objects
    assert momentum.params is not config.strategies[0].params
    assert momentum.risk_overrides is not config.strategies[0].risk_overrides


def test_disabled_binding_with_empty_universe_is_mapped(tmp_path: Path) -> None:
    body = """
mode: backtest
strategies:
  - id: parked
    name: threshold
    enabled: false
    universe: []
    slots: []
  - id: momentum
    name: threshold
    universe: [AAPL]
    slots:
      - {id: morning, time: "09:45"}
"""
    _schedule, bindings = load_bindings(load_config(_write(tmp_path, body)))
    disabled = next(b for b in bindings if b.strategy_id == "parked")
    assert disabled.enabled is False
    assert disabled.universe == ()  # allowed for a disabled binding


def test_explicit_true_catch_up_overrides_false_schedule(tmp_path: Path) -> None:
    body = _TWO_STRATEGIES.replace("catch_up: true", "catch_up: false").replace(
        'time: "09:45"', 'time: "09:45", catch_up: true'
    )
    _schedule, bindings = load_bindings(load_config(_write(tmp_path, body)))
    momentum = next(b for b in bindings if b.strategy_id == "momentum")
    assert momentum.slots[0].catch_up is True  # explicit True kept despite schedule False


def test_unknown_strategy_name_rejected(tmp_path: Path) -> None:
    body = _TWO_STRATEGIES.replace("name: threshold", "name: no_such_strategy")
    with pytest.raises(ValueError, match="unknown strategy name"):
        load_bindings(load_config(_write(tmp_path, body)))


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    body = _TWO_STRATEGIES.replace("id: meanrev", "id: momentum")
    with pytest.raises((ValidationError, ValueError)):  # AppConfig enforces unique ids
        load_config(_write(tmp_path, body))


def test_drift_ceiling_rejected(tmp_path: Path) -> None:
    body = _TWO_STRATEGIES.replace('time: "09:45"', 'time: "09:45", drift_max_minutes: 999')
    with pytest.raises(ValidationError):  # SlotConfig caps drift at the ceiling
        load_config(_write(tmp_path, body))
