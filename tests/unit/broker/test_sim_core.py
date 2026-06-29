"""Tests for SimBroker core: market fill price (slippage), cash/position updates,
fees, idempotency, and account valuation (M2.5)."""

from datetime import UTC, datetime, timedelta
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


def test_regulatory_fee_on_sell_only() -> None:
    broker = _broker(bid="100", ask="100", cash="100000", fees=FeesModel(regulatory_bps=5.0))
    buy_fill = broker.get_order(broker.submit_order(_order(Side.BUY, qty=10, cid="b")))
    assert buy_fill.fees == Decimal("0")  # regulatory fee is sell-side only

    sell_fill = broker.get_order(broker.submit_order(_order(Side.SELL, qty=10, cid="s")))
    assert sell_fill.fees == Decimal("1000") * Decimal("5.0") / Decimal("10000")


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


# --- short / flip / realized P&L / marking ---------------------------------- #


def test_short_sell_from_flat() -> None:
    broker = _broker(bid="100", ask="100", cash="100000")
    broker.submit_order(_order(Side.SELL, qty=10))
    pos = broker.get_positions()[0]
    assert pos.quantity == -10
    assert pos.avg_price == Decimal("100")


def test_sell_more_than_held_flips_to_short() -> None:
    broker = _broker(bid="100", ask="100", cash="100000")
    broker.submit_order(_order(Side.BUY, qty=5, cid="b"))
    broker.submit_order(_order(Side.SELL, qty=8, cid="s"))  # flip through zero
    pos = broker.get_positions()[0]
    assert pos.quantity == -3
    assert pos.avg_price == Decimal("100")  # basis resets to the new side's price


def test_full_sell_drops_position() -> None:
    broker = _broker(bid="100", ask="100", cash="100000")
    broker.submit_order(_order(Side.BUY, qty=10, cid="b"))
    broker.submit_order(_order(Side.SELL, qty=10, cid="s"))
    assert broker.get_positions() == []


def _two_quote_broker(p1: str, p2: str) -> tuple[SimBroker, FakeClock]:
    q1 = Quote("AAPL", NOW, Decimal(p1), Decimal(p1), Decimal(p1), 1000)
    q2 = Quote("AAPL", NOW + timedelta(hours=1), Decimal(p2), Decimal(p2), Decimal(p2), 1000)
    clock = FakeClock(NOW)
    data = FakeMarketDataProvider(quotes={"AAPL": [q1, q2]})
    return SimBroker(data, clock, starting_cash=Decimal("100000")), clock


def test_equity_reflects_realized_pnl() -> None:
    broker, clock = _two_quote_broker("100", "120")
    broker.submit_order(_order(Side.BUY, qty=10, cid="b"))  # -1000
    clock.advance(timedelta(hours=1))
    broker.submit_order(_order(Side.SELL, qty=10, cid="s"))  # +1200 (sell @ 120)
    assert broker.get_positions() == []
    acct = broker.get_account()
    assert acct.cash == Decimal("100200")  # +200 realized
    assert acct.equity == Decimal("100200")


def test_marks_to_current_quote_not_last_fill() -> None:
    broker, clock = _two_quote_broker("100", "110")
    broker.submit_order(_order(Side.BUY, qty=10))  # filled @ 100
    clock.advance(timedelta(hours=1))  # price moved to 110, no new fill
    pos = broker.get_positions()[0]
    assert pos.market_value == Decimal("1100")  # marked to current 110, not fill 100
    assert broker.get_account().equity == Decimal("100100")


def test_negative_fill_price_is_atomic() -> None:
    broker = _broker(
        bid="100", ask="100", cash="100000", slippage=SlippageModel("fixed", Decimal("200"))
    )
    with pytest.raises(ValueError):
        broker.submit_order(_order(Side.SELL, qty=1))
    assert broker.get_account().cash == Decimal("100000")  # no state mutated
    assert broker.get_positions() == []


def test_models_from_config() -> None:
    from trader.config.models import FeesModelConfig, SlippageModelConfig

    s = SlippageModel.from_config(SlippageModelConfig(type="bps", value=10.0))
    assert s.kind == "bps"
    assert s.value == Decimal("10.0")
    f = FeesModel.from_config(FeesModelConfig(commission=Decimal("1"), regulatory_bps=5.0))
    assert f.commission == Decimal("1")
    assert f.regulatory_bps == 5.0


def test_unknown_slippage_kind_rejected() -> None:
    with pytest.raises(ValueError, match="unknown slippage kind"):
        SlippageModel("bogus", Decimal("1"))
