"""Tests for SimBroker core: market fill price (slippage), cash/position updates,
fees, idempotency, and account valuation (M2.5)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fakes import FakeClock, FakeMarketDataProvider
from trader.broker import FeesModel, SimBroker, SlippageModel
from trader.core import Order, Quote
from trader.core.enums import OrderType, Side
from trader.core.protocols import Broker

NOW = datetime(2026, 6, 28, 15, 0, tzinfo=UTC)


def _quote(symbol: str, *, bid: str, ask: str) -> Quote:
    mid = (Decimal(bid) + Decimal(ask)) / 2
    return Quote(symbol=symbol, ts=NOW, last=mid, bid=Decimal(bid), ask=Decimal(ask), volume=1000)


def _broker(
    *,
    bid: str = "100",
    ask: str = "100",
    cash: str = "100000",
    fees: FeesModel | None = None,
    slippage: SlippageModel | None = None,
) -> SimBroker:
    data = FakeMarketDataProvider(quotes={"AAPL": [_quote("AAPL", bid=bid, ask=ask)]})
    return SimBroker(
        data,
        FakeClock(NOW),
        starting_cash=Decimal(cash),
        fees=fees,
        slippage=slippage,
    )


def _order(side: Side, qty: int = 10, cid: str = "c1") -> Order:
    return Order(
        client_order_id=cid,
        strategy_id="s1",
        symbol="AAPL",
        side=side,
        quantity=qty,
        order_type=OrderType.MARKET,
    )


def test_satisfies_broker_protocol() -> None:
    assert isinstance(_broker(), Broker)


def test_market_buy_fills_at_ask_plus_slippage() -> None:
    broker = _broker(bid="99", ask="101", slippage=SlippageModel("bps", Decimal("10")))
    bid = broker.submit_order(_order(Side.BUY))
    fill = broker.get_order(bid)
    # ask 101 + 10bps of 101 = 101 + 0.101
    assert fill.price == Decimal("101") + Decimal("101") * Decimal("10") / Decimal("10000")


def test_market_sell_fills_at_bid_minus_slippage() -> None:
    broker = _broker(bid="99", ask="101", slippage=SlippageModel("bps", Decimal("10")))
    bid = broker.submit_order(_order(Side.SELL))
    fill = broker.get_order(bid)
    assert fill.price == Decimal("99") - Decimal("99") * Decimal("10") / Decimal("10000")


def test_fixed_slippage() -> None:
    broker = _broker(bid="100", ask="100", slippage=SlippageModel("fixed", Decimal("0.05")))
    fill = broker.get_order(broker.submit_order(_order(Side.BUY)))
    assert fill.price == Decimal("100.05")


def test_cash_and_position_update_on_buy() -> None:
    broker = _broker(bid="100", ask="100", cash="100000")
    broker.submit_order(_order(Side.BUY, qty=10))
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == 10
    assert positions[0].avg_price == Decimal("100")
    # cash down by qty*price (no fees here)
    assert broker.get_account().cash == Decimal("100000") - Decimal("1000")


def test_fees_applied() -> None:
    broker = _broker(bid="100", ask="100", cash="100000", fees=FeesModel(regulatory_bps=5.0))
    fill = broker.get_order(broker.submit_order(_order(Side.BUY, qty=10)))
    notional = Decimal("1000")
    expected_fee = notional * Decimal("5.0") / Decimal("10000")
    assert fill.fees == expected_fee
    assert broker.get_account().cash == Decimal("100000") - notional - expected_fee


def test_sell_reduces_position_and_keeps_basis() -> None:
    broker = _broker(bid="100", ask="100", cash="100000")
    broker.submit_order(_order(Side.BUY, qty=10, cid="b"))
    broker.submit_order(_order(Side.SELL, qty=4, cid="s"))
    pos = broker.get_positions()[0]
    assert pos.quantity == 6
    assert pos.avg_price == Decimal("100")  # basis unchanged on a reduction


def test_buy_then_buy_weighted_average_basis() -> None:
    data = FakeMarketDataProvider(
        quotes={
            "AAPL": [
                Quote("AAPL", NOW, Decimal("100"), Decimal("100"), Decimal("100"), 1000),
            ]
        }
    )
    broker = SimBroker(data, FakeClock(NOW), starting_cash=Decimal("100000"))
    broker.submit_order(_order(Side.BUY, qty=10, cid="a"))
    broker.submit_order(_order(Side.BUY, qty=30, cid="b"))
    pos = broker.get_positions()[0]
    assert pos.quantity == 40
    assert pos.avg_price == Decimal("100")  # same price -> avg stays 100


def test_idempotent_resubmit_does_not_double_fill() -> None:
    broker = _broker(bid="100", ask="100")
    first = broker.submit_order(_order(Side.BUY, qty=10, cid="dup"))
    second = broker.submit_order(_order(Side.BUY, qty=10, cid="dup"))
    assert first == second
    assert broker.get_positions()[0].quantity == 10  # only filled once


def test_limit_order_not_supported_yet() -> None:
    broker = _broker()
    order = Order(
        client_order_id="L1",
        strategy_id="s1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("90"),
    )
    with pytest.raises(NotImplementedError):
        broker.submit_order(order)


def test_get_order_unknown_raises() -> None:
    with pytest.raises(KeyError):
        _broker().get_order("SIM-999")


def test_account_equity_marks_position() -> None:
    broker = _broker(bid="100", ask="100", cash="100000")
    broker.submit_order(_order(Side.BUY, qty=10))
    # equity = remaining cash + position market value (10 * 100)
    acct = broker.get_account()
    assert acct.equity == Decimal("100000")
    assert acct.buying_power == acct.cash
