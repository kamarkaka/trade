"""Tests for size_decision: HOLD/zero -> None, side mapping, limit pass-through,
client_order_id uniqueness + injection (M3.8)."""

from decimal import Decimal

import pytest

from trader.config.models import ExecutionConfig
from trader.core import Decision
from trader.core.enums import Action, OrderType, Side
from trader.sizing.sizer import size_decision

MARKET = ExecutionConfig(order_type=OrderType.MARKET)
LIMIT = ExecutionConfig(order_type=OrderType.LIMIT)


def test_hold_returns_none() -> None:
    # HOLD (the only constructible non-positive-quantity Decision) sizes to nothing
    assert size_decision(Decision(Action.HOLD, "AAPL"), "s1", MARKET) is None


def test_buy_maps_side() -> None:
    order = size_decision(Decision(Action.BUY, "AAPL", 10), "momentum", MARKET)
    assert order is not None
    assert order.side is Side.BUY
    assert order.quantity == 10
    assert order.symbol == "AAPL"
    assert order.strategy_id == "momentum"
    assert order.order_type is OrderType.MARKET
    assert order.limit_price is None


def test_sell_maps_side() -> None:
    order = size_decision(Decision(Action.SELL, "AAPL", 5), "s1", MARKET)
    assert order is not None
    assert order.side is Side.SELL


def test_limit_passthrough() -> None:
    decision = Decision(Action.BUY, "AAPL", 10, limit_price=Decimal("99.50"))
    order = size_decision(decision, "s1", LIMIT)
    assert order is not None
    assert order.order_type is OrderType.LIMIT
    assert order.limit_price == Decimal("99.50")


def test_market_ignores_decision_limit_price() -> None:
    decision = Decision(Action.BUY, "AAPL", 10, limit_price=Decimal("99.50"))
    order = size_decision(decision, "s1", MARKET)  # MARKET must not carry a limit
    assert order is not None
    assert order.limit_price is None


def test_client_order_id_unique() -> None:
    a = size_decision(Decision(Action.BUY, "AAPL", 1), "s1", MARKET)
    b = size_decision(Decision(Action.BUY, "AAPL", 1), "s1", MARKET)
    assert a is not None and b is not None
    assert a.client_order_id != b.client_order_id


def test_limit_without_price_fails_loud() -> None:
    # a LIMIT exec config with a decision that supplied no limit_price is a bug:
    # Order() raises rather than silently dropping the intent.
    with pytest.raises(ValueError, match="LIMIT order requires a limit_price"):
        size_decision(Decision(Action.BUY, "AAPL", 10), "s1", LIMIT)


def test_id_factory_injected() -> None:
    order = size_decision(Decision(Action.BUY, "AAPL", 1), "s1", MARKET, id_factory=lambda: "fixed")
    assert order is not None
    assert order.client_order_id == "fixed"
