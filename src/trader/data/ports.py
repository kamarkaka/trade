"""Structural port the live market-data adapter depends on (design §5).

``SchwabMarketData`` needs only the *reads* of the Schwab client, so it depends on
this narrow Protocol rather than the concrete ``SchwabClient``. That keeps the data
layer decoupled from the transport and lets tests inject a lightweight fake that
mypy still checks structurally.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from trader.schwab.models import SchwabPriceHistory, SchwabQuote


@runtime_checkable
class QuoteSource(Protocol):
    """The subset of the Schwab read client used by the market-data adapter."""

    def get_quotes(
        self, symbols: Sequence[str], *, fields: str = ...
    ) -> dict[str, SchwabQuote]: ...

    def get_price_history(
        self,
        symbol: str,
        *,
        period_type: str = ...,
        period: int = ...,
        frequency_type: str = ...,
        frequency: int = ...,
        start_date_ms: int | None = ...,
        end_date_ms: int | None = ...,
    ) -> SchwabPriceHistory: ...
