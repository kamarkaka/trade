"""Schwab order + account WRITE endpoints on the first-party client (design §8.5).

Kept SEPARATE from the read-only ``SchwabClient`` so reads and writes are cleanly
partitioned: ``SchwabTradingClient`` holds the only methods that place/replace/cancel orders
and read balances/positions. It is contract-tested with respx only — nothing wires it to the
daemon until the SchwabBroker (M5.2) + the go-live double-confirm (M5.6/M5.7).

Every endpoint path, payload shape, the 201/``Location`` behavior, and the status enums are
**[VERIFY]** against the live Schwab portal (§8.7); all such facts are isolated here.

Safety choices for real money:
- ``place_order``/``replace_order`` read the new id from the **201 Location header**, never
  the body, and NEVER assume a synchronous fill (the caller polls ``get_order``).
- Unknown / in-flight order statuses map to ``WORKING`` (keep polling), never to a fill.
- Prices are serialized as strings (no binary float in money).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from trader.core.enums import OrderStatus, OrderType, Side

from .constants import ACCOUNTS_PATH
from .errors import SchwabBadResponseError
from .http import SchwabHttp

# --- small parse helpers (local; mirror models.py to keep this module self-contained) ----- #


def _require(mapping: Any, key: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise SchwabBadResponseError(f"missing key {key!r} in Schwab response")
    return mapping[key]


def _int(value: Any, field: str) -> int:
    # Fail loud on a non-integral quantity rather than silently truncating: get_json parses
    # JSON numbers as Decimal, so a stray 3.7 must NOT become 3 shares.
    try:
        dec = Decimal(str(value))
    except Exception as exc:
        raise SchwabBadResponseError(f"{field} is not an int: {value!r}") from exc
    if dec != dec.to_integral_value():
        raise SchwabBadResponseError(f"{field} is not an integer: {value!r}")
    return int(dec)


def _dec(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # InvalidOperation et al.
        raise SchwabBadResponseError(f"{field} is not a number: {value!r}") from exc


# --- status mapping ---------------------------------------------------------------------- #

# Schwab order-status string -> normalized core OrderStatus. Anything not explicitly terminal
# (accepted/queued/new/pending/awaiting/replaced) maps to WORKING so the caller keeps polling
# and NEVER assumes a fill — the safe default for real money.
_STATUS_MAP: dict[str, OrderStatus] = {
    "FILLED": OrderStatus.FILLED,
    "PARTIAL_FILL": OrderStatus.PARTIAL_FILL,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "WORKING": OrderStatus.WORKING,
}


def map_order_status(raw: str) -> OrderStatus:
    """Map a Schwab status string to a core ``OrderStatus`` (unknown/in-flight -> WORKING)."""
    return _STATUS_MAP.get(raw.upper(), OrderStatus.WORKING)


# --- order JSON builder (§8.5) ----------------------------------------------------------- #

_INSTRUCTION = {Side.BUY: "BUY", Side.SELL: "SELL"}


def build_order_json(
    *,
    symbol: str,
    side: Side,
    quantity: int,
    order_type: OrderType,
    limit_price: Decimal | None = None,
    duration: str = "DAY",
    session: str = "NORMAL",
) -> dict[str, Any]:
    """Build the §8.5 single-leg equity order payload. Price travels only on LIMIT orders
    and is serialized as a string (money never passes through binary float)."""
    if quantity <= 0:
        raise ValueError(f"order quantity must be positive, got {quantity}")
    body: dict[str, Any] = {
        "orderType": order_type.value,
        "session": session,
        "duration": duration,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": _INSTRUCTION[side],
                "quantity": quantity,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
    }
    if order_type is OrderType.LIMIT:
        if limit_price is None or limit_price <= 0:
            raise ValueError("LIMIT order requires a positive limit_price")
        body["price"] = format(limit_price, "f")  # string, no float
    elif limit_price is not None:
        raise ValueError("MARKET order must not carry a limit_price")
    return body


# --- typed responses --------------------------------------------------------------------- #


@dataclass(frozen=True)
class SchwabOrderStatus:
    order_id: str
    status: OrderStatus
    quantity: int
    filled_quantity: int
    raw_status: str


def parse_order_status(data: Any) -> SchwabOrderStatus:
    raw = str(_require(data, "status"))
    return SchwabOrderStatus(
        order_id=str(_require(data, "orderId")),
        status=map_order_status(raw),
        quantity=_int(data.get("quantity", 0), "quantity"),
        filled_quantity=_int(data.get("filledQuantity", 0), "filledQuantity"),
        raw_status=raw,
    )


@dataclass(frozen=True)
class SchwabPositionRow:
    symbol: str
    quantity: int  # signed: long positive, short negative
    average_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class SchwabAccountSnapshot:
    cash: Decimal
    buying_power: Decimal
    equity: Decimal
    positions: tuple[SchwabPositionRow, ...]


def parse_account(data: Any) -> SchwabAccountSnapshot:
    """Parse ``GET accounts/{hash}?fields=positions`` into balances + signed positions."""
    account = _require(data, "securitiesAccount")
    balances = _require(account, "currentBalances")
    rows: list[SchwabPositionRow] = []
    for p in account.get("positions", []) or []:
        instrument = _require(p, "instrument")
        long_qty = _int(p.get("longQuantity", 0), "longQuantity")
        short_qty = _int(p.get("shortQuantity", 0), "shortQuantity")
        rows.append(
            SchwabPositionRow(
                symbol=str(_require(instrument, "symbol")),
                quantity=long_qty - short_qty,  # net signed
                average_price=_dec(p.get("averagePrice", 0), "averagePrice"),
                market_value=_dec(p.get("marketValue", 0), "marketValue"),
            )
        )
    return SchwabAccountSnapshot(
        cash=_dec(balances.get("cashBalance", 0), "cashBalance"),
        buying_power=_dec(balances.get("buyingPower", 0), "buyingPower"),
        equity=_dec(_require(balances, "liquidationValue"), "liquidationValue"),
        positions=tuple(rows),
    )


# --- client ------------------------------------------------------------------------------ #


class SchwabTradingClient:
    """Order placement/replace/cancel + status poll + balances/positions (hashed account id)."""

    def __init__(self, http: SchwabHttp) -> None:
        self._http = http

    def _orders_path(self, account_hash: str) -> str:
        return f"{ACCOUNTS_PATH}/{account_hash}/orders"

    def place_order(self, account_hash: str, order_json: dict[str, Any]) -> str:
        """POST a new order; return the order id from the 201 ``Location`` header.

        NOT IDEMPOTENT: calling this twice places TWO real orders. On a timeout/unknown
        response, the transport deliberately does NOT auto-retry the POST — the caller must
        reconcile-before-resend and reuse the client_order_id (the M5.3 idempotent wrapper).
        Never call this directly from ad-hoc/daemon code."""
        resp = self._http.request("POST", self._orders_path(account_hash), json=order_json)
        return self._order_id_from_location(resp)

    def get_order(self, account_hash: str, order_id: str) -> SchwabOrderStatus:
        """Poll a single order's status (never assume a synchronous fill)."""
        data = self._http.get_json(f"{self._orders_path(account_hash)}/{order_id}")
        return parse_order_status(data)

    def cancel_order(self, account_hash: str, order_id: str) -> None:
        self._http.request("DELETE", f"{self._orders_path(account_hash)}/{order_id}")

    def replace_order(self, account_hash: str, order_id: str, order_json: dict[str, Any]) -> str:
        """PUT a replacement; return the NEW order id from the ``Location`` header."""
        resp = self._http.request(
            "PUT", f"{self._orders_path(account_hash)}/{order_id}", json=order_json
        )
        return self._order_id_from_location(resp)

    def get_account(self, account_hash: str) -> SchwabAccountSnapshot:
        data = self._http.get_json(
            f"{ACCOUNTS_PATH}/{account_hash}", params={"fields": "positions"}
        )
        return parse_account(data)

    def get_positions(self, account_hash: str) -> tuple[SchwabPositionRow, ...]:
        return self.get_account(account_hash).positions

    @staticmethod
    def _order_id_from_location(resp: httpx.Response) -> str:
        location = resp.headers.get("Location")  # httpx headers are case-insensitive
        if not location:
            raise SchwabBadResponseError("order placement returned no Location header")
        # Extract the id after ".../orders/", stripping any query string (urlsplit drops it)
        # and trailing slash. Reject a degenerate Location rather than return a wrong id.
        path = urlsplit(str(location)).path
        marker = "/orders/"
        idx = path.rfind(marker)
        order_id = path[idx + len(marker) :].strip("/") if idx != -1 else ""
        if not order_id or "/" in order_id:
            raise SchwabBadResponseError(f"could not parse order id from Location {location!r}")
        return order_id


__all__ = [
    "SchwabAccountSnapshot",
    "SchwabOrderStatus",
    "SchwabPositionRow",
    "SchwabTradingClient",
    "build_order_json",
    "map_order_status",
    "parse_account",
    "parse_order_status",
]
