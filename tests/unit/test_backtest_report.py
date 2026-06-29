"""Tests for the M6.6 per-strategy + combined backtest report (HTML/JSON + manifest).

Feeds a synthetic, fully offline run_result (no engine, no network) and asserts the JSON
structure (combined + one block per strategy + manifest), zero-trade safety, HTML render,
and byte-for-byte JSON determinism (the property the M6.8 golden depends on).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trader.backtest.report import (
    BacktestReportDoc,
    BacktestRunResult,
    FireRecord,
    build_report,
)
from trader.core import Fill
from trader.core.enums import OrderStatus, Side

BASE = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)

MANIFEST = {
    "config_hash": "abc123",
    "data_hashes": {"AAPL": "h1", "MSFT": "h2"},
    "seed": 42,
    "git_commit": "deadbeef",
    "python_version": "3.12.0",
    "lib_versions": {"pandas": "2.2.0"},
}


def _fill(cid: str, sym: str, qty: int, price: str, day: int) -> Fill:
    return Fill(
        client_order_id=cid,
        broker_order_id=f"b-{cid}",
        symbol=sym,
        quantity=qty,
        price=Decimal(price),
        fees=Decimal("0.50"),
        ts=BASE + timedelta(days=day),
        status=OrderStatus.FILLED,
    )


def _curve(vals: list[str]) -> list[tuple[datetime, Decimal]]:
    return [(BASE + timedelta(days=i), Decimal(v)) for i, v in enumerate(vals)]


def _run_result(*, with_per_strategy_equity: bool = False) -> BacktestRunResult:
    trades = {
        "mr": [
            (_fill("mr-1", "AAPL", 10, "100", 0), Side.BUY),
            (_fill("mr-2", "AAPL", 10, "110", 1), Side.SELL),
        ],
        "mom": [
            (_fill("mom-1", "MSFT", 5, "200", 0), Side.BUY),
            (_fill("mom-2", "MSFT", 5, "190", 1), Side.SELL),
        ],
        "idle": [],  # zero-trade strategy still gets a section
    }
    fire_log = [
        FireRecord("mr", "open", BASE, 120, 7),
        FireRecord("mom", "open", BASE + timedelta(seconds=30), -45, 7),
        FireRecord("idle", "close", BASE + timedelta(hours=6), 0, None),
    ]
    per_strategy_equity = None
    if with_per_strategy_equity:
        per_strategy_equity = {
            "mr": _curve(["100000", "100100", "100100"]),
            "mom": _curve(["100000", "99950", "99950"]),
            "idle": _curve(["100000", "100000", "100000"]),
        }
    return BacktestRunResult(
        combined_equity_curve=_curve(["100000", "100050", "100050"]),
        per_strategy_trades=trades,
        fire_log=fire_log,
        per_strategy_equity=per_strategy_equity,
    )


def test_report_json_has_combined_and_per_strategy() -> None:
    doc = build_report(_run_result(), MANIFEST)
    data = doc.data
    assert "combined" in data and "per_strategy" in data
    assert set(data["per_strategy"]) == {"mr", "mom", "idle"}
    # combined metrics + blotter present
    assert data["combined"]["num_trades"] == 4
    assert len(data["combined"]["blotter"]) == 4
    assert data["combined"]["total_return"] == "0.00050000"  # (100050-100000)/100000


def test_report_includes_manifest() -> None:
    data = build_report(_run_result(), MANIFEST).data
    m = data["manifest"]
    assert m["config_hash"] == "abc123"
    assert m["data_hashes"] == {"AAPL": "h1", "MSFT": "h2"}
    assert m["git_commit"] == "deadbeef"
    assert m["seed"] == 42


def test_per_strategy_trade_metrics() -> None:
    data = build_report(_run_result(), MANIFEST).data
    mr = data["per_strategy"]["mr"]
    # mr: buy 10@100 / sell 10@110 -> realized +100, minus 2*0.50 fees = +99.00
    assert mr["num_trades"] == 2
    assert mr["realized_pnl"] == "99.00"
    assert mr["hit_rate"] == "1.00000000"  # one winning round trip
    assert mr["equity_metrics"] is None  # no per-strategy equity supplied
    assert len(mr["fire_log"]) == 1 and mr["fire_log"][0]["drift_seconds"] == 120
    mom = data["per_strategy"]["mom"]
    assert mom["hit_rate"] == "0.00000000"  # one losing round trip


def test_per_strategy_equity_metrics_when_supplied() -> None:
    data = build_report(_run_result(with_per_strategy_equity=True), MANIFEST).data
    mr = data["per_strategy"]["mr"]
    assert mr["equity_metrics"] is not None
    assert mr["equity_metrics"]["total_return"] == "0.00100000"  # 100000 -> 100100
    assert len(mr["equity_curve"]) == 3


def test_zero_trade_strategy_section() -> None:
    data = build_report(_run_result(), MANIFEST).data
    idle = data["per_strategy"]["idle"]
    assert idle["num_trades"] == 0
    assert idle["realized_pnl"] == "0"
    assert idle["hit_rate"] is None  # nothing closed -> None
    assert idle["blotter"] == []


def test_html_renders() -> None:
    html = build_report(_run_result(with_per_strategy_equity=True), MANIFEST).html_str()
    assert html.startswith("<!DOCTYPE html>")
    assert "Combined portfolio" in html
    for sid in ("mr", "mom", "idle"):
        assert sid in html
    assert "abc123" in html  # manifest config_hash rendered


def test_json_is_deterministic() -> None:
    a = build_report(_run_result(with_per_strategy_equity=True), MANIFEST).json_str()
    b = build_report(_run_result(with_per_strategy_equity=True), MANIFEST).json_str()
    assert a == b
    assert a.endswith("\n")
    # sorted keys: 'combined' sorts before 'manifest' before 'per_strategy'
    assert a.index('"combined"') < a.index('"manifest"') < a.index('"per_strategy"')


def test_json_deterministic_under_hostile_ambient_context() -> None:
    # The golden's byte-reproducibility must not depend on the caller's decimal context.
    import decimal

    baseline = build_report(_run_result(with_per_strategy_equity=True), MANIFEST).json_str()
    for ctx in (decimal.Context(prec=3), decimal.Context(prec=50, rounding=decimal.ROUND_FLOOR)):
        with decimal.localcontext(ctx):
            assert build_report(
                _run_result(with_per_strategy_equity=True), MANIFEST
            ).json_str() == (baseline)


def test_combined_blotter_tie_break_is_order_independent() -> None:
    # Two fills at the SAME timestamp across strategies must sort deterministically,
    # independent of the per_strategy_trades dict insertion order (golden contract).
    f_a = (_fill("a-1", "AAA", 1, "10", 0), Side.BUY)
    f_z = (_fill("z-1", "ZZZ", 1, "20", 0), Side.BUY)  # identical ts (day 0)
    curve = _curve(["100000", "100000"])
    forward = BacktestRunResult(curve, {"alpha": [f_a], "zeta": [f_z]})
    reverse = BacktestRunResult(curve, {"zeta": [f_z], "alpha": [f_a]})
    assert build_report(forward, MANIFEST).json_str() == build_report(reverse, MANIFEST).json_str()


def test_to_json_and_to_html_write_files(tmp_path) -> None:
    doc = build_report(_run_result(), MANIFEST)
    jp = tmp_path / "r.json"
    hp = tmp_path / "r.html"
    doc.to_json(jp)
    doc.to_html(hp)
    assert jp.read_text(encoding="utf-8") == doc.json_str()
    assert "<html" in hp.read_text(encoding="utf-8")
    assert isinstance(doc, BacktestReportDoc)
