"""Tests for the seeded jitter module: reproducibility, bounds, direction,
strategy independence, and entropy wiring (M3.2)."""

from datetime import date, time

import numpy as np
import pytest

from trader.core.enums import DriftDirection
from trader.core.types import SlotSpec
from trader.scheduler import jitter
from trader.scheduler.jitter import compute_drift, stable_seed

DAY = date(2026, 6, 29)


def _slot(direction: DriftDirection = DriftDirection.FORWARD, minutes: int = 30) -> SlotSpec:
    return SlotSpec(
        slot_id="open", at=time(10, 0), drift_max_minutes=minutes, drift_direction=direction
    )


def test_reproducible_with_seed() -> None:
    a = compute_drift(_slot(), base_seed=42, slot_date=DAY, strategy_id="momentum")
    b = compute_drift(_slot(), base_seed=42, slot_date=DAY, strategy_id="momentum")
    assert a == b  # (drift_seconds, seed) identical across calls
    # stable_seed is a pure function of inputs (blake2b, not salted hash())
    assert stable_seed(42, DAY, "momentum", "open") == stable_seed(42, DAY, "momentum", "open")


def test_forward_bounds() -> None:
    slot = _slot(DriftDirection.FORWARD, minutes=30)
    for i in range(200):
        drift, _ = compute_drift(slot, base_seed=42, slot_date=DAY, strategy_id=f"s{i}")
        assert 0 <= drift <= 30 * 60


def test_symmetric_bounds() -> None:
    slot = _slot(DriftDirection.SYMMETRIC, minutes=30)
    for i in range(200):
        drift, _ = compute_drift(slot, base_seed=7, slot_date=DAY, strategy_id=f"s{i}")
        assert -30 * 60 <= drift <= 30 * 60


def test_backward_bounds() -> None:
    slot = _slot(DriftDirection.BACKWARD, minutes=15)
    for i in range(200):
        drift, _ = compute_drift(slot, base_seed=1, slot_date=DAY, strategy_id=f"s{i}")
        assert -15 * 60 <= drift <= 0


def test_zero_drift_when_max_is_zero() -> None:
    drift, _ = compute_drift(_slot(minutes=0), base_seed=42, slot_date=DAY, strategy_id="m")
    assert drift == 0


def test_strategy_independence() -> None:
    a, _ = compute_drift(_slot(), base_seed=42, slot_date=DAY, strategy_id="momentum")
    b, _ = compute_drift(_slot(), base_seed=42, slot_date=DAY, strategy_id="meanrev")
    assert a != b  # different strategy_id -> independent drift (high probability)


def test_date_independence() -> None:
    a, _ = compute_drift(_slot(), base_seed=42, slot_date=DAY, strategy_id="m")
    b, _ = compute_drift(_slot(), base_seed=42, slot_date=date(2026, 6, 30), strategy_id="m")
    assert a != b  # different day -> different drift


def test_entropy_when_seed_none_usually_differs() -> None:
    seeds = {stable_seed(None, DAY, "m", "open") for _ in range(10)}
    assert len(seeds) > 1  # fresh entropy each call


def test_entropy_wiring_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    # patch the entropy source: base_seed=None must derive from secrets.randbits
    monkeypatch.setattr(jitter.secrets, "randbits", lambda _bits: 123456789)
    assert stable_seed(None, DAY, "m", "open") == 123456789
    drift, seed = compute_drift(_slot(), base_seed=None, slot_date=DAY, strategy_id="m")
    assert seed == 123456789
    # and the drift matches a Generator built from that exact seed
    expected = round(float(np.random.default_rng(123456789).uniform(0, 30 * 60)))
    assert drift == expected


def test_does_not_disturb_global_rng() -> None:
    np.random.seed(0)
    before = np.random.get_state()[1].copy()  # type: ignore[index]
    compute_drift(_slot(), base_seed=42, slot_date=DAY, strategy_id="m")
    after = np.random.get_state()[1]  # type: ignore[index]
    assert np.array_equal(before, after)  # local Generator only; global untouched
