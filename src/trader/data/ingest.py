"""Cache-on-demand daily ingestion: MarketDataProvider -> ParquetCache (design §9).

``ingest_daily`` fills only the *missing* ranges for each symbol (computed by the
cache's coverage catalog), so re-running is cheap and the first run is offline
thereafter. It is provider-agnostic — driven by the M1 ``SchwabMarketData`` adapter
in production and by ``FakeMarketDataProvider`` in tests — and never places orders
(reads only).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from trader.core import Bar
from trader.core.protocols import MarketDataProvider

from .cache import BAR_COLUMNS, ParquetCache


@dataclass(frozen=True)
class IngestResult:
    """Per-symbol ingestion summary."""

    symbol: str
    ranges_fetched: int
    bars_written: int


def _bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [b.ts for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        columns=list(BAR_COLUMNS),
    )


def ingest_daily(
    provider: MarketDataProvider,
    cache: ParquetCache,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
) -> list[IngestResult]:
    """Fetch and cache daily bars for ``symbols`` over ``[start, end]``, missing-only.

    For each gap the cache reports, fetch via the provider and write it back. Even a
    gap that yields no bars (holiday/halt) records its coverage, so it is not
    re-fetched on the next run.
    """
    results: list[IngestResult] = []
    for symbol in symbols:
        ranges_fetched = 0
        bars_written = 0
        for gap_start, gap_end in cache.missing_ranges(symbol, start, end):
            bars = provider.get_bars(symbol, gap_start, gap_end, "daily", asof=gap_end)
            cache.write_bars(symbol, _bars_to_frame(bars), covered=(gap_start, gap_end))
            ranges_fetched += 1
            bars_written += len(bars)
        results.append(IngestResult(symbol, ranges_fetched, bars_written))
    return results
