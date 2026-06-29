"""M4.7 paper-pipeline integration: quotes -> strategy -> risk gate -> SimBroker fill ->
attribution -> durable audit chain, end to end, with no real order."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

import trader.strategy  # noqa: F401 - registers built-in strategies (threshold)
from fakes import FakeClock, FakeMarketDataProvider
from trader.app.cli import app
from trader.broker import SimBroker
from trader.clock.virtual import VirtualClock
from trader.config.models import ExecutionConfig, RiskConfig, ScheduleConfig
from trader.core import Account, Decision, MarketSnapshot, Position, Quote
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.core.types import SlotSpec, StrategyBinding
from trader.observability.alerting import AlertEvent, AlertKind
from trader.observability.heartbeat import Heartbeat
from trader.orchestrator.cycle import Orchestrator, SqliteAuditSink
from trader.orchestrator.lock import NullLock
from trader.risk.gate import RiskManager
from trader.scheduler.calendar import TradingCalendar
from trader.scheduler.daemon import SchedulerDaemon
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.ledger import FiredSlotLedger
from trader.state.migrate import run_migrations

SESSION_NOW = datetime(2024, 7, 8, 14, 0, tzinfo=UTC)  # 10:00 ET on a trading session

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)
runner = CliRunner()


def _quote(symbol: str = "AAPL") -> Quote:
    p = Decimal("100")
    return Quote(symbol, NOW, p, p, p, 100_000, prev_close=p)


class _AlwaysBuy:
    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return [Decision(Action.BUY, "AAPL", 10)]


def test_end_to_end_paper_cycle(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    clock = FakeClock(NOW)
    data = FakeMarketDataProvider(quotes={"AAPL": [_quote()]})
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))  # PAPER: simulated fills
    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=NullLock(),
        attribution=AttributionLedger(conn),
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig()),
        risk=RiskManager(account_config=RiskConfig(), clock=VirtualClock(NOW)),
        audit=SqliteAuditSink(conn),
    )

    result = orch.run_cycle(_AlwaysBuy(), ["AAPL"], "momentum", NOW)

    # A SimBroker fill occurred (no real order anywhere in the path).
    assert len(result.fills) == 1 and result.fills[0].symbol == "AAPL"
    positions = {p.symbol: p.quantity for p in broker.get_positions()}
    assert positions["AAPL"] == 10

    # The durable audit chain is persisted, correlated by cycle_id, with the full chain.
    rows = conn.execute(
        "SELECT kind, payload FROM audit_log WHERE cycle_id = ? ORDER BY id",
        (result.cycle_id,),
    ).fetchall()
    kinds = [r[0] for r in rows]
    assert kinds == ["order_pending", "fill"]
    pending_payload = json.loads(rows[0][1])
    assert pending_payload["symbol"] == "AAPL" and pending_payload["quantity"] == 10
    fill_payload = json.loads(rows[1][1])
    assert fill_payload["symbol"] == "AAPL" and fill_payload["status"] == "FILLED"


def test_risk_rejection_recorded_in_audit(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    clock = FakeClock(NOW)
    data = FakeMarketDataProvider(quotes={"AAPL": [_quote()]})
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))
    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=NullLock(),
        attribution=AttributionLedger(conn),
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig()),
        risk=RiskManager(account_config=RiskConfig(denylist=("AAPL",)), clock=VirtualClock(NOW)),
        audit=SqliteAuditSink(conn),
    )

    result = orch.run_cycle(_AlwaysBuy(), ["AAPL"], "momentum", NOW)

    assert result.fills == [] and broker.get_positions() == []  # denylisted -> no fill
    rows = conn.execute(
        "SELECT kind, payload FROM audit_log WHERE cycle_id = ?", (result.cycle_id,)
    ).fetchall()
    assert [r[0] for r in rows] == ["rejected"]
    assert "denylist" in json.loads(rows[0][1])["reason"]


def _write_config(path: Path, mode: str, db_path: Path) -> None:
    path.write_text(
        f"""
mode: {mode}
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


def test_run_refuses_live_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write_config(cfg, "live", tmp_path / "state.sqlite")
    result = runner.invoke(app, ["run", "--config", str(cfg)])
    assert result.exit_code != 0
    assert "live mode is refused" in result.output


# --- daemon-level wiring (heartbeat + audit through the real fire path) ------ #


class _RecAlerter:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def alert(self, event: AlertEvent) -> None:
        self.events.append(event)


def _dip_quote() -> Quote:
    # last < prev_close*(1-band) so the threshold strategy emits a BUY.
    return Quote(
        "AAPL",
        SESSION_NOW,
        Decimal("97"),
        Decimal("96.95"),
        Decimal("97.05"),
        100_000,
        prev_close=Decimal("100"),
    )


def _paper_daemon(
    tmp_path: Path,
) -> tuple[SchedulerDaemon, SimBroker, object, Heartbeat, _RecAlerter]:
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    clock = FakeClock(SESSION_NOW)
    data = FakeMarketDataProvider(quotes={"AAPL": [_dip_quote()]})
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))
    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=NullLock(),
        attribution=AttributionLedger(conn),
        sizer=lambda d, sid: size_decision(d, sid, ExecutionConfig()),
        risk=RiskManager(account_config=RiskConfig(), clock=VirtualClock(SESSION_NOW)),
        audit=SqliteAuditSink(conn),
    )
    rec = _RecAlerter()
    heartbeat = Heartbeat(conn, clock=clock, max_age_seconds=120, alerter=rec)
    binding = StrategyBinding(
        strategy_id="momentum",
        strategy_name="threshold",
        params={"band": 0.02, "lot": 10},
        universe=("AAPL",),
        slots=(SlotSpec(slot_id="open", at=time(10, 0), drift_max_minutes=0),),
    )
    daemon = SchedulerDaemon(
        bindings=[binding],
        schedule=ScheduleConfig(base_seed=42),
        calendar=TradingCalendar(),
        ledger=FiredSlotLedger(conn),
        orchestrator=orch,
        clock=clock,
        alerter=rec,
        heartbeat=heartbeat,
        sleep=lambda _s: None,  # never block on jitter
    )
    return daemon, broker, conn, heartbeat, rec


def test_daemon_fire_writes_audit_chain_and_fills(tmp_path: Path) -> None:
    daemon, broker, conn, _heartbeat, rec = _paper_daemon(tmp_path)
    result = daemon.fire("momentum", "open")
    assert result is not None and len(result.fills) == 1  # threshold dip -> BUY -> SimBroker fill
    assert {p.symbol: p.quantity for p in broker.get_positions()} == {"AAPL": 10}
    rows = conn.execute(
        "SELECT kind FROM audit_log WHERE cycle_id = ? ORDER BY id", (result.cycle_id,)
    ).fetchall()
    assert [r[0] for r in rows] == ["order_pending", "fill"]
    # No spurious reconcile alert in the paper path (startup reconcile is M5, design note).
    assert all(e.kind is not AlertKind.RECONCILE_MISMATCH for e in rec.events)


def test_daemon_beat_touches_heartbeat(tmp_path: Path) -> None:
    daemon, _broker, _conn, heartbeat, _rec = _paper_daemon(tmp_path)
    daemon._beat()  # the dedicated 'heartbeat' executor calls this in production
    assert heartbeat.is_alive() is True
