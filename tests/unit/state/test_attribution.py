"""Tests for the per-strategy attribution ledger (M3.9b)."""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trader.core import Fill, Position
from trader.core.enums import OrderStatus, Side
from trader.state.attribution import (
    UNKNOWN,
    AttributedPosition,
    AttributionLedger,
    BaselineChange,
)
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


def _position(symbol: str, qty: int, avg: str = "100") -> Position:
    return Position(symbol, qty, Decimal(avg), Decimal(avg) * qty)


def test_unattributed_is_what_the_broker_holds_beyond_the_strategies(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 6, "100"), "momentum", Side.BUY)  # attributed 6
    ledger.apply(_fill("MSFT", 3, "200"), "m", Side.BUY)
    broker = [_position("AAPL", 10), _position("MSFT", 3, "200"), _position("VTI", 5)]
    assert ledger.unattributed(broker) == {"AAPL": 4, "VTI": 5}  # MSFT ties out
    assert ledger.unattributed([]) == {"AAPL": -6, "MSFT": -3}  # broker flat: negative
    assert ledger.get_attributed(UNKNOWN) == []  # read-only


def test_accepting_sets_the_baseline_and_reports_each_change(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.apply(_fill("AAPL", 6, "100"), "momentum", Side.BUY)  # attributed 6
    assert ledger.accept_unattributed([_position("AAPL", 10)]) == [BaselineChange("AAPL", 0, 4)]
    assert ledger.get_attributed(UNKNOWN) == [AttributedPosition(UNKNOWN, "AAPL", 4, Decimal(100))]
    assert ledger.accept_unattributed([_position("AAPL", 10)]) == []  # nothing more to change
    # the broker grew: the baseline becomes the new unattributed quantity, not 4 + drift
    assert ledger.accept_unattributed([_position("AAPL", 15)]) == [BaselineChange("AAPL", 4, 9)]
    # back to exactly the attribution: the baseline row goes
    assert ledger.accept_unattributed([_position("AAPL", 6)]) == [BaselineChange("AAPL", 9, 0)]
    assert ledger.get_attributed(UNKNOWN) == []


def test_accepting_is_all_or_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = _ledger(tmp_path)
    ledger.accept_unattributed([_position("AAPL", 10), _position("MSFT", 5, "200")])
    real_upsert = ledger._upsert
    writes = {"n": 0}

    def fail_on_the_second(*args: object) -> None:
        writes["n"] += 1
        if writes["n"] == 2:
            raise sqlite3.OperationalError("disk I/O error")
        real_upsert(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(ledger, "_upsert", fail_on_the_second)
    with pytest.raises(sqlite3.OperationalError):
        ledger.accept_unattributed([_position("AAPL", 12), _position("MSFT", 7, "200")])
    baseline = {p.symbol: p.quantity for p in ledger.get_attributed(UNKNOWN)}
    assert baseline == {"AAPL": 10, "MSFT": 5}  # the AAPL write was rolled back too
    assert not ledger.connection.in_transaction
