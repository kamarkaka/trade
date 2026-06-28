"""A deterministic MarketDataProvider test double (honors the no-lookahead asof)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trader.core import Bar, Quote


class FakeMarketDataProvider:
    """Implements ``trader.core.protocols.MarketDataProvider`` from canned data.

    ``quotes`` and ``bars`` map symbol -> chronologically-ordered series; reads
    return only entries with ``ts <= asof`` (structural no-lookahead).
    """

    def __init__(
        self,
        quotes: dict[str, Sequence[Quote]] | None = None,
        bars: dict[str, Sequence[Bar]] | None = None,
    ) -> None:
        self._quotes = quotes or {}
        self._bars = bars or {}

    def get_quote(self, symbol: str, asof: datetime) -> Quote:
        available = [q for q in self._quotes.get(symbol, ()) if q.ts <= asof]
        if not available:
            raise KeyError(f"no quote for {symbol!r} at or before {asof.isoformat()}")
        return max(available, key=lambda q: q.ts)  # latest at-or-before asof, order-independent

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, freq: str, asof: datetime
    ) -> Sequence[Bar]:
        return [b for b in self._bars.get(symbol, ()) if start <= b.ts <= end and b.ts <= asof]
