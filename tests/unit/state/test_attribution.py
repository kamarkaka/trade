"""Tests for the per-strategy attribution ledger (M3.9b)."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trader.core import Fill, Position
from trader.core.enums import OrderStatus, Side
from trader.state.attribution import UNKNOWN, AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2024, 7, 8, 14, 30, tzinfo=UTC)


def _ledger(tmp_path: Path) -> AttributionLedger:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return AttributionLedger(conn)


def _fill(symbol: str, qty: int, price: str) -> Fill:
    return Fill("c", "b", symbol, qty, Decimal(price), Decimal("0"), NOW, OrderStatus.FILLED)


def test_apply_attributes_fill(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 10, "100"), "momentum", Side.BUY)
    pos = ledger.get_attributed("momentum")
    assert pos == [type(pos[0])("momentum", "AAPL", 10, Decimal("100"))]


def test_apply_weighted_average(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 10, "100"), "m", Side.BUY)
    ledger.apply(_fill("AAPL", 30, "140"), "m", Side.BUY)  # avg = (1000+4200)/40 = 130
    assert ledger.get_attributed("m")[0].avg_price == Decimal("130")
    assert ledger.get_attributed("m")[0].quantity == 40


def test_reduce_to_flat_removes_row(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 10, "100"), "m", Side.BUY)
    ledger.apply(_fill("AAPL", 10, "120"), "m", Side.SELL)
    assert ledger.get_attributed("m") == []  # flat -> no row


def test_independent_strategies_same_symbol(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 10, "100"), "momentum", Side.BUY)
    ledger.apply(_fill("AAPL", 5, "100"), "meanrev", Side.SELL)
    assert ledger.get_attributed("momentum")[0].quantity == 10
    assert ledger.get_attributed("meanrev")[0].quantity == -5  # separate sub-positions


def test_reconcile_parks_unattributed_delta(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 6, "100"), "momentum", Side.BUY)  # attributed 6
    broker = [Position("AAPL", 10, Decimal("100"), Decimal("1000"))]  # broker holds 10
    parked = ledger.reconcile_total(broker)
    assert parked == [type(parked[0])(UNKNOWN, "AAPL", 4, Decimal("100"))]  # 10 - 6 = 4
    assert ledger.get_attributed(UNKNOWN)[0].quantity == 4
    # idempotent: re-running now ties out (unknown counted) -> nothing new parked
    assert ledger.reconcile_total(broker) == []
