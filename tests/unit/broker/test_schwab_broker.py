"""Tests for SchwabBroker: Broker-protocol conformance, status->Fill mapping, position/
account mapping, and READ-ONLY safe-mode refusal (M5.2). Uses a fake trading client."""

from datetime import UTC, datetime
from decimal import Decimal

from fakes import FakeClock
from trader.broker.schwab_broker import SchwabBroker
from trader.core import Order
from trader.core.enums import OrderStatus, OrderType, Side
from trader.core.protocols import Broker
from trader.schwab.errors import SchwabReadOnlyModeError
from trader.schwab.orders import SchwabAccountSnapshot, SchwabOrderStatus, SchwabPositionRow

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)
ACCT = "HASHEDACCT"


class _FakeTradingClient:
    def __init__(self, *, read_only: bool = False) -> None:
        self.is_read_only = read_only
        self.placed: list[tuple[str, dict]] = []
        self.canceled: list[str] = []
        self._status: dict[str, SchwabOrderStatus] = {}
        self._positions: tuple[SchwabPositionRow, ...] = ()
        self._account = SchwabAccountSnapshot(
            cash=Decimal("1000"),
            buying_power=Decimal("5000"),
            equity=Decimal("12345.67"),
            positions=(),
        )

    def place_order(self, account_hash: str, order_json: dict) -> str:
        self.placed.append((account_hash, order_json))
        return "SCHWAB-1"

    def set_status(self, order_id: str, status: SchwabOrderStatus) -> None:
        self._status[order_id] = status

    def get_order(self, account_hash: str, order_id: str) -> SchwabOrderStatus:
        return self._status[order_id]

    def cancel_order(self, account_hash: str, order_id: str) -> None:
        self.canceled.append(order_id)

    def set_positions(self, *rows: SchwabPositionRow) -> None:
        self._positions = rows

    def get_positions(self, account_hash: str) -> tuple[SchwabPositionRow, ...]:
        return self._positions

    def get_account(self, account_hash: str) -> SchwabAccountSnapshot:
        return self._account


def _broker(client: _FakeTradingClient) -> SchwabBroker:
    return SchwabBroker(client, ACCT, clock=FakeClock(NOW))  # type: ignore[arg-type]


def _order(side: Side = Side.BUY, qty: int = 10) -> Order:
    return Order("c1", "s1", "AAPL", side, qty, OrderType.MARKET)


def test_satisfies_broker_protocol() -> None:
    assert isinstance(_broker(_FakeTradingClient()), Broker)


def test_submit_builds_payload_and_returns_id() -> None:
    client = _FakeTradingClient()
    broker = _broker(client)
    broker_order_id = broker.submit_order(_order(qty=10))
    assert broker_order_id == "SCHWAB-1"
    account_hash, payload = client.placed[0]
    assert account_hash == ACCT
    assert payload["orderLegCollection"][0] == {
        "instruction": "BUY",
        "quantity": 10,
        "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
    }


def test_safe_mode_refuses_submit() -> None:
    client = _FakeTradingClient(read_only=True)
    broker = _broker(client)
    try:
        broker.submit_order(_order())
        raise AssertionError("expected a refusal in READ-ONLY safe mode")
    except SchwabReadOnlyModeError:
        pass
    assert client.placed == []  # never reached the wire


def test_status_mapping_filled_to_fill() -> None:
    client = _FakeTradingClient()
    broker = _broker(client)
    broker.submit_order(_order(qty=10))  # records client_order_id + symbol for SCHWAB-1
    client.set_status(
        "SCHWAB-1",
        SchwabOrderStatus(
            "SCHWAB-1", OrderStatus.FILLED, "AAPL", 10, 10, Decimal("150.10"), "FILLED"
        ),
    )
    fill = broker.get_order("SCHWAB-1")
    assert fill.status is OrderStatus.FILLED
    assert fill.quantity == 10 and fill.price == Decimal("150.10")
    assert fill.client_order_id == "c1" and fill.symbol == "AAPL"  # mapped from submit
    assert fill.ts == NOW


def test_status_mapping_working_is_zero_fill() -> None:
    client = _FakeTradingClient()
    broker = _broker(client)
    broker.submit_order(_order(qty=10))
    client.set_status(
        "SCHWAB-1",
        SchwabOrderStatus("SCHWAB-1", OrderStatus.WORKING, "AAPL", 10, 0, Decimal("0"), "QUEUED"),
    )
    fill = broker.get_order("SCHWAB-1")
    assert fill.status is OrderStatus.WORKING and fill.quantity == 0 and fill.price == Decimal("0")


def test_status_mapping_partial_fill() -> None:
    client = _FakeTradingClient()
    broker = _broker(client)
    broker.submit_order(_order(qty=10))
    client.set_status(
        "SCHWAB-1",
        SchwabOrderStatus(
            "SCHWAB-1", OrderStatus.PARTIAL_FILL, "AAPL", 10, 6, Decimal("150.00"), "PARTIAL_FILL"
        ),
    )
    fill = broker.get_order("SCHWAB-1")
    assert fill.status is OrderStatus.PARTIAL_FILL
    assert fill.quantity == 6 and fill.price == Decimal("150.00")  # cumulative filled qty


def test_get_order_after_restart_has_empty_cid() -> None:
    # No submit recorded this process (in-memory map empty) -> client_order_id falls back to
    # "" and symbol comes from the status (crash-safe recovery is M5.3).
    client = _FakeTradingClient()
    client.set_status(
        "SCHWAB-9",
        SchwabOrderStatus("SCHWAB-9", OrderStatus.FILLED, "TSLA", 5, 5, Decimal("200"), "FILLED"),
    )
    fill = _broker(client).get_order("SCHWAB-9")
    assert fill.client_order_id == "" and fill.symbol == "TSLA"


def test_cancel_delegates() -> None:
    client = _FakeTradingClient()
    _broker(client).cancel_order("SCHWAB-1")
    assert client.canceled == ["SCHWAB-1"]


def test_get_positions_maps_signed() -> None:
    client = _FakeTradingClient()
    client.set_positions(
        SchwabPositionRow("AAPL", 10, Decimal("150"), Decimal("1500")),
        SchwabPositionRow("TSLA", -5, Decimal("200"), Decimal("-1000")),
    )
    positions = {p.symbol: p.quantity for p in _broker(client).get_positions()}
    assert positions == {"AAPL": 10, "TSLA": -5}


def test_get_account_maps_balances() -> None:
    account = _broker(_FakeTradingClient()).get_account()
    assert account.cash == Decimal("1000")
    assert account.buying_power == Decimal("5000")
    assert account.equity == Decimal("12345.67")
