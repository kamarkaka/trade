"""Tests for Schwab read endpoints + parsers (recorded-shape fixtures; FakeHttp)."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.schwab.constants import ACCOUNT_NUMBERS_PATH, PRICEHISTORY_PATH, QUOTES_PATH
from trader.schwab.endpoints import SchwabClient
from trader.schwab.errors import SchwabBadResponseError
from trader.schwab.models import parse_quote

FIXTURES = Path(__file__).parents[2] / "fixtures" / "schwab"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class _FakeHttp:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((url, dict(params) if params else None))
        return self.response


def _client(response: Any) -> tuple[SchwabClient, _FakeHttp]:
    http = _FakeHttp(response)
    return SchwabClient(http), http  # type: ignore[arg-type]


# --- parsers ---------------------------------------------------------------- #


def test_parse_quote_uses_decimal_and_utc() -> None:
    q = parse_quote("AAPL", _fixture("quotes_AAPL_MSFT.json")["AAPL"])
    assert q.last == Decimal("150.25")
    assert isinstance(q.last, Decimal)
    assert q.prev_close == Decimal("149.50")
    assert q.volume == 12345678
    assert q.quote_time.utcoffset().total_seconds() == 0  # tz-aware UTC


def test_parse_quote_missing_field_raises() -> None:
    with pytest.raises(SchwabBadResponseError):
        parse_quote("AAPL", {"quote": {"bidPrice": 1.0}})  # no lastPrice


def test_parse_quote_missing_prev_close_is_none() -> None:
    q = parse_quote(
        "X", {"quote": {"lastPrice": 1, "bidPrice": 1, "askPrice": 1, "quoteTime": 1782556800000}}
    )
    assert q.prev_close is None


# --- endpoints -------------------------------------------------------------- #


def test_get_quotes_params_and_mapping() -> None:
    client, http = _client(_fixture("quotes_AAPL_MSFT.json"))
    quotes = client.get_quotes(["AAPL", "MSFT"])
    assert set(quotes) == {"AAPL", "MSFT"}
    assert quotes["MSFT"].last == Decimal("405.10")
    assert http.calls[0] == (QUOTES_PATH, {"symbols": "AAPL,MSFT", "fields": "quote"})


def test_get_quotes_omits_missing_symbol() -> None:
    client, _ = _client(_fixture("quotes_AAPL_MSFT.json"))
    quotes = client.get_quotes(["AAPL", "GOOG"])  # GOOG not in response
    assert set(quotes) == {"AAPL"}


def test_get_price_history_parses_candles_ascending() -> None:
    client, http = _client(_fixture("pricehistory_AAPL_daily.json"))
    history = client.get_price_history("AAPL")
    assert history.symbol == "AAPL"
    assert len(history.candles) == 2
    assert history.candles[0].ts < history.candles[1].ts
    assert history.candles[1].close == Decimal("150.25")
    assert http.calls[0][0] == PRICEHISTORY_PATH
    assert http.calls[0][1] == {
        "symbol": "AAPL",
        "periodType": "year",
        "period": 1,
        "frequencyType": "daily",
        "frequency": 1,
    }


def test_get_account_numbers_exposes_hash() -> None:
    client, http = _client(_fixture("account_numbers.json"))
    mappings = client.get_account_numbers()
    assert mappings[0].hash_value == "ABCDEF0123456789ABCDEF"
    assert mappings[0].account_number == "123456789"
    assert http.calls[0] == (ACCOUNT_NUMBERS_PATH, None)


def test_parse_price_history_bad_candle_raises() -> None:
    client, _ = _client({"symbol": "AAPL", "candles": [{"open": 1}]})  # missing datetime/high/...
    with pytest.raises(SchwabBadResponseError):
        client.get_price_history("AAPL")


def test_get_price_history_with_date_range() -> None:
    client, http = _client(_fixture("pricehistory_AAPL_daily.json"))
    client.get_price_history("AAPL", start_date_ms=1782000000000, end_date_ms=1782600000000)
    params = http.calls[0][1]
    assert params is not None
    assert params["startDate"] == 1782000000000
    assert params["endDate"] == 1782600000000


def test_parse_quote_non_dict_entry_raises() -> None:
    with pytest.raises(SchwabBadResponseError):
        parse_quote("X", "not-a-dict")


def test_parse_quote_bad_decimal_raises() -> None:
    with pytest.raises(SchwabBadResponseError):
        parse_quote(
            "X",
            {
                "quote": {
                    "lastPrice": "abc",
                    "bidPrice": 1,
                    "askPrice": 1,
                    "quoteTime": 1782556800000,
                }
            },
        )


def test_get_account_numbers_not_list_raises() -> None:
    client, _ = _client({"oops": "not a list"})
    with pytest.raises(SchwabBadResponseError):
        client.get_account_numbers()


def test_empty_candles_returns_empty_history() -> None:
    client, _ = _client({"symbol": "AAPL", "candles": []})
    history = client.get_price_history("AAPL")
    assert history.candles == ()


def test_parse_quote_flat_shape() -> None:
    q = parse_quote(
        "X", {"lastPrice": 10, "bidPrice": 9, "askPrice": 11, "quoteTime": 1782556800000}
    )
    assert q.last == Decimal("10")
