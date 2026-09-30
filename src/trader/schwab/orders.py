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
from datetime import UTC, datetime, timedelta
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
    symbol: str  # from the order's leg ("" if absent)
    quantity: int
    filled_quantity: int
    average_price: Decimal  # weighted avg execution price (0 if nothing filled yet)
    raw_status: str
    # Intent fields used to match a listed order back to a local order (reconciliation).
    # An empty string / None means UNKNOWN (absent or unparseable), never "different".
    entered_time: datetime | None = None  # when Schwab accepted the order (tz-aware UTC)
    instruction: str = ""  # the leg's raw instruction, upper-cased (BUY, SELL, SELL_SHORT…)
    side: Side | None = None  # the side of the book that instruction trades
    order_type: str = ""  # raw Schwab orderType, upper-cased (MARKET, LIMIT…)
    price: Decimal | None = None  # the order's price field (None for MARKET / absent)
    leg_quantity: int = 0  # the first leg's quantity (0 if absent)
    duration: str = ""  # DAY, GTC… (upper-cased; "" if absent)
    session: str = ""  # NORMAL, AM, PM, SEAMLESS… (upper-cased; "" if absent)
    strategy_type: str = ""  # orderStrategyType: SINGLE, OCO, TRIGGER… ("" if absent)


def _first_leg(data: Any) -> dict[str, Any] | None:
    legs = data.get("orderLegCollection") if isinstance(data, dict) else None
    if isinstance(legs, list) and legs and isinstance(legs[0], dict):
        return legs[0]
    return None


def _symbol_of(data: Any) -> str:
    leg = _first_leg(data)
    instrument = leg.get("instrument") if leg is not None else None
    if isinstance(instrument, dict) and "symbol" in instrument:
        return str(instrument["symbol"])
    return ""


# Schwab equity instructions -> the side of the book they trade. [VERIFY] the short-sale
# spellings. We only ever send BUY/SELL; match OUR orders on ``instruction`` exactly and use
# ``side`` only to spot orders that could conflict with one (e.g. a manual SELL_SHORT).
_SIDE_BY_INSTRUCTION: dict[str, Side] = {
    "BUY": Side.BUY,
    "BUY_TO_COVER": Side.BUY,
    "SELL": Side.SELL,
    "SELL_SHORT": Side.SELL,
}


def _instruction_of(data: Any) -> str:
    leg = _first_leg(data)
    instruction = leg.get("instruction") if leg is not None else None
    return str(instruction).upper() if instruction else ""


# enteredTime arrives like "2026-06-29T14:30:05+0000" [VERIFY]; %z also accepts "Z"/"+00:00".
_ENTERED_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z")


def _entered_time_of(data: Any) -> datetime | None:
    raw = data.get("enteredTime") if isinstance(data, dict) else None
    if raw is None:
        return None
    for fmt in _ENTERED_TIME_FORMATS:
        try:
            return datetime.strptime(str(raw), fmt).astimezone(UTC)
        except ValueError:
            continue
    raise SchwabBadResponseError(f"unparseable enteredTime: {raw!r}")


def _average_fill_price(data: Any) -> Decimal:
    """Quantity-weighted average over the execution legs (0 if there are none)."""
    total_qty = Decimal(0)
    total_cost = Decimal(0)
    for activity in data.get("orderActivityCollection", []) or []:
        if not isinstance(activity, dict):
            continue
        for leg in activity.get("executionLegs", []) or []:
            if not isinstance(leg, dict):
                continue
            qty = _dec(leg.get("quantity", 0), "executionLeg.quantity")
            price = _dec(leg.get("price", 0), "executionLeg.price")
            total_qty += qty
            total_cost += qty * price
    return (total_cost / total_qty) if total_qty > 0 else Decimal(0)


def _intent_fields(data: Any, order_type: str) -> tuple[datetime | None, Decimal | None, int]:
    price = data.get("price") if order_type != "MARKET" else None
    leg = _first_leg(data)
    leg_quantity = _int(leg.get("quantity", 0), "leg quantity") if leg is not None else 0
    return (
        _entered_time_of(data),
        (_dec(price, "price") if price is not None else None),
        leg_quantity,
    )


