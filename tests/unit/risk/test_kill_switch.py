"""Kill switch: engage/release, persistence across restart, idempotent alerting, auto-trip
on daily loss, and enforcement (the gate rejects all orders when engaged) (M5.4)."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from trader.app.cli import app
from trader.clock.virtual import VirtualClock
from trader.config.models import RiskConfig
from trader.core import Account, DayState, Order, Quote
from trader.core.enums import OrderType, Side
from trader.observability.alerting import AlertEvent, AlertKind
from trader.risk import rules
from trader.risk.gate import RiskManager
from trader.risk.kill_switch import KillSwitch
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)
ACCOUNT = Account(cash=Decimal("100000"), buying_power=Decimal("100000"), equity=Decimal("100000"))
QUOTE = Quote("AAPL", NOW, Decimal("100"), Decimal("99.5"), Decimal("100.5"), 1000)
runner = CliRunner()


def _day(*, killed: bool = False, loss: str = "0") -> DayState:
    return DayState(
        date(2026, 6, 29),
        Decimal("100000"),
        Decimal("0"),
        Decimal("0"),
        0,
        Decimal(loss),
        kill_switch_engaged=killed,
    )


class _RecAlerter:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def alert(self, event: AlertEvent) -> None:
        self.events.append(event)


def _switch(tmp_path: Path, alerter: object = None) -> tuple[KillSwitch, object]:
    conn = connect(tmp_path / "s.sqlite")
    run_migrations(conn)
    return KillSwitch(conn, now=lambda: NOW, alerter=alerter), conn  # type: ignore[arg-type]


# --- primitive -------------------------------------------------------------- #


def test_engage_disengage_round_trip(tmp_path: Path) -> None:
    switch, _ = _switch(tmp_path)
    assert switch.is_engaged() is False
    assert switch.engage("manual halt", source="cli") is True
    assert switch.is_engaged() is True
    state = switch.state()
    assert state.reason == "manual halt" and state.source == "cli"
    switch.disengage(source="cli")
    assert switch.is_engaged() is False


def test_persists_across_restart(tmp_path: Path) -> None:
    switch, _ = _switch(tmp_path)
    switch.engage("halt", source="cli")
    # Re-open the DB (simulate a restart) -> the flag survives.
    conn2 = connect(tmp_path / "s.sqlite")
    assert KillSwitch(conn2, now=lambda: NOW).is_engaged() is True


def test_engage_idempotent_alerts_once(tmp_path: Path) -> None:
    alerter = _RecAlerter()
    switch, _ = _switch(tmp_path, alerter)
    assert switch.engage("first", source="auto") is True
    assert switch.engage("second", source="auto") is False  # already engaged -> no-op
    assert len(alerter.events) == 1 and alerter.events[0].kind is AlertKind.KILL_SWITCH


def test_auto_trip_on_daily_loss(tmp_path: Path) -> None:
    alerter = _RecAlerter()
    switch, _ = _switch(tmp_path, alerter)
    cfg = RiskConfig(daily_loss_limit_pct=2.0)  # limit = 2% of 100k = 2000
    assert switch.maybe_trip_on_daily_loss(_day(loss="1500"), cfg) is False  # under limit
    assert switch.is_engaged() is False
    assert switch.maybe_trip_on_daily_loss(_day(loss="2500"), cfg) is True  # breach -> trip
    assert switch.is_engaged() is True
    assert alerter.events[0].kind is AlertKind.KILL_SWITCH


# --- enforcement ------------------------------------------------------------ #


def test_rule_engaged_blocks_new_orders() -> None:
    ctx = rules.RuleContext(RiskConfig(), (), ACCOUNT, QUOTE, _day(killed=True), NOW)
    order = Order("c1", "s1", "AAPL", Side.BUY, 10, OrderType.MARKET)
    assert rules.kill_switch(order, ctx).ok is False


def test_gate_checked_pre_submit_when_engaged() -> None:
    gate = RiskManager(account_config=RiskConfig(), clock=VirtualClock(NOW))
    order = Order("c1", "s1", "AAPL", Side.BUY, 10, OrderType.MARKET)
    verdict = gate.check(order, (), ACCOUNT, QUOTE, _day(killed=True))
    assert verdict.approved is False
    assert any("kill switch" in r for r in verdict.reasons)
    # a de-risking SELL is ALSO halted (auto-flatten is off; exit manually if needed)
    sell = Order("c2", "s1", "AAPL", Side.SELL, 5, OrderType.MARKET)
    assert gate.check(sell, (), ACCOUNT, QUOTE, _day(killed=True)).approved is False


# --- end-to-end: engaged switch halts a real orchestrator cycle ------------- #


def test_engaged_switch_halts_orchestrator_cycle(tmp_path: Path) -> None:
    # The acceptance criterion: engage the PERSISTED switch, run a cycle through the real
    # orchestrator (reading the switch each cycle), assert NO order is placed.
    import itertools
    from collections.abc import Sequence

    from fakes import FakeBroker, FakeClock, FakeMarketDataProvider
    from trader.config.models import ExecutionConfig
    from trader.core import Decision, Position
    from trader.core.enums import Action
    from trader.orchestrator.cycle import Orchestrator
    from trader.orchestrator.lock import NullLock
    from trader.sizing.sizer import size_decision
    from trader.state.attribution import AttributionLedger

    switch, conn = _switch(tmp_path)
    switch.engage("halt", source="cli")  # persisted ON

    class _AlwaysBuy:
        def decide(self, *a: object, **k: object) -> Sequence[Decision]:
            return [Decision(Action.BUY, "AAPL", 10)]

    ids = (f"o{i}" for i in itertools.count())
    broker = FakeBroker()
    orch = Orchestrator(
        broker=broker,
        data=FakeMarketDataProvider(quotes={"AAPL": [QUOTE]}),
        clock=FakeClock(NOW),
        cycle_lock=NullLock(),
        attribution=AttributionLedger(conn),  # type: ignore[arg-type]
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig(), id_factory=lambda: next(ids)),
        kill_switch=switch.is_engaged,  # the daemon wires this in `trader run`
    )
    result = orch.run_cycle(_AlwaysBuy(), ["AAPL"], "m", NOW)
    assert result.halted is True
    assert broker.submitted == [] and result.orders == []  # nothing placed while engaged
    _ = Position  # keep import used


# --- CLI -------------------------------------------------------------------- #


def _write_config(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""
mode: paper
strategies:
  - id: momentum
    name: threshold
    universe: [AAPL]
    slots:
      - {{id: open, time: "09:45"}}
observability:
  db_path: "{db_path}"
""",
        encoding="utf-8",
    )


def test_cli_kill_on_off(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    cfg = tmp_path / "c.yaml"
    _write_config(cfg, db)
    on = runner.invoke(app, ["kill", "--on", "--reason", "boom", "--config", str(cfg)])
    assert on.exit_code == 0 and "ENGAGED" in on.output
    assert KillSwitch(connect(db)).is_engaged() is True  # persisted
    off = runner.invoke(app, ["kill", "--off", "--config", str(cfg)])
    assert off.exit_code == 0 and "released" in off.output
    assert KillSwitch(connect(db)).is_engaged() is False
