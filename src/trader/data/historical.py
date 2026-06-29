"""HistoricalDataProvider — asof-bound MarketDataProvider over the Parquet cache
(design §9, Appendix B). The backtest counterpart of the live ``SchwabMarketData``.

The single most important rule (Appendix B): **never expose data after ``asof``**.
Every read is clamped to ``asof - latency_seconds`` (the signal-to-data delay), so
the SAME strategy code that runs live is fed strictly point-in-time data in
backtest — no-lookahead is structural, not a matter of caller discipline.

A daily ``Quote`` is synthesized from the most recent bar at-or-before the cutoff:
no intraday spread exists in daily history, so ``bid == ask == last == close`` and
``prev_close`` is the prior bar's close.

Parity note: ``latency_seconds`` models the signal-to-data delay and is a backtest
concept; the live ``SchwabMarketData`` has no latency clamp (it rejects *stale*
quotes instead). With ``latency_seconds == 0`` the asof boundary matches live
(``ts == asof`` is visible).
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

    # Escalating lookback windows for get_quote so it's O(window), not O(history);
    # falls back to a full read only for very sparse symbols.
    _QUOTE_LOOKBACK_DAYS = (14, 90, 400)

    def __init__(self, cache: ParquetCache, clock: Clock, *, latency_seconds: float = 0.0) -> None:
        if latency_seconds < 0:
            # A negative latency would push the cutoff past asof and expose the
            # future — the one invariant this class exists to prevent.
            raise ValueError(f"latency_seconds must be non-negative, got {latency_seconds}")
        self._cache = cache
        self._clock = clock
        self._latency = timedelta(seconds=latency_seconds)

    def _cutoff(self, asof: datetime) -> datetime:
        """The latest instant whose data is visible at ``asof`` (asof - latency)."""
        return _require_utc(asof, "asof") - self._latency

    def _bars_up_to(self, symbol: str, cutoff: datetime) -> Any:
        """Recent bars ending at ``cutoff`` — escalating lookback, then full read.

        Returns enough trailing bars to identify the latest bar and its predecessor
        without reading the whole history on every quote.
        """
        for days in self._QUOTE_LOOKBACK_DAYS:
            window = self._cache.read_bars(symbol, cutoff - timedelta(days=days), cutoff)
            if len(window) >= 2:
                return window
        # Sparse symbol (or <2 bars in 400d): fall back to the full history.
        return self._cache.read_bars(symbol, _EPOCH, cutoff)

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
        """Synthesize a quote from the most recent bar at-or-before ``asof - latency``.

        ``prev_close`` is the previous *cached* bar's close (the immediately-preceding
        visible bar); across a gap in the cache it is the last available bar, not
        necessarily the prior calendar session.
        """
        cutoff = self._cutoff(asof)
        df = self._bars_up_to(symbol, cutoff)
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
