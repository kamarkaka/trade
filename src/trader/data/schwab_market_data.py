"""SchwabMarketData: adapts the read-only SchwabClient to the core
MarketDataProvider protocol (design §5/§8, Appendix B).

This is the live half of the live/backtest parity seam: strategies and the
orchestrator depend on ``MarketDataProvider``, and in production this adapter
satisfies it by translating Schwab's wire models (``SchwabQuote`` / ``SchwabCandle``)
into core value types (``Quote`` / ``Bar``). Two invariants are enforced here, at
the boundary:

* **No lookahead** — ``get_bars`` returns only candles with ``ts <= asof``.
* **Staleness** — ``get_quote`` rejects a quote older than ``max_staleness_seconds``
  before ``asof`` (price-sanity, §4.2/§10), so a frozen feed can't drive trades.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from trader.core import Bar, Quote
from trader.core.protocols import Clock
from trader.schwab.errors import SchwabBadResponseError, SchwabStaleQuoteError

from .ports import QuoteSource

__all__ = ["SchwabMarketData"]


def _require_utc(dt: datetime, name: str) -> None:
    """Reject naive datetimes at the boundary.

    A naive ``asof``/``start``/``end`` would be interpreted in *local* time by
    ``datetime.timestamp()`` (silently producing a wrong Schwab date-range request)
    rather than UTC. The ``Clock`` contract guarantees tz-aware UTC, but we enforce
    it here so a misuse fails loudly instead of querying the wrong window.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {dt!r}")


class SchwabMarketData:
    """Live MarketDataProvider backed by the read-only Schwab client."""

    def __init__(
        self,
        client: QuoteSource,
        clock: Clock,
        *,
        max_staleness_seconds: int = 60,
    ) -> None:
        self._client = client
        # Reserved for future intraday/streaming staleness checks; current reads
        # derive everything from the caller-supplied ``asof``.
        self._clock = clock
        self._max_staleness_seconds = max_staleness_seconds

    def get_quote(self, symbol: str, asof: datetime) -> Quote:
        """Return the current quote for ``symbol``, rejecting stale data."""
        _require_utc(asof, "asof")
        quotes = self._client.get_quotes([symbol])
        sq = quotes.get(symbol)
        if sq is None:
            raise SchwabBadResponseError(f"no quote returned for {symbol!r}")
        oldest_allowed = asof - timedelta(seconds=self._max_staleness_seconds)
        if sq.quote_time < oldest_allowed:
            raise SchwabStaleQuoteError(
                f"stale quote for {symbol!r}: {sq.quote_time.isoformat()} is older than "
                f"{self._max_staleness_seconds}s before asof {asof.isoformat()}"
            )
        return Quote(
            symbol=symbol,
            ts=sq.quote_time,
            last=sq.last,
            bid=sq.bid,
            ask=sq.ask,
            volume=sq.volume,
            prev_close=sq.prev_close,
        )

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, freq: str, asof: datetime
    ) -> Sequence[Bar]:
        """Return OHLCV bars in ``[start, end]`` (inclusive), ascending by time, with
        no candle later than ``asof``."""
        if freq != "daily":
            raise NotImplementedError(f"only daily bars are supported in M1, got {freq!r}")
        for value, label in ((start, "start"), (end, "end"), (asof, "asof")):
            _require_utc(value, label)
        history = self._client.get_price_history(
            symbol,
            frequency_type="daily",
            frequency=1,
            start_date_ms=int(start.timestamp() * 1000),
            end_date_ms=int(end.timestamp() * 1000),
        )
        bars = [
            Bar(
                symbol=symbol,
                ts=c.ts,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
            )
            for c in history.candles
            # no-lookahead is enforced at the boundary, not by caller discipline
            if start <= c.ts <= end and c.ts <= asof
        ]
        # Defensive: don't rely on Schwab returning candles chronologically.
        bars.sort(key=lambda b: b.ts)
        return bars