def parse_order_status(data: Any, *, strict_intent: bool = False) -> SchwabOrderStatus:
    """Parse one order object.

    The status/quantity/fill fields are always strict (a bad value raises). The intent-only
    fields (``enteredTime``, ``price``, the leg quantity) are parsed leniently by default —
    an unexpected format becomes None/0 so it can never break status POLLING — and strictly
    for reconciliation listings (``strict_intent=True``), where a silently-dropped timestamp
    could widen a match window."""
    raw = str(_require(data, "status"))
    order_type = str(data.get("orderType") or "").upper()
    try:
        entered_time, price, leg_quantity = _intent_fields(data, order_type)
    except SchwabBadResponseError:
        if strict_intent:
            raise
        entered_time, price, leg_quantity = None, None, 0
    instruction = _instruction_of(data)
    return SchwabOrderStatus(
        order_id=str(_require(data, "orderId")),
        status=map_order_status(raw),
        symbol=_symbol_of(data),
        quantity=_int(data.get("quantity", 0), "quantity"),
        filled_quantity=_int(data.get("filledQuantity", 0), "filledQuantity"),
        average_price=_average_fill_price(data),
        raw_status=raw,
        entered_time=entered_time,
        instruction=instruction,
        side=_SIDE_BY_INSTRUCTION.get(instruction),
        order_type=order_type,
        price=price,
        leg_quantity=leg_quantity,
        duration=str(data.get("duration") or "").upper(),
        session=str(data.get("session") or "").upper(),
        strategy_type=str(data.get("orderStrategyType") or "").upper(),
    )


@dataclass(frozen=True)
class SchwabUnparsedOrder:
    """A listed order that could not be parsed. Kept (not dropped) so a reconciler can
    treat it as "might be ours" when it could match — an order hidden by a parse failure
    must never be mistaken for an absent one."""

    order_id: str  # "" if absent
    symbol: str  # best effort from the first leg ("" if unknown)
    error: str
    entered_time: datetime | None = None  # best effort (None if absent or unparseable)


@dataclass(frozen=True)
class OrderListing:
    orders: tuple[SchwabOrderStatus, ...]
    unparsed: tuple[SchwabUnparsedOrder, ...] = ()


def _lenient_entered(item: Any) -> datetime | None:
    try:
        return _entered_time_of(item)
    except SchwabBadResponseError:
        return None


def parse_order_list(data: Any) -> OrderListing:
    """Parse the list-orders response (a JSON array of order objects). One malformed order
    (e.g. a fractional-share Stock Slice entered elsewhere) is isolated in ``unparsed``
    instead of failing the whole listing."""
    if not isinstance(data, list):
        raise SchwabBadResponseError("list-orders response is not a JSON array")
    orders: list[SchwabOrderStatus] = []
    unparsed: list[SchwabUnparsedOrder] = []
    for item in data:
        try:
            orders.append(parse_order_status(item, strict_intent=True))
        except (SchwabBadResponseError, AttributeError, TypeError) as exc:
            order_id = str(item.get("orderId", "")) if isinstance(item, dict) else ""
            unparsed.append(
                SchwabUnparsedOrder(order_id, _symbol_of(item), str(exc), _lenient_entered(item))
            )
    return OrderListing(tuple(orders), tuple(unparsed))


