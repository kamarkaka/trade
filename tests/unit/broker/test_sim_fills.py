"""Tests for SimBroker advanced fills: limit range-crossing, volume-capped partials
with WORKING remainder, working-order re-processing, and DAY expiry (M2.6)."""

from datetime import UTC, datetime
from decimal import Decimal

from fakes import FakeClock, FakeMarketDataProvider
from trader.broker import SimBroker
from trader.core import Bar, Order, Quote
from trader.core.enums import OrderStatus, OrderType, Side, TimeInForce

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _bar(*, low: str, high: str, close: str = "100", volume: int = 1000) -> Bar:
    return Bar(
        symbol="AAPL",
        ts=NOW,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def _quote(*, price: str = "100", volume: int = 1000) -> Quote:
    p = Decimal(price)
    return Quote(symbol="AAPL", ts=NOW, last=p, bid=p, ask=p, volume=volume)


def _broker(
    *,
    bar: Bar | None = None,
    quote: Quote | None = None,
    cash: str = "1000000",
    max_participation: Decimal | None = None,
) -> SimBroker:
    data = FakeMarketDataProvider(
        quotes={"AAPL": [quote or _quote()]},
        bars={"AAPL": [bar]} if bar is not None else {},
    )
    return SimBroker(
        data, FakeClock(NOW), starting_cash=Decimal(cash), max_participation=max_participation
    )


def _limit(
    side: Side, price: str, qty: int = 10, cid: str = "L1", tif: TimeInForce = TimeInForce.DAY
) -> Order:
    return Order(
        client_order_id=cid,
        strategy_id="s1",
        symbol="AAPL",
        side=side,
        quantity=qty,
        order_type=OrderType.LIMIT,
        limit_price=Decimal(price),
        tif=tif,
    )


def _market(side: Side, qty: int = 10, cid: str = "M1") -> Order:
    return Order(
        client_order_id=cid,
        strategy_id="s1",
        symbol="AAPL",
        side=side,
        quantity=qty,
        order_type=OrderType.MARKET,
    )


# --- limit crossing --------------------------------------------------------- #


def test_limit_fills_when_range_crosses() -> None:
    broker = _broker(bar=_bar(low="95", high="105"))
    fill = broker.get_order(broker.submit_order(_limit(Side.BUY, "100", qty=10)))
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 10
    assert fill.price == Decimal("100")  # fills at the limit price


def test_limit_sell_fills_when_high_crosses() -> None:
    broker = _broker(bar=_bar(low="95", high="105"))
    fill = broker.get_order(broker.submit_order(_limit(Side.SELL, "102", qty=10)))
    assert fill.status is OrderStatus.FILLED
    assert fill.price == Decimal("102")


def test_limit_no_fill_when_out_of_range() -> None:
    broker = _broker(bar=_bar(low="98", high="105"))
    boid = broker.submit_order(_limit(Side.BUY, "90", qty=10))  # 90 < low 98 -> no cross
    fill = broker.get_order(boid)
    assert fill.status is OrderStatus.WORKING
    assert fill.quantity == 0
    assert broker.get_positions() == []  # nothing filled


def test_limit_no_bar_stays_working() -> None:
    broker = _broker(bar=None)  # no bar data to evaluate against
    fill = broker.get_order(broker.submit_order(_limit(Side.BUY, "100")))
    assert fill.status is OrderStatus.WORKING


# --- partial fills (volume cap) --------------------------------------------- #


def test_partial_fill_capped_by_volume() -> None:
    broker = _broker(quote=_quote(volume=1000), max_participation=Decimal("0.1"))
    boid = broker.submit_order(_market(Side.BUY, qty=250))  # cap = 0.1 * 1000 = 100
    fill = broker.get_order(boid)
    assert fill.status is OrderStatus.PARTIAL_FILL
    assert fill.quantity == 100  # capped at ADV fraction
    assert broker.get_positions()[0].quantity == 100  # remainder (150) still WORKING


def test_working_remainder_fills_on_next_bar() -> None:
    broker = _broker(quote=_quote(volume=1000), max_participation=Decimal("0.1"))
    boid = broker.submit_order(_market(Side.BUY, qty=250))  # fills 100, 150 remain
    broker.process_working_orders()  # +100 -> 200
    broker.process_working_orders()  # +50 -> 250 (capped each round)
    fill = broker.get_order(boid)
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 250
    assert broker.get_positions()[0].quantity == 250


def test_limit_partial_then_complete() -> None:
    broker = _broker(bar=_bar(low="95", high="105", volume=1000), max_participation=Decimal("0.1"))
    boid = broker.submit_order(_limit(Side.BUY, "100", qty=150))  # cap 100 -> partial
    assert broker.get_order(boid).status is OrderStatus.PARTIAL_FILL
    assert broker.get_order(boid).quantity == 100
    broker.process_working_orders()  # +50 -> filled
    fill = broker.get_order(boid)
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 150
    assert fill.price == Decimal("100")  # VWAP of two fills at the same limit


# --- expiry / cancel -------------------------------------------------------- #


def test_day_order_expires_at_close() -> None:
    broker = _broker(bar=_bar(low="98", high="105"))
    boid = broker.submit_order(_limit(Side.BUY, "90", tif=TimeInForce.DAY))  # WORKING
    broker.expire_day_orders()
    assert broker.get_order(boid).status is OrderStatus.EXPIRED


def test_gtc_order_does_not_expire_at_close() -> None:
    broker = _broker(bar=_bar(low="98", high="105"))
    boid = broker.submit_order(_limit(Side.BUY, "90", tif=TimeInForce.GTC))
    broker.expire_day_orders()
    assert broker.get_order(boid).status is OrderStatus.WORKING


def test_cancel_working_order() -> None:
    broker = _broker(bar=_bar(low="98", high="105"))
    boid = broker.submit_order(_limit(Side.BUY, "90"))
    broker.cancel_order(boid)
    assert broker.get_order(boid).status is OrderStatus.CANCELED
