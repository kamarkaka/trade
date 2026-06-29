"""Tests for ingest_daily: missing-only fetching, write-back, frontier handling,
and re-run no-op (M2.4)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fakes import FakeClock, FakeMarketDataProvider
from trader.core import Bar
from trader.data.cache import ParquetCache
from trader.data.ingest import ingest_daily


def _ts(day: int) -> datetime:
    return datetime(2023, 1, day, tzinfo=UTC)


# "now" well after the test windows: data is settled, so coverage isn't clamped.
SETTLED = FakeClock(datetime(2030, 1, 1, tzinfo=UTC))


def _bar(day: int, close: str, symbol: str = "AAPL") -> Bar:
    p = Decimal(close)
    return Bar(symbol=symbol, ts=_ts(day), open=p, high=p, low=p, close=p, volume=100)


class _CountingProvider:
    """Wraps a provider and counts get_bars calls + records requested ranges."""

    def __init__(self, inner: FakeMarketDataProvider) -> None:
        self._inner = inner
        self.calls: list[tuple[str, datetime, datetime]] = []

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, freq: str, asof: datetime
    ) -> Sequence[Bar]:
        self.calls.append((symbol, start, end))
        return self._inner.get_bars(symbol, start, end, freq, asof)

    def get_quote(self, symbol: str, asof: datetime):  # pragma: no cover - unused here
        return self._inner.get_quote(symbol, asof)


def _provider() -> _CountingProvider:
    bars = {"AAPL": [_bar(3, "10"), _bar(4, "11"), _bar(5, "12")]}
    return _CountingProvider(FakeMarketDataProvider(bars=bars))


def test_ingest_writes_bars_to_cache(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    results = ingest_daily(_provider(), cache, ["AAPL"], _ts(1), _ts(10), clock=SETTLED)

    assert results[0].symbol == "AAPL"
    assert results[0].bars_written == 3
    got = cache.read_bars("AAPL", _ts(1), _ts(10))
    assert got["close"].tolist() == [Decimal("10"), Decimal("11"), Decimal("12")]


def test_ingest_writes_missing_only(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    first = _provider()
    ingest_daily(first, cache, ["AAPL"], _ts(1), _ts(10), clock=SETTLED)
    assert len(first.calls) == 1  # one missing range on a cold cache

    # second run over the same window: nothing missing -> no fetches
    second = _provider()
    results = ingest_daily(second, cache, ["AAPL"], _ts(1), _ts(10), clock=SETTLED)
    assert second.calls == []
    assert results[0].ranges_fetched == 0
    assert results[0].bars_written == 0


def test_ingest_fetches_only_the_gap_on_extension(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    ingest_daily(_provider(), cache, ["AAPL"], _ts(1), _ts(5), clock=SETTLED)

    # extend the window; only the new tail [5, 10] should be fetched
    extender = _provider()
    ingest_daily(extender, cache, ["AAPL"], _ts(1), _ts(10), clock=SETTLED)
    assert len(extender.calls) == 1
    _symbol, gap_start, _gap_end = extender.calls[0]
    assert gap_start == _ts(5)


def test_ingest_empty_range_records_coverage(tmp_path: Path) -> None:
    # a settled symbol with no bars still records coverage so it isn't re-fetched
    cache = ParquetCache(tmp_path)
    empty = _CountingProvider(FakeMarketDataProvider(bars={"AAPL": []}))
    r1 = ingest_daily(empty, cache, ["AAPL"], _ts(1), _ts(10), clock=SETTLED)
    assert r1[0].bars_written == 0
    assert len(empty.calls) == 1

    again = _CountingProvider(FakeMarketDataProvider(bars={"AAPL": []}))
    ingest_daily(again, cache, ["AAPL"], _ts(1), _ts(10), clock=SETTLED)
    assert again.calls == []  # coverage recorded -> no re-fetch


def test_ingest_keeps_future_tail_refetchable(tmp_path: Path) -> None:
    # request reaches past "now": coverage must stop at the frontier so later-published
    # bars aren't masked forever.
    cache = ParquetCache(tmp_path)
    p1 = _CountingProvider(
        FakeMarketDataProvider(bars={"AAPL": [_bar(3, "10"), _bar(4, "11"), _bar(5, "12")]})
    )
    ingest_daily(p1, cache, ["AAPL"], _ts(1), _ts(10), clock=FakeClock(_ts(5)))

    # later: days 6-7 have since been published; "now" is day 11
    p2 = _CountingProvider(FakeMarketDataProvider(bars={"AAPL": [_bar(6, "13"), _bar(7, "14")]}))
    r = ingest_daily(p2, cache, ["AAPL"], _ts(1), _ts(10), clock=FakeClock(_ts(11)))
    assert len(p2.calls) == 1  # the tail was refetched, not masked
    _symbol, gap_start, _gap_end = p2.calls[0]
    assert gap_start == _ts(5)  # from the previous run's frontier
    assert r[0].bars_written == 2
    got = cache.read_bars("AAPL", _ts(1), _ts(10))
    assert got["close"].tolist() == [Decimal(x) for x in ("10", "11", "12", "13", "14")]


def test_ingest_multi_symbol(tmp_path: Path) -> None:
    cache = ParquetCache(tmp_path)
    provider = _CountingProvider(
        FakeMarketDataProvider(
            bars={
                "AAPL": [_bar(3, "10", symbol="AAPL")],
                "MSFT": [_bar(3, "200", symbol="MSFT")],
            }
        )
    )
    results = ingest_daily(provider, cache, ["AAPL", "MSFT"], _ts(1), _ts(10), clock=SETTLED)
    assert {r.symbol for r in results} == {"AAPL", "MSFT"}
    assert cache.read_bars("AAPL", _ts(1), _ts(10))["close"].tolist() == [Decimal("10")]
    assert cache.read_bars("MSFT", _ts(1), _ts(10))["close"].tolist() == [Decimal("200")]
