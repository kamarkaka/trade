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
from trader.core import Account, Bar, Decision, MarketSnapshot, Position, Quote
from trader.core.enums import Action
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


def _decide(
    strategy: Strategy, snapshot: MarketSnapshot, data: FakeMarketDataProvider
) -> Sequence[Decision]:
    return strategy.decide(snapshot, [], ACCOUNT, data, FakeClock(ASOF))


def _signal_inputs(name: str) -> tuple[MarketSnapshot, FakeMarketDataProvider]:
    """Inputs tuned to actually PROVOKE a non-HOLD decision for ``name`` so the determinism /
    mutation checks exercise the real signal path, not just an early-out."""
    if name == "zscore_revert":
        # Ascending closes give std>0; a far-below-mean current last => very negative z => BUY.
        bars = [
            Bar(
                "AAPL",
                ASOF - timedelta(days=40 - i),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal(100 + i),
                1000,
            )
            for i in range(40)
        ]
        data = FakeMarketDataProvider(quotes={"AAPL": [_quote()]}, bars={"AAPL": bars})
        return make_snapshot(ASOF, {"AAPL": _quote(last="50")}), data
    # threshold (and any quote-based default): a dip below prev_close*(1-band).
    return make_snapshot(ASOF, {"AAPL": _quote(last="96")}), _data()


@pytest.fixture(params=REGISTRY.names())
def strategy_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_registry_has_strategies() -> None:
    assert set(REGISTRY.names()) >= {"threshold", "zscore_revert"}


def test_decide_returns_wellformed_decisions(strategy_name: str) -> None:
    decisions = _decide(_strategy(strategy_name), make_snapshot(ASOF, {"AAPL": _quote()}), _data())
    assert isinstance(decisions, Sequence)
    assert_decisions_well_formed(decisions, UNIVERSE)


def test_decide_produces_a_signal(strategy_name: str) -> None:
    # Each strategy's signal path is reachable (so the determinism/mutation checks below
    # exercise real output, not a vacuous early-out).
    snap, data = _signal_inputs(strategy_name)
    decisions = _decide(_strategy(strategy_name), snap, data)
    assert any(d.action in (Action.BUY, Action.SELL) for d in decisions), "expected a signal"
    assert_decisions_well_formed(decisions, UNIVERSE)


def test_decide_is_deterministic(strategy_name: str) -> None:
    strat = _strategy(strategy_name)
    snap, data = _signal_inputs(strategy_name)  # inputs that actually produce a decision
    first = list(_decide(strat, snap, data))
    second = list(_decide(strat, snap, data))
    assert first == second  # identical inputs -> identical outputs


def test_decide_does_not_mutate_inputs(strategy_name: str) -> None:
    snap, data = _signal_inputs(strategy_name)
    positions: list[Position] = [Position("AAPL", 5, Decimal("100"), Decimal("500"))]
    snap_before, pos_before, acct_before = (
        copy.deepcopy(snap),
        copy.deepcopy(positions),
        copy.deepcopy(ACCOUNT),
    )
    _strategy(strategy_name).decide(snap, positions, ACCOUNT, data, FakeClock(ASOF))
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
