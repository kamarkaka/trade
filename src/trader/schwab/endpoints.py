"""Read-only Schwab endpoints over the resilient transport (design §8.4/§8.5).

A thin facade exposing only the reads M1 needs: quotes, daily price history, and
hashed account-number resolution. NO order/position/balance writes (those are M5).
"""

from __future__ import annotations

from collections.abc import Sequence

from trader.observability.logging import register_secret

from .constants import ACCOUNT_NUMBERS_PATH, PRICEHISTORY_PATH, QUOTES_PATH
from .http import SchwabHttp
from .models import (
    AccountNumberMapping,
    SchwabPriceHistory,
    SchwabQuote,
    parse_account_numbers,
    parse_price_history,
    parse_quote,
)


class SchwabClient:
    """Read-only Schwab API client built on the SchwabHttp transport."""

    def __init__(self, http: SchwabHttp) -> None:
        self._http = http

    def get_quotes(
        self, symbols: Sequence[str], *, fields: str = "quote"
    ) -> dict[str, SchwabQuote]:
        """Batched quotes for ``symbols`` (a symbol absent from the response is omitted)."""
        data = self._http.get_json(
            QUOTES_PATH, params={"symbols": ",".join(symbols), "fields": fields}
        )
        result: dict[str, SchwabQuote] = {}
        for symbol in symbols:
            entry = data.get(symbol) if isinstance(data, dict) else None
            if entry is not None:
                result[symbol] = parse_quote(symbol, entry)
        return result

    def get_price_history(
        self,
        symbol: str,
        *,
        period_type: str = "year",
        period: int = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        start_date_ms: int | None = None,
        end_date_ms: int | None = None,
    ) -> SchwabPriceHistory:
        """Daily (by default) candles for ``symbol`` (§8.4 period/frequency model)."""
        params: dict[str, str | int] = {
            "symbol": symbol,
            "periodType": period_type,
            "period": period,
            "frequencyType": frequency_type,
            "frequency": frequency,
        }
        if start_date_ms is not None:
            params["startDate"] = start_date_ms
        if end_date_ms is not None:
            params["endDate"] = end_date_ms
        return parse_price_history(self._http.get_json(PRICEHISTORY_PATH, params=params))

    def get_account_numbers(self) -> list[AccountNumberMapping]:
        """Resolve raw account numbers to the hashed ids used by trading endpoints.

        The raw account number is PII; register it as a scrub literal (like tokens)
        so it can never leak into logs even via free text (§13).
        """
        mappings = parse_account_numbers(self._http.get_json(ACCOUNT_NUMBERS_PATH))
        for mapping in mappings:
            register_secret(mapping.account_number)
        return mappings
