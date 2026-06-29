"""Generic Strategy-contract conformance suite (M6.1): every registered strategy must be
pure (no wall clock, no input mutation, deterministic), asof-bound, and emit well-formed
universe-scoped decisions. Run against every class in the registry, so a new strategy is
guarded automatically."""

import copy
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import trader.strategy  # noqa: F401 - registers the built-in strategies
from fakes import FakeClock, FakeMarketDataProvider
from trader.core import Account, Bar, MarketSnapshot, Position, Quote
from trader.core.protocols import Strategy
from trader.strategy.contract import (
    assert_decisions_well_formed,
    assert_no_wallclock,
    make_snapshot,
)
from trader.strategy.registry import REGISTRY

ASOF = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)
UNIVERSE = ("AAPL",)
ACCOUNT = Account(cash=Decimal("100000"), buying_power=Decimal("100000"), equity=Decimal("100000"))


def _quote(prev_close: str | None = "100", last: str = "100") -> Quote:
    pc = Decimal(prev_close) if prev_close is not None else None
    return Quote(
        "AAPL", ASOF, Decimal(last), Decimal("99.9"), Decimal("100.1"), 1000, prev_close=pc
    )


def _bars(*, volume: int = 1000, n: int = 40) -> list[Bar]:
    # n daily bars strictly before asof (asof-bound providers exclude > asof).
    out: list[Bar] = []
    for i in range(1, n + 1):
        ts = ASOF - timedelta(days=i)
        out.append(
            Bar("AAPL", ts, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), volume)
        )
    return list(reversed(out))


def _data(*, volume: int = 1000) -> FakeMarketDataProvider:
    return FakeMarketDataProvider(quotes={"AAPL": [_quote()]}, bars={"AAPL": _bars(volume=volume)})


def _strategy(name: str) -> Strategy:
    return REGISTRY.create(name, {})  # default params


def _decide(strategy: Strategy, snapshot: MarketSnapshot, data: FakeMarketDataProvider):
    return strategy.decide(snapshot, [], ACCOUNT, data, FakeClock(ASOF))


@pytest.fixture(params=REGISTRY.names())
def strategy_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_registry_has_strategies() -> None:
    assert set(REGISTRY.names()) >= {"threshold", "zscore_revert"}


def test_decide_returns_wellformed_decisions(strategy_name: str) -> None:
    decisions = _decide(_strategy(strategy_name), make_snapshot(ASOF, {"AAPL": _quote()}), _data())
    assert isinstance(decisions, Sequence)
    assert_decisions_well_formed(decisions, UNIVERSE)


def test_decide_is_deterministic(strategy_name: str) -> None:
    strat = _strategy(strategy_name)
    snap = make_snapshot(ASOF, {"AAPL": _quote(last="96")})  # a dip, to provoke a decision
    first = list(_decide(strat, snap, _data()))
    second = list(_decide(strat, snap, _data()))
    assert first == second  # identical inputs -> identical outputs


def test_decide_does_not_mutate_inputs(strategy_name: str) -> None:
    snap = make_snapshot(ASOF, {"AAPL": _quote(last="96")})
    positions: list[Position] = [Position("AAPL", 5, Decimal("100"), Decimal("500"))]
    snap_before, pos_before, acct_before = (
        copy.deepcopy(snap),
        copy.deepcopy(positions),
        copy.deepcopy(ACCOUNT),
    )
    _strategy(strategy_name).decide(snap, positions, ACCOUNT, _data(), FakeClock(ASOF))
    assert snap == snap_before and positions == pos_before and acct_before == ACCOUNT


def test_strategy_module_has_no_wallclock(strategy_name: str) -> None:
    assert_no_wallclock(type(_strategy(strategy_name)))


def test_no_wallclock_runtime(strategy_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Runtime guard: with wall-clock sources poisoned, decide() still works via the injected
    # clock / snapshot.asof only.
    def _boom(*_a: object, **_k: object) -> float:
        raise AssertionError("strategy read the wall clock")

    monkeypatch.setattr(time, "time", _boom)
    monkeypatch.setattr(time, "monotonic", _boom)
    decisions = _decide(_strategy(strategy_name), make_snapshot(ASOF, {"AAPL": _quote()}), _data())
    assert_decisions_well_formed(decisions, UNIVERSE)


def test_empty_universe(strategy_name: str) -> None:
    decisions = _decide(_strategy(strategy_name), make_snapshot(ASOF, {}), FakeMarketDataProvider())
    assert list(decisions) == []  # no symbols -> no decisions, no raise


def test_missing_prev_close(strategy_name: str) -> None:
    snap = make_snapshot(ASOF, {"AAPL": _quote(prev_close=None)})
    assert_decisions_well_formed(_decide(_strategy(strategy_name), snap, _data()), UNIVERSE)


def test_zero_volume_bar(strategy_name: str) -> None:
    snap = make_snapshot(ASOF, {"AAPL": _quote()})
    assert_decisions_well_formed(_decide(_strategy(strategy_name), snap, _data(volume=0)), UNIVERSE)
