"""HistoricalDataProvider — asof-bound MarketDataProvider over the Parquet cache
(design §9, Appendix B). The backtest counterpart of the live ``SchwabMarketData``.

The single most important rule (Appendix B): **never expose data after ``asof``**.
Every read is clamped to ``asof - latency_seconds`` (the signal-to-data delay), so
the SAME strategy code that runs live is fed strictly point-in-time data in
backtest — no-lookahead is structural, not a matter of caller discipline.

A daily ``Quote`` is synthesized from the most recent bar at-or-before the cutoff:
no intraday spread exists in daily history, so ``bid == ask == last == close`` and
``prev_close`` is the prior bar's close.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from trader.core import Bar, Quote
from trader.core.protocols import Clock

from .cache import ParquetCache

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class NoHistoricalDataError(LookupError):
    """No cached bar exists at or before the requested asof for a symbol."""


def _require_utc(dt: datetime, name: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")
    return dt.astimezone(UTC)


class HistoricalDataProvider:
    """Point-in-time market data sourced from the cache, clamped to ``asof``."""

    def __init__(self, cache: ParquetCache, clock: Clock, *, latency_seconds: float = 0.0) -> None:
        self._cache = cache
        self._clock = clock
        self._latency = timedelta(seconds=latency_seconds)

    def _cutoff(self, asof: datetime) -> datetime:
        """The latest instant whose data is visible at ``asof`` (asof - latency)."""
        return _require_utc(asof, "asof") - self._latency

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, freq: str, asof: datetime
    ) -> Sequence[Bar]:
        """Bars in ``[start, min(end, asof - latency)]``, ascending. No lookahead."""
        if freq != "daily":
            raise NotImplementedError(f"only daily bars are supported in M2, got {freq!r}")
        start = _require_utc(start, "start")
        end = _require_utc(end, "end")
        visible_end = min(end, self._cutoff(asof))
        df = self._cache.read_bars(symbol, start, visible_end)
        return [self._to_bar(symbol, row) for row in df.itertuples(index=False)]

    def get_quote(self, symbol: str, asof: datetime) -> Quote:
        """Synthesize a quote from the most recent bar at-or-before ``asof - latency``."""
        cutoff = self._cutoff(asof)
        df = self._cache.read_bars(symbol, _EPOCH, cutoff)
        if df.empty:
            raise NoHistoricalDataError(f"no bar at or before {cutoff.isoformat()} for {symbol!r}")
        last = df.iloc[-1]
        prev_close = df.iloc[-2].close if len(df) >= 2 else None
        return Quote(
            symbol=symbol,
            ts=last.ts.to_pydatetime(),
            last=last.close,
            bid=last.close,  # daily history has no spread
            ask=last.close,
            volume=int(last.volume),
            prev_close=prev_close,
        )

    @staticmethod
    def _to_bar(symbol: str, row: Any) -> Bar:
        return Bar(
            symbol=symbol,
            ts=row.ts.to_pydatetime(),
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=int(row.volume),
        )
