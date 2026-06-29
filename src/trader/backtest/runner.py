"""Backtest runner: the shared multi-strategy pipeline (design §9, M6.7/M6.8).

ONE function — ``run_backtest_report`` — wires the offline backtest exactly as the
``trader backtest`` CLI and the golden-run test (M6.8) both use it, so the golden
proves the *real* path is reproducible. It is fully OFFLINE and deterministic:
``VirtualClock`` + ``HistoricalDataProvider`` over the Parquet cache + ``SimBroker`` +
the XNYS calendar + merged time-sorted triggers across every enabled strategy. It never
touches the broker network or the durable state DB (per-strategy attribution runs against
a throwaway temp database).
"""

from __future__ import annotations

import itertools
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import trader.strategy  # noqa: F401 - registers built-in strategies into REGISTRY
from trader.broker import SimBroker
from trader.clock import VirtualClock
from trader.config import AppConfig
from trader.core import Decision, Order
from trader.data.cache import ParquetCache
from trader.data.historical import HistoricalDataProvider
from trader.scheduler.calendar import TradingCalendar
from trader.sizing.sizer import size_decision
from trader.state.attribution import AttributionLedger
from trader.state.db import connect
from trader.state.migrate import run_migrations
from trader.strategy import load_bindings

from .engine import run_multi_strategy
from .manifest import build_manifest
from .report import BacktestReportDoc, BacktestRunResult, FireRecord, build_report

# Default backtest starting capital until a config-driven account balance exists.
STARTING_CASH = Decimal("100000")


@dataclass(frozen=True)
class BacktestRun:
    """The artifacts of a backtest run: the report document, its manifest, and the
    total number of fills (for the CLI's no-data warning)."""

    doc: BacktestReportDoc
    manifest: dict[str, Any]
    num_fills: int


def run_backtest_report(cfg: AppConfig, start_d: date, end_d: date) -> BacktestRun:
    """Run the multi-strategy backtest over cached history and build the per-strategy +
    combined report. Raises ``ValueError`` for an invalid config / no enabled strategy."""
    schedule, bindings = load_bindings(cfg)  # registry-validated; raises ValueError on bad params
    enabled = [b for b in bindings if b.enabled]
    if not enabled:
        raise ValueError("no enabled strategy in config")
    universe = sorted({sym for b in enabled for sym in b.universe})
    seed = schedule.base_seed or 0

    clock = VirtualClock(datetime.combine(start_d, time.min, tzinfo=UTC))
    cache = ParquetCache(cfg.observability.data_cache)
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=STARTING_CASH)

    # Deterministic client_order_ids (assigned in trigger order) so the report is
    # byte-reproducible run-to-run.
    ids = itertools.count()

    def _sizer(decision: Decision, strategy_id: str) -> Order | None:
        return size_decision(
            decision, strategy_id, cfg.execution, id_factory=lambda: f"bt-{next(ids)}"
        )

    # Throwaway state DB for per-strategy attribution — a backtest writes nothing durable
    # and never opens the configured observability.db_path.
    with tempfile.TemporaryDirectory() as state_dir:
        conn = connect(Path(state_dir) / "state.sqlite")
        try:
            run_migrations(conn)
            result = run_multi_strategy(
                bindings=enabled,
                schedule=schedule,
                calendar=TradingCalendar(),
                data=data,
                broker=broker,
                attribution=AttributionLedger(conn),
                sizer=_sizer,
                clock=clock,
                start=start_d,
                end=end_d,
            )
        finally:
            conn.close()

    data_hashes = {symbol: cache.content_hash(symbol) for symbol in universe}
    manifest = build_manifest(cfg, data_hashes, seed)
    run_result = BacktestRunResult(
        combined_equity_curve=result.equity_curve,
        per_strategy_trades=result.per_strategy_trades,
        fire_log=[
            FireRecord(t.strategy_id, t.slot_id, t.fire_ts, t.drift_seconds, t.seed)
            for t in result.fire_log
        ],
    )
    doc = build_report(run_result, manifest, strategy_ids=[b.strategy_id for b in enabled])
    num_fills = sum(len(t) for t in result.per_strategy_trades.values())
    return BacktestRun(doc=doc, manifest=manifest, num_fills=num_fills)


__all__ = ["STARTING_CASH", "BacktestRun", "run_backtest_report"]
