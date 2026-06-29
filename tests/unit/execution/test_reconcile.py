"""Tests for the reconciliation engine: true-to-broker, unknown bucket, clean state (M4.1)."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeBroker
from trader.core import Fill, Position
from trader.core.enums import OrderStatus, Side
from trader.execution.reconcile import reconcile
from trader.state.attribution import UNKNOWN, AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2024, 7, 8, 14, 30, tzinfo=UTC)


def _attribution(tmp_path: Path) -> AttributionLedger:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return AttributionLedger(conn)


def _position(symbol: str, qty: int) -> Position:
    return Position(symbol, qty, Decimal("100"), Decimal(qty) * Decimal("100"))


def _buy(attribution: AttributionLedger, strategy_id: str, symbol: str, qty: int) -> None:
    fill = Fill("c", "b", symbol, qty, Decimal("100"), Decimal("0"), NOW, OrderStatus.FILLED)
    attribution.apply(fill, strategy_id, Side.BUY)


def test_local_trued_to_broker(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # broker holds 10; nothing attributed locally
    report = reconcile(broker, attribution)
    assert not report.is_clean
    assert report.requires_attention
    disc = report.discrepancies[0]
    assert (disc.symbol, disc.broker_qty, disc.attributed_qty, disc.parked_qty) == (
        "AAPL",
        10,
        0,
        10,
    )
    assert attribution.get_attributed(UNKNOWN)[0].quantity == 10  # adopted into 'unknown'


def test_unattributed_delta_parked_in_unknown(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)  # attributed 6
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # broker holds 10 -> +4 unexplained
    report = reconcile(broker, attribution)
    disc = report.discrepancies[0]
    assert (disc.broker_qty, disc.attributed_qty, disc.parked_qty) == (10, 6, 4)
    assert attribution.get_attributed(UNKNOWN)[0].quantity == 4


def test_attributed_exceeds_broker_reports_negative_delta(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)  # attributed 6
    broker = FakeBroker()  # broker flat -> local claims a position the broker doesn't have
    report = reconcile(broker, attribution)
    disc = report.discrepancies[0]
    assert (disc.broker_qty, disc.attributed_qty, disc.parked_qty) == (0, 6, -6)
    assert report.requires_attention


def test_reconcile_is_idempotent(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))
    first = reconcile(broker, attribution)
    second = reconcile(broker, attribution)  # unchanged broker -> same residual, no compounding
    assert first.discrepancies == second.discrepancies
    assert attribution.get_attributed(UNKNOWN)[0].quantity == 4  # not doubled to 8


def test_multi_symbol_reports_all_divergent(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)
    _buy(attribution, "momentum", "TSLA", 5)  # ties out below
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # over by 4
    broker.set_position(_position("MSFT", 3))  # absent locally
    broker.set_position(_position("TSLA", 5))  # clean
    report = reconcile(broker, attribution)
    parked = {d.symbol: d.parked_qty for d in report.discrepancies}
    assert parked == {"AAPL": 4, "MSFT": 3}  # both divergences reported, TSLA excluded


def test_clean_state_no_discrepancy(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 10)
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # broker matches attribution exactly
    report = reconcile(broker, attribution)
    assert report.is_clean
    assert report.discrepancies == []
    assert attribution.get_attributed(UNKNOWN) == []  # nothing parked
