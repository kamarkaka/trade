"""Golden-run reproducibility (M2.10): a fixed config+data fixture must reproduce a
committed report bit-for-bit (after stripping environment-sensitive manifest fields).
This is the regression guard against accidental lookahead/non-determinism.

Regenerate the golden (after an intentional change) with:
    python tests/backtest/test_golden_single.py
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from trader.backtest import BacktestEngine, Portfolio, build_manifest
from trader.backtest.report import BacktestReport, strip_volatile
from trader.broker import SimBroker
from trader.clock import VirtualClock
from trader.config import DEFAULT_CONFIG_PATH, load_config
from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.data.cache import ParquetCache
from trader.data.historical import HistoricalDataProvider

GOLDEN = Path(__file__).parent / "golden" / "report_single.json"
SEED = 7


class _AlwaysBuy:
    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return [Decision(action=Action.BUY, symbol=s, quantity=1) for s in snapshot.quotes]


def _bars() -> pd.DataFrame:
    rows = [(datetime(2023, 1, d, tzinfo=UTC), Decimal(f"{100 + d}")) for d in range(2, 7)]
    return pd.DataFrame(
        {
            "ts": [ts for ts, _ in rows],
            "open": [p for _, p in rows],
            "high": [p for _, p in rows],
            "low": [p for _, p in rows],
            "close": [p for _, p in rows],
            "volume": [10000 for _ in rows],
        }
    )


def _produce(root: Path) -> dict[str, Any]:
    cache = ParquetCache(root)
    cache.write_bars("AAPL", _bars())
    clock = VirtualClock(datetime(2023, 1, 1, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))
    portfolio = Portfolio(Decimal("100000"))
    engine = BacktestEngine(clock=clock, data=data, broker=broker, portfolio=portfolio)
    result = engine.run(
        _AlwaysBuy(),
        universe=["AAPL"],
        slots=[time(15, 0)],
        start=date(2023, 1, 2),
        end=date(2023, 1, 6),
        seed=SEED,
    )
    manifest = build_manifest(
        load_config(DEFAULT_CONFIG_PATH), {"AAPL": cache.content_hash("AAPL")}, SEED
    )
    return BacktestReport.build(result.fills, result.equity_curve, manifest)


def test_matches_committed_golden(tmp_path: Path) -> None:
    first = strip_volatile(_produce(tmp_path / "a"))
    second = strip_volatile(_produce(tmp_path / "b"))
    assert first == second  # two runs are byte-identical (deterministic)
    assert first == json.loads(GOLDEN.read_text())  # and equal the committed golden


if __name__ == "__main__":  # regen helper: writes the committed golden
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        report = strip_volatile(_produce(Path(tmp)))
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN}")
