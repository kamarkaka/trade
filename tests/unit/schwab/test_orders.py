"""Contract tests for the Schwab order + account WRITE endpoints (respx; no live calls).

Covers the §8.5 facts: order JSON shape, 201/Location id parsing, status-enum mapping,
cancel/replace, and account/positions parsing."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.core.enums import OrderStatus, OrderType, Side
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import ACCOUNTS_PATH
from trader.schwab.errors import SchwabBadResponseError
from trader.schwab.http import SchwabHttp
from trader.schwab.orders import (
    SchwabTradingClient,
    build_order_json,
    map_order_status,
    parse_account,
    parse_order_status,
)

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
ACCT = "HASHEDACCT"
ORDERS_URL = f"{ACCOUNTS_PATH}/{ACCT}/orders"
FIXTURES = Path(__file__).parents[2] / "fixtures" / "schwab"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _client(tmp_path: Path, http_client: httpx.Client) -> SchwabTradingClient:
    cfg = SchwabClientConfig(app_key="K", app_secret="S", token_store_path=tmp_path / "t.sqlite")
    store = TokenStore(tmp_path / "t.sqlite")
    store.save(TokenSet("ACC", "REF", NOW + timedelta(seconds=1800), NOW))
    http = SchwabHttp(cfg, http_client, store, clock=FakeClock(NOW), sleep=lambda _s: None)
    return SchwabTradingClient(http)


# --- order JSON shape (§8.5) ------------------------------------------------- #


def test_order_json_shape_market() -> None:
    body = build_order_json(symbol="AAPL", side=Side.BUY, quantity=10, order_type=OrderType.MARKET)
    assert body == {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 10,
                "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
            }
        ],
    }
    assert "price" not in body  # MARKET carries no price


def test_order_json_shape_limit() -> None:
    body = build_order_json(
        symbol="MSFT",
        side=Side.SELL,
        quantity=5,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("123.45"),
    )
    assert body["orderType"] == "LIMIT"
    assert body["price"] == "123.45"  # string, no float
    assert body["orderLegCollection"][0]["instruction"] == "SELL"


def test_order_json_validation() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_order_json(symbol="A", side=Side.BUY, quantity=0, order_type=OrderType.MARKET)
    with pytest.raises(ValueError, match="LIMIT order requires"):
        build_order_json(symbol="A", side=Side.BUY, quantity=1, order_type=OrderType.LIMIT)
    with pytest.raises(ValueError, match="MARKET order must not"):
        build_order_json(
            symbol="A",
            side=Side.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
            limit_price=Decimal("1"),
        )


# --- place / replace: 201 Location parsing ---------------------------------- #


@respx.mock
def test_place_order_parses_location_header(tmp_path: Path) -> None:
    route = respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(201, headers={"Location": f"{ORDERS_URL}/1003490104"})
    )
    with httpx.Client() as c:
        order_id = _client(tmp_path, c).place_order(
            ACCT,
            build_order_json(
                symbol="AAPL", side=Side.BUY, quantity=10, order_type=OrderType.MARKET
            ),
        )
    assert order_id == "1003490104"  # from Location, not the (empty) body
    posted = json.loads(route.calls.last.request.content)
    assert posted["orderLegCollection"][0]["instruction"] == "BUY"  # payload shape sent over wire


@respx.mock
def test_place_order_no_location_raises(tmp_path: Path) -> None:
    respx.post(ORDERS_URL).mock(return_value=httpx.Response(201))  # no Location header
    with httpx.Client() as c, pytest.raises(SchwabBadResponseError, match="Location"):
        _client(tmp_path, c).place_order(
            ACCT,
            build_order_json(symbol="AAPL", side=Side.BUY, quantity=1, order_type=OrderType.MARKET),
        )


@respx.mock
def test_place_order_location_with_query_string(tmp_path: Path) -> None:
    respx.post(ORDERS_URL).mock(
        return_value=httpx.Response(201, headers={"Location": f"{ORDERS_URL}/1003490104?x=1"})
    )
    with httpx.Client() as c:
        order_id = _client(tmp_path, c).place_order(
            ACCT,
            build_order_json(symbol="AAPL", side=Side.BUY, quantity=1, order_type=OrderType.MARKET),
        )
    assert order_id == "1003490104"  # query string stripped, not part of the id


@respx.mock
def test_place_order_not_auto_retried_on_5xx(tmp_path: Path) -> None:
    # SAFETY: a POST that 5xx's must NOT be re-sent (a 5xx can arrive after the order was
    # accepted -> a blind retry would double the real position). Surface the error instead.
    from trader.schwab.errors import SchwabServerError

    route = respx.post(ORDERS_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as c, pytest.raises(SchwabServerError):
        _client(tmp_path, c).place_order(
            ACCT,
            build_order_json(symbol="AAPL", side=Side.BUY, quantity=1, order_type=OrderType.MARKET),
        )
    assert route.call_count == 1  # exactly one POST attempt, never retried


@respx.mock
def test_cancel_is_retried_on_5xx(tmp_path: Path) -> None:
    # DELETE (cancel) is idempotent, so it IS safe to retry on a transient 5xx.
    route = respx.delete(f"{ORDERS_URL}/1003490104").mock(
        side_effect=[httpx.Response(503), httpx.Response(200)]
    )
    with httpx.Client() as c:
        _client(tmp_path, c).cancel_order(ACCT, "1003490104")
    assert route.call_count == 2  # retried once, then succeeded


def test_parse_order_status_rejects_fractional_quantity() -> None:
    with pytest.raises(SchwabBadResponseError, match="integer"):
        parse_order_status({"orderId": "1", "status": "FILLED", "filledQuantity": 3.7})


@respx.mock
def test_replace_order_returns_new_id(tmp_path: Path) -> None:
    route = respx.put(f"{ORDERS_URL}/1003490104").mock(
        return_value=httpx.Response(201, headers={"Location": f"{ORDERS_URL}/1003490200"})
    )
    with httpx.Client() as c:
        new_id = _client(tmp_path, c).replace_order(
            ACCT,
            "1003490104",
            build_order_json(
                symbol="AAPL",
                side=Side.BUY,
                quantity=10,
                order_type=OrderType.LIMIT,
                limit_price=Decimal("150"),
            ),
        )
    assert new_id == "1003490200" and route.called


# --- poll status: enum mapping ---------------------------------------------- #


def test_poll_status_maps_enums() -> None:
    filled = parse_order_status(_fixture("order_status_filled.json"))
    assert filled.status is OrderStatus.FILLED and filled.filled_quantity == 10
    working = parse_order_status(_fixture("order_status_working.json"))
    assert working.status is OrderStatus.WORKING  # QUEUED (in-flight) maps to WORKING
    # unknown / in-flight statuses NEVER map to a fill
    assert map_order_status("AWAITING_MANUAL_REVIEW") is OrderStatus.WORKING
    assert map_order_status("CANCELED") is OrderStatus.CANCELED
    assert map_order_status("rejected") is OrderStatus.REJECTED  # case-insensitive


@respx.mock
def test_get_order_polls(tmp_path: Path) -> None:
    respx.get(f"{ORDERS_URL}/1003490104").mock(
        return_value=httpx.Response(200, json=_fixture("order_status_filled.json"))
    )
    with httpx.Client() as c:
        status = _client(tmp_path, c).get_order(ACCT, "1003490104")
    assert status.order_id == "1003490104" and status.status is OrderStatus.FILLED


# --- cancel ----------------------------------------------------------------- #


@respx.mock
def test_cancel_order(tmp_path: Path) -> None:
    route = respx.delete(f"{ORDERS_URL}/1003490104").mock(return_value=httpx.Response(200))
    with httpx.Client() as c:
        _client(tmp_path, c).cancel_order(ACCT, "1003490104")
    assert route.called and route.calls.last.request.method == "DELETE"


# --- account / positions ---------------------------------------------------- #


def test_parse_account_signed_positions() -> None:
    snap = parse_account(_fixture("account_with_positions.json"))
    assert snap.equity == Decimal("12345.67")
    assert snap.cash == Decimal("1000.00") and snap.buying_power == Decimal("5000.00")
    by_symbol = {p.symbol: p.quantity for p in snap.positions}
    assert by_symbol == {"AAPL": 10, "TSLA": -5}  # short reported as negative


@respx.mock
def test_get_account_fields_positions(tmp_path: Path) -> None:
    route = respx.get(f"{ACCOUNTS_PATH}/{ACCT}").mock(
        return_value=httpx.Response(200, json=_fixture("account_with_positions.json"))
    )
    with httpx.Client() as c:
        snap = _client(tmp_path, c).get_account(ACCT)
    assert route.calls.last.request.url.params["fields"] == "positions"
    assert len(snap.positions) == 2
