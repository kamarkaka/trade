"""Tests for position reconciliation: broker vs attribution, the acknowledged baseline (the
'unknown' bucket, moved only by an explicit accept), clean state (M4.1, LR10)."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeBroker
from trader.core import Fill, Position
from trader.core.enums import OrderStatus, Side
from trader.execution.reconcile import accept, reconcile
from trader.state.attribution import UNKNOWN, AttributionLedger, BaselineChange
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


def _entry(report, symbol: str):  # type: ignore[no-untyped-def]
    (entry,) = [d for d in report.discrepancies + report.standing if d.symbol == symbol]
    return (entry.broker_qty, entry.attributed_qty, entry.baseline_qty, entry.unattributed_qty)


def test_an_unattributed_position_is_a_discrepancy_and_nothing_is_written(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # broker holds 10; nothing attributed locally
    report = reconcile(broker, attribution)
    assert not report.is_clean and report.requires_attention
    assert [d.symbol for d in report.discrepancies] == ["AAPL"]
    assert _entry(report, "AAPL") == (10, 0, 0, 10)  # broker, attributed, baseline, unattributed
    assert attribution.get_attributed(UNKNOWN) == []  # reconcile never writes the baseline


def test_the_unattributed_quantity_is_broker_minus_attributed(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)  # attributed 6
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # broker holds 10 -> 4 unexplained
    assert _entry(reconcile(broker, attribution), "AAPL") == (10, 6, 0, 4)


def test_attributed_exceeds_broker_reports_negative_delta(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)  # attributed 6
    broker = FakeBroker()  # broker flat -> local claims a position the broker doesn't have
    report = reconcile(broker, attribution)
    assert _entry(report, "AAPL") == (0, 6, 0, -6) and report.requires_attention


def test_a_discrepancy_stays_one_until_it_is_accepted(tmp_path: Path) -> None:
    # A retry — or a restart — must never turn a change into "standing" by itself.
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))
    first = reconcile(broker, attribution)
    second = reconcile(broker, attribution)
    assert first.discrepancies == second.discrepancies and not second.is_clean
    assert accept(second, attribution) == [BaselineChange("AAPL", 0, 4)]
    accepted = reconcile(broker, attribution)
    assert accepted.is_clean and _entry(accepted, "AAPL") == (10, 6, 4, 4)
    assert attribution.get_attributed(UNKNOWN)[0].quantity == 4


def test_acknowledged_holdings_stand_but_any_change_does_not(tmp_path: Path) -> None:
    # The owner's own long-term holdings live in the same account: once acknowledged and
    # unchanged they are standing (clean); a share nobody recorded is a change (not clean).
    attribution = _attribution(tmp_path)
    broker = FakeBroker()
    broker.set_position(_position("VTI", 100))
    first_sight = reconcile(broker, attribution)
    assert not first_sight.is_clean
    accept(first_sight, attribution)  # the operator reviewed and acknowledged them
    confirmed = reconcile(broker, attribution)
    assert confirmed.is_clean and [d.symbol for d in confirmed.standing] == ["VTI"]
    broker.set_position(_position("VTI", 101))  # one untracked share appeared
    changed = reconcile(broker, attribution)
    assert not changed.is_clean and _entry(changed, "VTI") == (101, 0, 100, 101)


def test_an_acknowledged_holding_that_disappears_is_a_change(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    broker = FakeBroker()
    broker.set_position(_position("VTI", 100))
    accept(reconcile(broker, attribution), attribution)
    broker._positions.clear()  # sold elsewhere
    report = reconcile(broker, attribution)
    assert not report.is_clean and _entry(report, "VTI") == (0, 0, 100, 0)


def test_accepting_takes_the_snapshot_the_report_compared(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    broker = FakeBroker()
    broker.set_position(_position("VTI", 100))
    report = reconcile(broker, attribution)
    broker.set_position(_position("VTI", 150))  # moved after the operator's report
    assert accept(report, attribution) == [BaselineChange("VTI", 0, 100)]  # what was shown
    assert not reconcile(broker, attribution).is_clean  # the later move is still reported


def test_multi_symbol_reports_all_divergent(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 6)
    _buy(attribution, "momentum", "TSLA", 5)  # ties out below
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # over by 4
    broker.set_position(_position("MSFT", 3))  # absent locally
    broker.set_position(_position("TSLA", 5))  # clean
    report = reconcile(broker, attribution)
    unattributed = {d.symbol: d.unattributed_qty for d in report.discrepancies}
    assert unattributed == {"AAPL": 4, "MSFT": 3}  # both divergences reported, TSLA excluded


def test_clean_state_no_discrepancy(tmp_path: Path) -> None:
    attribution = _attribution(tmp_path)
    _buy(attribution, "momentum", "AAPL", 10)
    broker = FakeBroker()
    broker.set_position(_position("AAPL", 10))  # broker matches attribution exactly
    report = reconcile(broker, attribution)
    assert report.is_clean
    assert report.discrepancies == []
    assert attribution.get_attributed(UNKNOWN) == []  # nothing parked
