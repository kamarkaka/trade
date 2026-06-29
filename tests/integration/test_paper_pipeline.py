"""M4.7 paper-pipeline integration: quotes -> strategy -> risk gate -> SimBroker fill ->
attribution -> durable audit chain, end to end, with no real order."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from fakes import FakeClock, FakeMarketDataProvider
from trader.app.cli import app
from trader.broker import SimBroker
from trader.clock.virtual import VirtualClock
from trader.config.models import ExecutionConfig, RiskConfig
from trader.core import Account, Decision, MarketSnapshot, Position, Quote
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.orchestrator.cycle import Orchestrator, SqliteAuditSink
from trader.orchestrator.lock import NullLock
from trader.risk.gate import RiskManager
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations

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
