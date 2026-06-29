"""Tests for HistoricalDataProvider — the asof-bound, no-lookahead MarketDataProvider
(M2.3). The most important property under test is that data after asof (minus
latency) is never exposed."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from fakes import FakeClock
from trader.core.protocols import MarketDataProvider
from trader.data.cache import ParquetCache
from trader.data.historical import HistoricalDataProvider, NoHistoricalDataError


def _ts(day: int, hour: int = 0) -> datetime:
    return datetime(2023, 1, day, hour, tzinfo=UTC)


def _bars(rows: list[tuple[datetime, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [r[0] for r in rows],
            "open": [Decimal(r[1]) for r in rows],
            "high": [Decimal(r[1]) for r in rows],
            "low": [Decimal(r[1]) for r in rows],
            "close": [Decimal(r[1]) for r in rows],
            "volume": [100 for _ in rows],
        }
    )


def _provider(tmp_path: Path, *, latency_seconds: float = 0.0) -> HistoricalDataProvider:
    cache = ParquetCache(tmp_path)
    cache.write_bars(
        "AAPL",
        _bars([(_ts(3), "10"), (_ts(4), "11"), (_ts(5), "12"), (_ts(6), "13")]),
    )
    return HistoricalDataProvider(cache, FakeClock(_ts(6)), latency_seconds=latency_seconds)


def test_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(_provider(tmp_path), MarketDataProvider)


# --- no-lookahead ----------------------------------------------------------- #


def test_no_lookahead(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    bars = provider.get_bars("AAPL", _ts(1), _ts(10), "daily", asof=_ts(4))
    # only bars with ts <= asof are visible
    assert [b.ts for b in bars] == [_ts(3), _ts(4)]


def test_asof_boundary_inclusive_when_no_latency(tmp_path: Path) -> None:
    provider = _provider(tmp_path)  # latency 0 -> cutoff == asof
    bars = provider.get_bars("AAPL", _ts(1), _ts(10), "daily", asof=_ts(4))
    assert bars[-1].ts == _ts(4)  # ts == asof is included


def test_asof_boundary_excludes_with_latency(tmp_path: Path) -> None:
    provider = _provider(tmp_path, latency_seconds=1)  # cutoff = asof - 1s
    bars = provider.get_bars("AAPL", _ts(1), _ts(10), "daily", asof=_ts(4))
    # the 01-04 bar sits exactly at asof, which is now after the cutoff -> excluded
    assert [b.ts for b in bars] == [_ts(3)]


def test_ts_equal_to_cutoff_is_included(tmp_path: Path) -> None:
    # asof one second after the bar, latency one second -> cutoff lands exactly on it
    provider = _provider(tmp_path, latency_seconds=1)
    bars = provider.get_bars("AAPL", _ts(1), _ts(10), "daily", asof=_ts(4) + timedelta(seconds=1))
    assert bars[-1].ts == _ts(4)


def test_get_bars_ascending_and_decimal(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    bars = provider.get_bars("AAPL", _ts(1), _ts(10), "daily", asof=_ts(6))
    assert [b.ts for b in bars] == [_ts(3), _ts(4), _ts(5), _ts(6)]
    assert all(isinstance(b.close, Decimal) for b in bars)


def test_get_bars_non_daily_raises(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        _provider(tmp_path).get_bars("AAPL", _ts(1), _ts(10), "minute", asof=_ts(6))


def test_get_bars_rejects_naive_asof(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _provider(tmp_path).get_bars("AAPL", _ts(1), _ts(10), "daily", asof=datetime(2023, 1, 6))


def test_get_bars_rejects_naive_start_and_end(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        provider.get_bars("AAPL", datetime(2023, 1, 1), _ts(10), "daily", asof=_ts(6))
    with pytest.raises(ValueError, match="timezone-aware"):
        provider.get_bars("AAPL", _ts(1), datetime(2023, 1, 10), "daily", asof=_ts(6))


def test_negative_latency_rejected(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    with pytest.raises(ValueError, match="non-negative"):
        HistoricalDataProvider(cache, FakeClock(_ts(6)), latency_seconds=-1)


# --- quote synthesis -------------------------------------------------------- #


def test_quote_is_at_or_before_asof(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    q = provider.get_quote("AAPL", asof=_ts(4, hour=12))  # between the 01-04 and 01-05 bars
    assert q.ts == _ts(4)
    assert q.last == Decimal("11")
    assert q.bid == q.ask == Decimal("11")  # daily history: no spread
    assert q.prev_close == Decimal("10")  # the 01-03 bar


def test_quote_excludes_future_with_latency(tmp_path: Path) -> None:
    provider = _provider(tmp_path, latency_seconds=1)
    # asof exactly at the 01-05 bar; with latency the visible quote is still 01-04
    q = provider.get_quote("AAPL", asof=_ts(5))
    assert q.ts == _ts(4)


def test_quote_prev_close_none_for_single_bar(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    q = provider.get_quote("AAPL", asof=_ts(3))  # only the first bar is visible
    assert q.ts == _ts(3)
    assert q.prev_close is None


def test_quote_no_data_raises(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    provider = HistoricalDataProvider(cache, FakeClock(_ts(6)))
    with pytest.raises(NoHistoricalDataError):
        provider.get_quote("AAPL", asof=_ts(6))


def test_quote_symbol_isolation(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", _bars([(_ts(3), "10"), (_ts(4), "11")]))
    cache.write_bars("MSFT", _bars([(_ts(3), "200"), (_ts(4), "201")]))
    provider = HistoricalDataProvider(cache, FakeClock(_ts(6)))
    assert provider.get_quote("MSFT", asof=_ts(6)).last == Decimal("201")
    assert provider.get_quote("AAPL", asof=_ts(6)).last == Decimal("11")


def test_quote_prev_close_across_cache_gap(tmp_path: Path) -> None:
    # a multi-day gap: prev_close is the last *cached* bar, not a calendar session
    cache = ParquetCache(tmp_path)
    cache.write_bars("AAPL", _bars([(_ts(3), "10"), (_ts(20), "30")]))
    provider = HistoricalDataProvider(cache, FakeClock(_ts(25)))
    q = provider.get_quote("AAPL", asof=_ts(25))
    assert q.ts == _ts(20)
    assert q.prev_close == Decimal("10")


def test_quote_bounded_lookback_finds_recent_bar(tmp_path: Path) -> None:
    # latest bar within the 14d window but its predecessor is older than 14d:
    # the escalating lookback must still surface prev_close.
    cache = ParquetCache(tmp_path)
    cache.write_bars(
        "AAPL",
        _bars([(datetime(2023, 1, 1, tzinfo=UTC), "10"), (datetime(2023, 2, 1, tzinfo=UTC), "20")]),
    )
    provider = HistoricalDataProvider(cache, FakeClock(datetime(2023, 2, 2, tzinfo=UTC)))
    q = provider.get_quote("AAPL", asof=datetime(2023, 2, 2, tzinfo=UTC))
    assert q.last == Decimal("20")
    assert q.prev_close == Decimal("10")  # found via the 90d window