def _entered_time_param(value: datetime, *, round_up: bool = False) -> str:
    """Format a list-orders time bound: ``yyyy-MM-dd'T'HH:mm:ss.SSSZ`` in UTC [VERIFY].
    Sub-millisecond precision is floored, or ceiled for an upper bound (``round_up``) so
    the requested window is never narrowed."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("list-orders time bounds must be timezone-aware")
    utc = value.astimezone(UTC)
    if round_up and utc.microsecond % 1000:
        utc += timedelta(microseconds=1000 - utc.microsecond % 1000)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


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
    start_of_day_equity: Decimal | None = None  # initialBalances.liquidationValue [VERIFY]
    round_trips: int | None = None  # securitiesAccount.roundTrips: PDT day-trades [VERIFY]


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
    # The start-of-day balances [VERIFY: initialBalances.liquidationValue is today's opening
    # account value]. Optional: absent => the daily counters capture it themselves.
    initial = account.get("initialBalances")
    sod = initial.get("liquidationValue") if isinstance(initial, dict) else None
    return SchwabAccountSnapshot(
        cash=_dec(balances.get("cashBalance", 0), "cashBalance"),
        buying_power=_dec(balances.get("buyingPower", 0), "buyingPower"),
        equity=_dec(_require(balances, "liquidationValue"), "liquidationValue"),
        positions=tuple(rows),
        start_of_day_equity=(
            _dec(sod, "initialBalances.liquidationValue") if sod is not None else None
        ),
        # [VERIFY: roundTrips is the day-trade count over the rolling PDT window]
        round_trips=(
            _int(account["roundTrips"], "roundTrips")
            if account.get("roundTrips") is not None
            else None
        ),
    )


# --- client ------------------------------------------------------------------------------ #


class SchwabTradingClient:
    """Order placement/replace/cancel + status poll + balances/positions (hashed account id)."""

    def __init__(self, http: SchwabHttp) -> None:
        self._http = http

    @property
    def is_read_only(self) -> bool:
        """True when the transport is in READ-ONLY safe mode (dead refresh token)."""
        return self._http.is_read_only

    def _orders_path(self, account_hash: str) -> str:
        return f"{ACCOUNTS_PATH}/{account_hash}/orders"

    def place_order(self, account_hash: str, order_json: dict[str, Any]) -> str:
        """POST a new order; return the order id from the 201 ``Location`` header.

        NOT IDEMPOTENT: calling this twice places TWO real orders. On a timeout/unknown
        response, the transport deliberately does NOT auto-retry the POST — the idempotent
        placement layer (execution.idempotency) sends each client_order_id at most once and
        settles unknown outcomes by reconciliation.
        Never call this directly from ad-hoc/daemon code."""
        resp = self._http.request("POST", self._orders_path(account_hash), json=order_json)
        return self._order_id_from_location(resp)

    def get_order(self, account_hash: str, order_id: str) -> SchwabOrderStatus:
        """Poll a single order's status (never assume a synchronous fill)."""
        data = self._http.get_json(f"{self._orders_path(account_hash)}/{order_id}")
        return parse_order_status(data)

    def get_orders(
        self,
        account_hash: str,
        *,
        from_entered: datetime,
        to_entered: datetime,
        max_results: int = 3000,
    ) -> OrderListing:
        """List the account's orders entered between ``from_entered`` and ``to_entered``
        (any status).

        Read-only (GET, so the transport may retry it). Used by reconciliation to find an
        order whose placement outcome is unknown. [VERIFY] whether the bounds are inclusive
        (callers should pad the window for clock skew) and the ~60-day look-back limit on
        ``fromEnteredTime`` (an older bound is rejected with a 400). Only top-level orders
        are returned; we place SINGLE orders only, so child orders of complex strategies are
        not traversed. Raises ``SchwabBadResponseError`` if the result may be truncated
        (``max_results`` reached) — a cut-off listing must never read as "absent"."""
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        params: dict[str, Any] = {  # formatting validates tz-awareness before comparing
            "fromEnteredTime": _entered_time_param(from_entered),
            "toEnteredTime": _entered_time_param(to_entered, round_up=True),
            "maxResults": max_results,
        }
        if to_entered < from_entered:
            raise ValueError("to_entered must be on or after from_entered")
        listing = parse_order_list(
            self._http.get_json(self._orders_path(account_hash), params=params)
        )
        if len(listing.orders) + len(listing.unparsed) >= max_results:
            raise SchwabBadResponseError(
                f"list-orders returned {max_results}+ orders; the result may be truncated"
            )
        return listing

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
    "OrderListing",
    "SchwabAccountSnapshot",
    "SchwabOrderStatus",
    "SchwabPositionRow",
    "SchwabTradingClient",
    "SchwabUnparsedOrder",
    "build_order_json",
    "map_order_status",
    "parse_account",
    "parse_order_list",
    "parse_order_status",
]
