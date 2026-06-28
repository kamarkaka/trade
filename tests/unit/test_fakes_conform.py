"""The shared fakes (M0.8) satisfy their core Protocols and behave deterministically."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fakes import FakeBroker, FakeClock, FakeMarketDataProvider
from trader.core import (
    Bar,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)
from trader.core.protocols import Broker, Clock, MarketDataProvider

T = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
D = Decimal


def _order(**kw: object) -> Order:
    base: dict[str, object] = {
        "client_order_id": "c1",
        "strategy_id": "s",
        "symbol": "AAPL",
        "side": Side.BUY,
        "quantity": 1,
        "order_type": OrderType.MARKET,
    }
    base.update(kw)
    return Order(**base)  # type: ignore[arg-type]


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeBroker(), Broker)
    assert isinstance(FakeMarketDataProvider(), MarketDataProvider)


def test_fake_clock_controls() -> None:
    c = FakeClock(T)
    assert c.now() == T
    c.advance(timedelta(hours=1))
    assert c.now() == T + timedelta(hours=1)
    assert c.is_market_open() is True
    c.set_market_open(False)
    assert c.is_market_open() is False


def test_fakebroker_submit_fill_and_log() -> None:
    b = FakeBroker()
    order = _order(order_type=OrderType.LIMIT, limit_price=D("150"), quantity=2)
    bid = b.submit_order(order)
    fill = b.get_order(bid)
    assert fill.client_order_id == "c1"
    assert fill.status is OrderStatus.FILLED
    assert fill.price == D("150")
    assert b.submitted == [order]


def test_fakebroker_simulated_timeout_then_recovers() -> None:
    b = FakeBroker()
    b.fail_next_submit = True
    with pytest.raises(TimeoutError):
        b.submit_order(_order())
    assert b.submit_order(_order()).startswith("b-")  # next submit succeeds


def test_fakebroker_dedupe_by_client_id() -> None:
    b = FakeBroker()
    b.dedupe_by_client_id = True
    order = _order()
    assert b.submit_order(order) == b.submit_order(order)  # at-most-once


def test_fakebroker_partial_fill() -> None:
    b = FakeBroker()
    b.fill_quantity = 1
    bid = b.submit_order(_order(quantity=3))
    fill = b.get_order(bid)
    assert fill.quantity == 1
    assert fill.status is OrderStatus.PARTIAL_FILL


def test_fakebroker_timeout_but_order_landed() -> None:
    # Simulate "request reached the broker, response was lost": submit raises, but
    # the fill is recoverable by client order id (idempotency/reconciliation path).
    b = FakeBroker()
    b.fail_next_submit = True
    b.record_on_timeout = True
    order = _order(client_order_id="c-lost")
    with pytest.raises(TimeoutError):
        b.submit_order(order)
    recovered = b.find_by_client_id("c-lost")
    assert recovered is not None
    assert recovered.client_order_id == "c-lost"


def test_fakebroker_timeout_without_landing() -> None:
    b = FakeBroker()
    b.fail_next_submit = True  # record_on_timeout stays False
    with pytest.raises(TimeoutError):
        b.submit_order(_order(client_order_id="c-none"))
    assert b.find_by_client_id("c-none") is None


def test_fakebroker_positions_and_account() -> None:
    b = FakeBroker()
    b.set_position(Position(symbol="AAPL", quantity=10, avg_price=D("100"), market_value=D("1010")))
    assert b.get_positions()[0].symbol == "AAPL"
    assert b.get_account().cash == D("100000")
    b.cancel_order("b-1")
    assert b.cancelled == ["b-1"]


def test_fake_market_data_no_lookahead() -> None:
    t0, t1 = T, T + timedelta(minutes=1)
    q0 = Quote(symbol="AAPL", ts=t0, last=D("100"), bid=D("99"), ask=D("101"), volume=1)
    q1 = Quote(symbol="AAPL", ts=t1, last=D("102"), bid=D("101"), ask=D("103"), volume=1)
    md = FakeMarketDataProvider(quotes={"AAPL": [q0, q1]})
    assert md.get_quote("AAPL", t0).last == D("100")  # q1 not visible yet
    assert md.get_quote("AAPL", t1).last == D("102")
    with pytest.raises(KeyError):
        md.get_quote("AAPL", t0 - timedelta(seconds=1))
    with pytest.raises(KeyError):
        md.get_quote("MSFT", t1)


def test_fake_market_data_bars_filtered_by_asof() -> None:
    days = [T + timedelta(days=n) for n in range(3)]
    bars = [
        Bar(symbol="AAPL", ts=t, open=D("1"), high=D("2"), low=D("1"), close=D("1"), volume=1)
        for t in days
    ]
    md = FakeMarketDataProvider(bars={"AAPL": bars})
    got = md.get_bars("AAPL", days[0], days[2], "daily", asof=days[1])  # asof excludes day 2
    assert [b.ts for b in got] == days[:2]
    # start/end window clips independently of asof
    windowed = md.get_bars("AAPL", days[1], days[2], "daily", asof=days[2])
    assert [b.ts for b in windowed] == days[1:]
