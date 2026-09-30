"""LR5 end to end in paper: quotes -> strategy -> real risk gate -> DurableOrderExecutor over
SimBroker -> durable orders/fills rows + attribution that ties out with the broker; a resting
limit order is cancelled, never left WORKING."""

import itertools
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeClock, FakeMarketDataProvider
from trader.broker import SimBroker
from trader.config.models import ExecutionConfig, RiskConfig
from trader.core import Account, Bar, Decision, MarketSnapshot, Position, Quote
from trader.core.enums import Action, OrderType
from trader.core.protocols import Clock, MarketDataProvider
from trader.execution.executor import DurableOrderExecutor, in_memory_reconciler
from trader.execution.idempotency import OrderRepository
from trader.execution.poller import PollPolicy
from trader.orchestrator.cycle import Orchestrator, SqliteAuditSink
from trader.orchestrator.lock import NullLock
from trader.risk.gate import RiskManager
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Decide:
    def __init__(self, decisions: Sequence[Decision]) -> None:
        self._decisions = decisions

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return self._decisions


def _pipeline(tmp_path: Path, order_type: OrderType = OrderType.MARKET):  # type: ignore[no-untyped-def]
    price = Decimal("100")
    quote = Quote("AAPL", NOW, price, price, price, 100_000, prev_close=price)
    bar = Bar("AAPL", NOW, price, Decimal("101"), Decimal("99"), price, 100_000)
    data = FakeMarketDataProvider(quotes={"AAPL": [quote]}, bars={"AAPL": [bar]})
    clock = FakeClock(NOW)
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))
    attribution = AttributionLedger(conn)
    executor = DurableOrderExecutor(
        broker=broker,
        repo=OrderRepository(conn),
        attribution=attribution,
        reconcile=in_memory_reconciler(broker.find_by_client_id),
        poll_policy=PollPolicy(timeout_seconds=0),  # what `trader run` uses for paper
    )
    ids = (f"o{i}" for i in itertools.count())
    exec_cfg = ExecutionConfig(order_type=order_type)
    orch = Orchestrator(
        broker=broker,
        data=data,
        clock=clock,
        cycle_lock=NullLock(),
        attribution=attribution,
        sizer=lambda d, sid: size_decision(d, sid, exec_cfg, id_factory=lambda: next(ids)),
        risk=RiskManager(account_config=RiskConfig(allowlist=("AAPL",)), clock=clock),
        audit=SqliteAuditSink(conn),
        executor=executor,
    )
    return orch, broker, conn, attribution


def test_paper_cycle_writes_durable_rows_that_tie_out(tmp_path: Path) -> None:
    orch, broker, conn, attribution = _pipeline(tmp_path)
    result = orch.run_cycle(_Decide([Decision(Action.BUY, "AAPL", 10)]), ["AAPL"], "s1", NOW)
    assert result.errors == [] and [f.quantity for f in result.fills] == [10]
    order_row = conn.execute("SELECT status, broker_order_id FROM orders").fetchone()
    assert tuple(order_row) == ("FILLED", "SIM-1")
    fill_row = conn.execute("SELECT quantity, price FROM fills").fetchone()
    assert (fill_row[0], Decimal(fill_row[1])) == (10, Decimal("100"))
    broker_qty = {p.symbol: p.quantity for p in broker.get_positions()}
    attributed = {p.symbol: p.quantity for p in attribution.get_attributed("s1")}
    assert broker_qty == attributed == {"AAPL": 10}  # books tie out with the broker
    kinds = [r[0] for r in conn.execute("SELECT kind FROM audit_log ORDER BY id")]
    assert kinds == ["order_pending", "fill"]


def test_resting_limit_order_is_cancelled_not_left_working(tmp_path: Path) -> None:
    orch, broker, conn, attribution = _pipeline(tmp_path, OrderType.LIMIT)
    buy_low = Decision(Action.BUY, "AAPL", 10, limit_price=Decimal("90"))  # won't cross
    result = orch.run_cycle(_Decide([buy_low]), ["AAPL"], "s1", NOW)
    assert result.errors == []
    assert tuple(conn.execute("SELECT status FROM orders").fetchone()) == ("CANCELED",)
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    assert attribution.get_attributed("s1") == [] and broker.get_positions() == []
