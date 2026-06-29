"""Tests for SimBroker advanced fills: limit range-crossing, volume-capped partials
with WORKING remainder, working-order re-processing, and DAY expiry (M2.6)."""

from datetime import UTC, datetime, timedelta
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


def test_same_bar_does_not_overshoot_participation() -> None:
    # repeated process calls within the SAME bar must not exceed that bar's budget
    broker = _broker(quote=_quote(volume=1000), max_participation=Decimal("0.1"))
    boid = broker.submit_order(_market(Side.BUY, qty=250))  # 100 at this bar
    broker.process_working_orders()  # same ts -> no additional fill
    assert broker.get_order(boid).quantity == 100


def test_working_remainder_fills_across_bars() -> None:
    # each new bar (advancing ts) grants a fresh 10% budget until complete
    quotes = [
        Quote("AAPL", NOW + timedelta(days=d), Decimal("100"), Decimal("100"), Decimal("100"), 1000)
        for d in range(3)
    ]
    clock = FakeClock(NOW)
    broker = SimBroker(
        FakeMarketDataProvider(quotes={"AAPL": quotes}),
        clock,
        starting_cash=Decimal("1000000"),
        max_participation=Decimal("0.1"),
    )
    boid = broker.submit_order(_market(Side.BUY, qty=250))  # 100 @ day 0
    clock.advance(timedelta(days=1))
    broker.process_working_orders()  # +100 -> 200 @ day 1
    clock.advance(timedelta(days=1))
    broker.process_working_orders()  # +50 -> 250 @ day 2
    fill = broker.get_order(boid)
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 250
    assert broker.get_positions()[0].quantity == 250


def test_limit_partial_then_complete_next_bar() -> None:
    bars = [
        Bar(
            "AAPL",
            NOW + timedelta(days=d),
            Decimal("100"),
            Decimal("105"),
            Decimal("95"),
            Decimal("100"),
            1000,
        )
        for d in range(2)
    ]
    clock = FakeClock(NOW)
    broker = SimBroker(
        FakeMarketDataProvider(quotes={"AAPL": [_quote()]}, bars={"AAPL": bars}),
        clock,
        starting_cash=Decimal("1000000"),
        max_participation=Decimal("0.1"),
    )
    boid = broker.submit_order(_limit(Side.BUY, "100", qty=150))  # cap 100 -> partial @ day 0
    assert broker.get_order(boid).status is OrderStatus.PARTIAL_FILL
    assert broker.get_order(boid).quantity == 100
    clock.advance(timedelta(days=1))
    broker.process_working_orders()  # +50 -> filled @ day 1
    fill = broker.get_order(boid)
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 150
    assert fill.price == Decimal("100")  # VWAP of two fills at the same limit


def test_low_volume_floors_cap_to_one() -> None:
    broker = _broker(quote=_quote(volume=5), max_participation=Decimal("0.1"))  # 0.5 -> floor 1
    fill = broker.get_order(broker.submit_order(_market(Side.BUY, qty=3)))
    assert fill.quantity == 1  # not starved at 0
    assert fill.status is OrderStatus.PARTIAL_FILL


def test_zero_volume_no_fill() -> None:
    broker = _broker(quote=_quote(volume=0), max_participation=Decimal("0.1"))
    fill = broker.get_order(broker.submit_order(_market(Side.BUY, qty=3)))
    assert fill.status is OrderStatus.WORKING
    assert fill.quantity == 0


def test_market_full_fill_without_cap() -> None:
    # max_participation=None -> market orders fully fill regardless of volume (M2.5 parity)
    broker = _broker(quote=_quote(volume=10))
    fill = broker.get_order(broker.submit_order(_market(Side.BUY, qty=1000)))
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 1000


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
