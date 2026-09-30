"""CanaryStrategy: the deterministic, long-only round trip used for the guarded live
verification (M5.7). It must never open a short or touch a position it did not create."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fakes import FakeClock, FakeMarketDataProvider
from trader.core import Account, Decision, Position, Quote
from trader.core.enums import Action
from trader.strategy.contract import make_snapshot
from trader.strategy.params import validate_params
from trader.strategy.registry import REGISTRY

ASOF = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)
ACCOUNT = Account(cash=Decimal("10000"), buying_power=Decimal("10000"), equity=Decimal("10000"))


def _quote(symbol: str) -> Quote:
    p = Decimal("100")
    return Quote(symbol, ASOF, p, p, p, 1000, prev_close=p)


def _decide(positions: list[Position], symbols: tuple[str, ...] = ("SPY",), lot: int = 1):  # type: ignore[no-untyped-def]
    strategy = REGISTRY.create("canary", {"lot": lot})
    snapshot = make_snapshot(ASOF, {s: _quote(s) for s in symbols})
    return list(
        strategy.decide(snapshot, positions, ACCOUNT, FakeMarketDataProvider(), FakeClock(ASOF))
    )


def _pos(symbol: str, qty: int) -> Position:
    return Position(symbol, qty, Decimal("100"), Decimal(100 * qty))


def test_flat_buys_one_lot() -> None:
    assert _decide([]) == [Decision(Action.BUY, "SPY", 1, rationale="canary open")]


def test_holding_exactly_its_lot_sells_it() -> None:
    assert _decide([_pos("SPY", 1)]) == [Decision(Action.SELL, "SPY", 1, rationale="canary close")]


@pytest.mark.parametrize("qty", [-1, 2, 50])
def test_unexpected_position_is_left_alone(qty: int) -> None:
    # A short, or shares bought by hand: never sell what it did not buy, never cover shorts.
    assert _decide([_pos("SPY", qty)]) == []


def test_symbols_are_decided_independently_and_in_order() -> None:
    decisions = _decide([_pos("QQQ", 2)], symbols=("SPY", "QQQ", "IWM"), lot=2)
    assert [(d.symbol, d.action, d.quantity) for d in decisions] == [
        ("IWM", Action.BUY, 2),
        ("QQQ", Action.SELL, 2),
        ("SPY", Action.BUY, 2),
    ]


def test_params_are_validated() -> None:
    assert validate_params("canary", {}) == {"lot": 1}
    with pytest.raises(ValueError, match="canary"):
        validate_params("canary", {"lot": 0})
    with pytest.raises(ValueError, match="canary"):
        validate_params("canary", {"lots": 1})  # typo'd key is rejected
    with pytest.raises(ValueError, match="positive"):
        REGISTRY.create("canary", {"lot": -1})
