"""Tests for BacktestReport.build: report fields + metrics + volatile stripping (M2.10)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.backtest.report import BacktestReport, strip_volatile
from trader.core import Fill
from trader.core.enums import OrderStatus

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


def _fill(symbol: str, qty: int, price: str, *, fees: str = "0") -> Fill:
    return Fill("c", "b", symbol, qty, Decimal(price), Decimal(fees), NOW, OrderStatus.FILLED)


def _manifest() -> dict[str, object]:
    return {
        "config_hash": "cfg",
        "data_hashes": {"AAPL": "d"},
        "seed": 7,
        "git_commit": "abc123",
        "python_version": "3.12.13",
        "lib_versions": {"numpy": "2.5.0"},
    }


def _curve() -> list[tuple[datetime, Decimal]]:
    return [
        (NOW, Decimal("100000")),
        (NOW + timedelta(days=1), Decimal("100500")),
        (NOW + timedelta(days=2), Decimal("100200")),  # a dip -> drawdown
        (NOW + timedelta(days=3), Decimal("101000")),
    ]


def test_report_fields() -> None:
    report = BacktestReport.build([_fill("AAPL", 10, "100", fees="1")], _curve(), _manifest())
    assert set(report) == {"manifest", "summary", "equity_curve", "blotter"}
    summary = report["summary"]
    for key in ("num_trades", "max_drawdown", "hit_rate", "turnover", "total_return", "total_fees"):
        assert key in summary
    assert len(report["equity_curve"]) == 4
    assert report["blotter"][0]["symbol"] == "AAPL"
    assert report["blotter"][0]["price"] == "100"  # money as exact string


def _q(value: Decimal) -> Decimal:
    from decimal import ROUND_HALF_UP

    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def test_summary_metrics() -> None:
    report = BacktestReport.build([_fill("AAPL", 10, "100", fees="2")], _curve(), _manifest())
    summary = report["summary"]
    assert summary["num_trades"] == 1
    assert summary["starting_equity"] == "100000"
    assert summary["ending_equity"] == "101000"
    # max drawdown = (100500 - 100200)/100500, quantized to 8dp
    assert summary["max_drawdown"] == str(_q(Decimal("300") / Decimal("100500")))
    assert summary["total_fees"] == "2"  # money kept exact (not quantized)
    assert summary["turnover"] == str(_q(Decimal("1000") / Decimal("100000")))
    assert summary["total_return"] == str(_q(Decimal("1000") / Decimal("100000")))


def test_losing_run_negative_return() -> None:
    curve = [
        (NOW, Decimal("100000")),
        (NOW + timedelta(days=1), Decimal("95000")),  # drop
    ]
    summary = BacktestReport.build([_fill("AAPL", 10, "100")], curve, _manifest())["summary"]
    assert summary["total_return"].startswith("-")  # negative return
    assert summary["max_drawdown"] == str(_q(Decimal("5000") / Decimal("100000")))


def test_multi_symbol_blotter_preserves_order() -> None:
    fills = [_fill("AAPL", 1, "100"), _fill("MSFT", 1, "200"), _fill("AAPL", 1, "101")]
    blotter = BacktestReport.build(fills, _curve(), _manifest())["blotter"]
    assert [row["symbol"] for row in blotter] == ["AAPL", "MSFT", "AAPL"]  # input order kept


def test_empty_run_is_safe() -> None:
    report = BacktestReport.build([], [], _manifest())
    assert report["summary"]["num_trades"] == 0
    assert report["summary"]["max_drawdown"] == "0"
    assert report["equity_curve"] == []


def test_strip_volatile_removes_env_fields() -> None:
    report = BacktestReport.build([], _curve(), _manifest())
    stripped = strip_volatile(report)
    assert "git_commit" not in stripped["manifest"]
    assert "lib_versions" not in stripped["manifest"]
    assert "python_version" not in stripped["manifest"]
    # portable, result-affecting fields are kept
    assert stripped["manifest"]["config_hash"] == "cfg"
    assert stripped["manifest"]["data_hashes"] == {"AAPL": "d"}
    assert report["manifest"]["git_commit"] == "abc123"  # original untouched
