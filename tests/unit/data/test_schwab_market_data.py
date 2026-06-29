"""Tests for SchwabMarketData: wire-model -> core mapping, quote staleness, and
no-lookahead bar filtering. Uses a fake QuoteSource (no transport/network)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fakes import FakeClock
from trader.data import SchwabMarketData
from trader.schwab.errors import SchwabBadResponseError, SchwabStaleQuoteError
from trader.schwab.models import SchwabCandle, SchwabPriceHistory, SchwabQuote

NOW = datetime(2026, 6, 28, 16, 0, tzinfo=UTC)


class _FakeClient:
    """Records calls and returns canned Schwab wire models."""

    def __init__(
        self,
        *,
        quotes: dict[str, SchwabQuote] | None = None,
        history: SchwabPriceHistory | None = None,
    ) -> None:
        self._quotes = quotes or {}
        self._history = history or SchwabPriceHistory("X", ())
        self.history_kwargs: dict[str, object] = {}

    def get_quotes(
        self, symbols: Sequence[str], *, fields: str = "quote"
    ) -> dict[str, SchwabQuote]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    def get_price_history(self, symbol: str, **kwargs: object) -> SchwabPriceHistory:
        self.history_kwargs = kwargs
        return self._history


def _quote(symbol: str, *, quote_time: datetime) -> SchwabQuote:
    return SchwabQuote(
        symbol=symbol,
        last=Decimal("150.25"),
        bid=Decimal("150.20"),
        ask=Decimal("150.30"),
        volume=12345,
        quote_time=quote_time,
        prev_close=Decimal("149.50"),
    )


def _adapter(client: _FakeClient, *, max_staleness_seconds: int = 60) -> SchwabMarketData:
    return SchwabMarketData(client, FakeClock(NOW), max_staleness_seconds=max_staleness_seconds)


# --- get_quote -------------------------------------------------------------- #


def test_get_quote_maps_fields_to_core_quote() -> None:
    client = _FakeClient(quotes={"AAPL": _quote("AAPL", quote_time=NOW)})
    q = _adapter(client).get_quote("AAPL", NOW)
    assert q.symbol == "AAPL"
    assert q.ts == NOW
    assert q.last == Decimal("150.25")
    assert q.bid == Decimal("150.20")
    assert q.ask == Decimal("150.30")
    assert q.volume == 12345
    assert q.prev_close == Decimal("149.50")


def test_get_quote_missing_symbol_raises() -> None:
    with pytest.raises(SchwabBadResponseError):
        _adapter(_FakeClient(quotes={})).get_quote("AAPL", NOW)


def test_get_quote_rejects_stale_quote() -> None:
    stale = NOW - timedelta(seconds=61)
    client = _FakeClient(quotes={"AAPL": _quote("AAPL", quote_time=stale)})
    with pytest.raises(SchwabStaleQuoteError):
        _adapter(client, max_staleness_seconds=60).get_quote("AAPL", NOW)


def test_get_quote_accepts_quote_at_staleness_boundary() -> None:
    boundary = NOW - timedelta(seconds=60)  # exactly the window edge: allowed
    client = _FakeClient(quotes={"AAPL": _quote("AAPL", quote_time=boundary)})
    q = _adapter(client, max_staleness_seconds=60).get_quote("AAPL", NOW)
    assert q.ts == boundary


# --- get_bars --------------------------------------------------------------- #


def _candle(ts: datetime, close: str) -> SchwabCandle:
    return SchwabCandle(
        ts=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal(close),
        volume=1000,
    )


def test_get_bars_maps_and_passes_date_range() -> None:
    start = NOW - timedelta(days=2)
    end = NOW
    history = SchwabPriceHistory("AAPL", (_candle(start, "100.5"), _candle(end, "101.5")))
    client = _FakeClient(history=history)
    bars = _adapter(client).get_bars("AAPL", start, end, "daily", NOW)
    assert [b.close for b in bars] == [Decimal("100.5"), Decimal("101.5")]
    assert bars[0].symbol == "AAPL"
    assert client.history_kwargs["start_date_ms"] == int(start.timestamp() * 1000)
    assert client.history_kwargs["end_date_ms"] == int(end.timestamp() * 1000)
    assert client.history_kwargs["frequency_type"] == "daily"


def test_get_bars_drops_candles_after_asof() -> None:
    start = NOW - timedelta(days=3)
    future = NOW + timedelta(days=1)  # later than asof -> must be excluded
    history = SchwabPriceHistory("AAPL", (_candle(NOW, "100"), _candle(future, "200")))
    bars = _adapter(_FakeClient(history=history)).get_bars("AAPL", start, future, "daily", NOW)
    assert [b.ts for b in bars] == [NOW]  # no-lookahead


def test_get_bars_filters_outside_range() -> None:
    start = NOW - timedelta(days=2)
    end = NOW
    before = start - timedelta(days=5)
    history = SchwabPriceHistory("AAPL", (_candle(before, "1"), _candle(end, "2")))
    bars = _adapter(_FakeClient(history=history)).get_bars("AAPL", start, end, "daily", NOW)
    assert [b.close for b in bars] == [Decimal("2")]


def test_get_bars_returns_ascending_even_if_source_unordered() -> None:
    start = NOW - timedelta(days=5)
    end = NOW
    earlier = NOW - timedelta(days=2)
    later = NOW - timedelta(days=1)
    # source returns candles out of order
    history = SchwabPriceHistory("AAPL", (_candle(later, "2"), _candle(earlier, "1")))
    bars = _adapter(_FakeClient(history=history)).get_bars("AAPL", start, end, "daily", NOW)
    assert [b.ts for b in bars] == [earlier, later]


def test_get_bars_rejects_non_daily_frequency() -> None:
    with pytest.raises(NotImplementedError):
        _adapter(_FakeClient()).get_bars("AAPL", NOW, NOW, "minute", NOW)


def test_get_quote_rejects_naive_asof() -> None:
    naive = datetime(2026, 6, 28, 16, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        _adapter(_FakeClient()).get_quote("AAPL", naive)


def test_get_bars_rejects_naive_bounds() -> None:
    naive = datetime(2026, 6, 28, 16, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        _adapter(_FakeClient()).get_bars("AAPL", naive, NOW, "daily", NOW)


def test_adapter_satisfies_market_data_protocol() -> None:
    from trader.core.protocols import MarketDataProvider

    assert isinstance(_adapter(_FakeClient()), MarketDataProvider)
