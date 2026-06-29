"""Tests for the backtest Portfolio: realized/unrealized P&L, equity curve, the
equity invariant, fees, average cost, and shorts (M2.7)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.backtest import Portfolio
from trader.core import Fill, Quote
from trader.core.enums import OrderStatus, Side

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _fill(symbol: str, qty: int, price: str, *, fees: str = "0") -> Fill:
    return Fill(
        client_order_id="c",
        broker_order_id="b",
        symbol=symbol,
        quantity=qty,
        price=Decimal(price),
        fees=Decimal(fees),
        ts=NOW,
        status=OrderStatus.FILLED,
    )


def _quote(symbol: str, price: str) -> Quote:
    p = Decimal(price)
    return Quote(symbol=symbol, ts=NOW, last=p, bid=p, ask=p, volume=1000)


def test_realized_pnl_on_close() -> None:
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "10"), Side.BUY)
    p.apply_fill(_fill("AAPL", 10, "12"), Side.SELL)
    assert p.realized_pnl() == Decimal("20")  # +2 * 10
    assert p.positions() == {}  # flat


def test_unrealized_marks_to_market() -> None:
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "10"), Side.BUY)
    p.mark_to_market({"AAPL": _quote("AAPL", "12")})
    assert p.unrealized_pnl() == Decimal("20")
    assert p.realized_pnl() == Decimal("0")


def test_equity_snapshot_series_grows() -> None:
    p = Portfolio(Decimal("100000"))
    p.snapshot(NOW)
    p.apply_fill(_fill("AAPL", 10, "10"), Side.BUY)
    p.snapshot(NOW + timedelta(days=1))
    series = p.equity_series()
    assert len(series) == 2
    assert series[0][0] == NOW
    assert series[1][1] == p.equity()


def test_equity_invariant_holds() -> None:
    # equity == starting + realized + unrealized, at all times
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "10", fees="1"), Side.BUY)
    p.apply_fill(_fill("AAPL", 4, "13", fees="1"), Side.SELL)
    p.mark_to_market({"AAPL": _quote("AAPL", "11")})
    assert p.equity() == Decimal("100000") + p.realized_pnl() + p.unrealized_pnl()


def test_fees_reduce_realized_and_cash() -> None:
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "10", fees="2"), Side.BUY)
    p.apply_fill(_fill("AAPL", 10, "12", fees="3"), Side.SELL)
    assert p.realized_pnl() == Decimal("20") - Decimal("5")  # net of both fees
    assert p.total_fees() == Decimal("5")
    assert p.cash() == Decimal("100000") + Decimal("20") - Decimal("5")  # flat -> cash == equity
    assert p.equity() == p.cash()


def test_weighted_average_cost() -> None:
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "10"), Side.BUY)
    p.apply_fill(_fill("AAPL", 30, "14"), Side.BUY)  # avg = (100 + 420)/40 = 13
    qty, avg = p.positions()["AAPL"]
    assert qty == 40
    assert avg == Decimal("13")


def test_short_realized_pnl() -> None:
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "12"), Side.SELL)  # open short @ 12
    p.apply_fill(_fill("AAPL", 10, "10"), Side.BUY)  # cover @ 10 -> +2 * 10
    assert p.realized_pnl() == Decimal("20")
    assert p.positions() == {}


def test_partial_close_keeps_basis() -> None:
    p = Portfolio(Decimal("100000"))
    p.apply_fill(_fill("AAPL", 10, "10"), Side.BUY)
    p.apply_fill(_fill("AAPL", 4, "15"), Side.SELL)  # realize 4 * 5 = 20
    qty, avg = p.positions()["AAPL"]
    assert qty == 6
    assert avg == Decimal("10")  # basis unchanged on a reduction
    assert p.realized_pnl() == Decimal("20")


def test_zero_quantity_fill_is_noop() -> None:
    p = Portfolio(Decimal("100000"))
    working = Fill("c", "b", "AAPL", 0, Decimal("0"), Decimal("0"), NOW, OrderStatus.WORKING)
    p.apply_fill(working, Side.BUY)
    assert p.cash() == Decimal("100000")
    assert p.positions() == {}
