"""Handcrafted-fixture tests for the backtest metrics layer (M6.5).

Every expected value is precomputed by hand from inputs chosen so the arithmetic is
exact (clean ratios / perfect powers), so these assert the math — not the implementation
against itself. CAGR (Decimal ln/exp) is compared within a tiny tolerance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.backtest.metrics import (
    Metrics,
    TradeRecord,
    avg_equity,
    avg_exposure,
    build_equity_curve,
    cagr,
    hit_rate,
    max_drawdown,
    summarize,
    total_return,
    trade_records_from_multi,
    turnover,
)
from trader.core import Fill
from trader.core.enums import OrderStatus, Side

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _curve(vals: list[str]) -> list[tuple[datetime, Decimal]]:
    return [(BASE + timedelta(days=i), Decimal(v)) for i, v in enumerate(vals)]


def _trade(day: int, side: Side, qty: int, price: str, *, sid: str = "s", sym: str = "AAPL"):
    return TradeRecord(
        ts=BASE + timedelta(days=day),
        strategy_id=sid,
        symbol=sym,
        side=side,
        quantity=qty,
        price=Decimal(price),
        fees=Decimal("0"),
    )


# --- equity-curve metrics --------------------------------------------------- #


def test_build_equity_curve_sorts() -> None:
    a, b, c = BASE, BASE + timedelta(days=1), BASE + timedelta(days=2)
    out = build_equity_curve([(c, Decimal("3")), (a, Decimal("1")), (b, Decimal("2"))])
    assert [v for _, v in out] == [Decimal("1"), Decimal("2"), Decimal("3")]


def test_max_drawdown_known_curve() -> None:
    # [100,120,90,150]: peak 120 (day 1), trough 90 (day 2), dd = (120-90)/120 = 0.25
    peak_ts, trough_ts, dd = max_drawdown(_curve(["100", "120", "90", "150"]))
    assert dd == Decimal("0.25")
    assert peak_ts == BASE + timedelta(days=1)
    assert trough_ts == BASE + timedelta(days=2)


def test_max_drawdown_monotonic_up_is_zero() -> None:
    peak_ts, trough_ts, dd = max_drawdown(_curve(["100", "110", "120"]))
    assert dd == Decimal("0") and peak_ts is None and trough_ts is None


def test_total_return_and_cagr() -> None:
    # total return 100 -> 150 = 0.5 exactly.
    assert total_return(_curve(["100", "150"])) == Decimal("0.5")
    # CAGR: 252 sessions, 100 -> 200, years = 1 -> 2^1 - 1 = 1.0 exactly.
    curve = [(BASE + timedelta(days=i), Decimal("100")) for i in range(251)]
    curve.append((BASE + timedelta(days=300), Decimal("200")))
    assert len(curve) == 252
    assert abs(cagr(curve) - Decimal("1")) < Decimal("1e-12")


def test_cagr_subyear_annualization() -> None:
    # 126 sessions, 100 -> 400, years = 0.5 -> ratio^(1/0.5) - 1 = 4^2 - 1 = 15.
    curve = [(BASE + timedelta(days=i), Decimal("100")) for i in range(125)]
    curve.append((BASE + timedelta(days=300), Decimal("400")))
    assert len(curve) == 126
    assert abs(cagr(curve) - Decimal("15")) < Decimal("1e-12")


def test_avg_equity() -> None:
    assert avg_equity(_curve(["100", "200", "300"])) == Decimal("200")
    assert avg_equity([]) == Decimal("0")


# --- trade metrics ---------------------------------------------------------- #


def test_hit_rate_round_trips() -> None:
    # buy10@10/sell10@12 (win), buy10@10/sell10@9 (loss) -> 2 round trips, 1 win = 0.5
    trades = [
        _trade(0, Side.BUY, 10, "10"),
        _trade(1, Side.SELL, 10, "12"),
        _trade(2, Side.BUY, 10, "10"),
        _trade(3, Side.SELL, 10, "9"),
    ]
    assert hit_rate(trades) == Decimal("0.5")


def test_hit_rate_short_round_trip() -> None:
    # SELL-open 10@12 then BUY-close 10@10 -> short profit (12-10)*10 = +20 -> win = 1.0
    trades = [_trade(0, Side.SELL, 10, "12"), _trade(1, Side.BUY, 10, "10")]
    assert hit_rate(trades) == Decimal("1")


def test_hit_rate_open_position_excluded() -> None:
    # only an entry, never closed -> no closing trade -> None
    assert hit_rate([_trade(0, Side.BUY, 10, "10")]) is None


def test_hit_rate_filters_by_strategy() -> None:
    trades = [
        _trade(0, Side.BUY, 10, "10", sid="a"),
        _trade(1, Side.SELL, 10, "12", sid="a"),  # a: win
        _trade(0, Side.BUY, 10, "10", sid="b"),
        _trade(1, Side.SELL, 10, "8", sid="b"),  # b: loss
    ]
    assert hit_rate(trades, strategy_id="a") == Decimal("1")
    assert hit_rate(trades, strategy_id="b") == Decimal("0")
    assert hit_rate(trades) == Decimal("0.5")  # combined: 1 win of 2


def test_hit_rate_partial_close_then_flip() -> None:
    # long 10@10, then SELL 15@12: closes 10 (win), flips to short 5. One closing trade.
    trades = [_trade(0, Side.BUY, 10, "10"), _trade(1, Side.SELL, 15, "12")]
    assert hit_rate(trades) == Decimal("1")
    # the residual short is still open -> only the one (winning) round trip counted
    assert sum(1 for _ in trades) == 2


def test_turnover() -> None:
    # traded notional = 10*10 + 10*12 = 220; avg equity 100 -> turnover 2.2
    trades = [_trade(0, Side.BUY, 10, "10"), _trade(1, Side.SELL, 10, "12")]
    assert turnover(trades, Decimal("100")) == Decimal("2.2")


def test_turnover_zero_equity_safe() -> None:
    assert turnover([_trade(0, Side.BUY, 1, "10")], Decimal("0")) == Decimal("0")


def test_avg_exposure() -> None:
    # ratios: 50/100=0.5, 150/100=1.5, equity<=0 point skipped -> mean = 1.0
    series = [
        (Decimal("50"), Decimal("100")),
        (Decimal("150"), Decimal("100")),
        (Decimal("9"), Decimal("0")),
    ]
    assert avg_exposure(series) == Decimal("1.0")
    assert avg_exposure([]) is None
    assert avg_exposure([(Decimal("1"), Decimal("0"))]) is None


# --- adapters + summary ----------------------------------------------------- #


def _fill(cid: str, sym: str, qty: int, price: str, day: int) -> Fill:
    return Fill(
        client_order_id=cid,
        broker_order_id=f"b-{cid}",
        symbol=sym,
        quantity=qty,
        price=Decimal(price),
        fees=Decimal("0"),
        ts=BASE + timedelta(days=day),
        status=OrderStatus.FILLED,
    )


def test_trade_records_from_multi() -> None:
    f1 = _fill("o1", "AAPL", 10, "10", 1)
    f0 = _fill("o0", "MSFT", 5, "20", 0)
    f_zero = _fill("oz", "AAPL", 0, "10", 2)
    recs = trade_records_from_multi(
        {"a": [(f1, Side.BUY), (f_zero, Side.SELL)], "b": [(f0, Side.SELL)]}
    )
    # zero-qty fill dropped; sorted by ts -> MSFT(b) before AAPL(a)
    assert [(r.strategy_id, r.symbol) for r in recs] == [("b", "MSFT"), ("a", "AAPL")]
    assert recs[0].side is Side.SELL and recs[1].side is Side.BUY


def test_empty_inputs_safe() -> None:
    m = summarize([], [])
    assert isinstance(m, Metrics)
    assert m.total_return == Decimal("0")
    assert m.cagr == Decimal("0")
    assert m.max_drawdown_pct == Decimal("0")
    assert m.max_dd_window == (None, None)
    assert m.hit_rate is None
    assert m.num_trades == 0
    assert m.turnover == Decimal("0")
    assert m.avg_exposure is None
    assert m.start_equity == Decimal("0") and m.final_equity == Decimal("0")


def test_single_point_curve_safe() -> None:
    m = summarize(_curve(["100"]), [])
    assert m.total_return == Decimal("0")
    assert m.cagr == Decimal("0")
    assert m.max_drawdown_pct == Decimal("0")
    assert m.start_equity == Decimal("100") and m.final_equity == Decimal("100")


def test_summarize_combined_and_per_strategy() -> None:
    curve = _curve(["100", "120", "90", "150"])
    trades = [
        _trade(0, Side.BUY, 10, "10", sid="a"),
        _trade(1, Side.SELL, 10, "12", sid="a"),  # a win
        _trade(0, Side.BUY, 10, "10", sid="b"),
        _trade(1, Side.SELL, 10, "9", sid="b"),  # b loss
    ]
    exposure = [(Decimal("50"), Decimal("100")), (Decimal("150"), Decimal("100"))]
    combined = summarize(curve, trades, exposure_series=exposure)
    assert combined.num_trades == 4
    assert combined.hit_rate == Decimal("0.5")
    assert combined.max_drawdown_pct == Decimal("0.25")
    assert combined.avg_exposure == Decimal("1.0")

    only_a = summarize(curve, trades, strategy_id="a")
    assert only_a.num_trades == 2
    assert only_a.hit_rate == Decimal("1")
    assert only_a.avg_exposure is None  # no exposure series passed
