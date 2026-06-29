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
from trader.core.protocols import Clock, MarketDataProvider

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
    *,
    clock: Clock,
) -> list[IngestResult]:
    """Fetch and cache daily bars for ``symbols`` over ``[start, end]``, missing-only.

    For each gap the cache reports, fetch via the provider and write it back. A
    settled gap that yields no bars (holiday/halt) still records its coverage so it
    is not re-fetched. Coverage is recorded only up to the data frontier
    (``clock.now()``): a gap reaching into the future stays a refetchable gap, so
    bars published later (after the requested ``end`` passes) are not masked forever.
    """
    now = clock.now()
    results: list[IngestResult] = []
    for symbol in symbols:
        ranges_fetched = 0
        bars_written = 0
        for gap_start, gap_end in cache.missing_ranges(symbol, start, end):
            bars = provider.get_bars(symbol, gap_start, gap_end, "daily", asof=gap_end)
            frame = _bars_to_frame(bars)
            settled_end = min(gap_end, now)
            if settled_end > gap_start:
                cache.write_bars(symbol, frame, covered=(gap_start, settled_end))
            elif bars:
                # Gap is entirely in the future but bars came back anyway: store them
                # without claiming coverage of the unsettled future.
                cache.write_bars(symbol, frame)
            ranges_fetched += 1
            bars_written += len(bars)
        results.append(IngestResult(symbol, ranges_fetched, bars_written))
    return results
