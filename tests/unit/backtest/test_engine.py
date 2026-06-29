"""Tests for the single-strategy BacktestEngine: it runs, records trades, is
deterministic, and defers fills to the next trigger (no-lookahead) (M2.8)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pandas as pd

from trader.backtest import Portfolio
from trader.backtest.engine import BacktestEngine
from trader.broker import SimBroker
from trader.clock import VirtualClock
from trader.core import Account, Decision, MarketSnapshot, Position
from trader.core.enums import Action
from trader.core.protocols import Clock, MarketDataProvider
from trader.data.cache import ParquetCache
from trader.data.historical import HistoricalDataProvider

SLOT = time(15, 0)
START = date(2023, 1, 2)
END = date(2023, 1, 6)  # 5 calendar days -> 5 triggers


class AlwaysBuy:
    """Buys ``qty`` of every symbol visible in the snapshot, every trigger."""

    def __init__(self, qty: int = 1) -> None:
        self._qty = qty

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return [Decision(action=Action.BUY, symbol=s, quantity=self._qty) for s in snapshot.quotes]


class AlwaysHold:
    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return [Decision(action=Action.HOLD, symbol=s) for s in snapshot.quotes]


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


def _engine(tmp_path: Path) -> BacktestEngine:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", _bars())
    clock = VirtualClock(datetime(2023, 1, 1, tzinfo=UTC))
    data = HistoricalDataProvider(cache, clock)
    broker = SimBroker(data, clock, starting_cash=Decimal("100000"))
    portfolio = Portfolio(Decimal("100000"))
    return BacktestEngine(clock=clock, data=data, broker=broker, portfolio=portfolio)


def test_runs_and_records_trades(tmp_path: Path) -> None:
    result = _engine(tmp_path).run(
        AlwaysBuy(1), universe=["AAPL"], slots=[SLOT], start=START, end=END
    )
    # 5 triggers, deferred fills -> the first trigger's decision fills on the 2nd, etc;
    # the last trigger's decision has no following trigger -> 4 fills.
    assert len(result.fills) == 4
    assert all(f.symbol == "AAPL" and f.quantity == 1 for f in result.fills)
    assert len(result.equity_curve) == 5  # one snapshot per trigger


def test_no_trades_when_strategy_holds(tmp_path: Path) -> None:
    result = _engine(tmp_path).run(
        AlwaysHold(), universe=["AAPL"], slots=[SLOT], start=START, end=END
    )
    assert result.fills == []
    assert len(result.equity_curve) == 5


def test_engine_is_deterministic(tmp_path: Path) -> None:
    a = _engine(tmp_path / "a").run(
        AlwaysBuy(1), universe=["AAPL"], slots=[SLOT], start=START, end=END
    )
    b = _engine(tmp_path / "b").run(
        AlwaysBuy(1), universe=["AAPL"], slots=[SLOT], start=START, end=END
    )
    assert a.fills == b.fills
    assert a.equity_curve == b.equity_curve


def test_fill_uses_next_trigger_bar_not_decision_bar(tmp_path: Path) -> None:
    # decision at Jan-2 (close 102) must fill at Jan-3's bar (close 103), never Jan-2.
    result = _engine(tmp_path).run(
        AlwaysBuy(1), universe=["AAPL"], slots=[SLOT], start=START, end=END
    )
    assert result.fills[0].price == Decimal("103")  # next bar, no lookahead
