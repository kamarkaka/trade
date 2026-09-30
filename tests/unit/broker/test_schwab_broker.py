"""Tests for SchwabBroker: Broker-protocol conformance, status->Fill mapping, position/
account mapping, and READ-ONLY safe-mode refusal (M5.2). Uses a fake trading client."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

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


def _filled(order_id: str, symbol: str = "TSLA") -> SchwabOrderStatus:
    return SchwabOrderStatus(order_id, OrderStatus.FILLED, symbol, 5, 5, Decimal("200"), "FILLED")


def test_get_order_after_restart_uses_the_durable_lookup() -> None:
    # A fresh process has no in-memory record of SCHWAB-9; the durable lookup (the orders
    # table in production) still yields the originating client order id.
    client = _FakeTradingClient()
    client.set_status("SCHWAB-9", _filled("SCHWAB-9"))
    lookups: list[str] = []

    def client_id_for(broker_order_id: str) -> str | None:
        lookups.append(broker_order_id)
        return {"SCHWAB-9": "c-before-restart"}.get(broker_order_id)

    broker = SchwabBroker(client, ACCT, clock=FakeClock(NOW), client_id_for=client_id_for)  # type: ignore[arg-type]
    fill = broker.get_order("SCHWAB-9")
    assert fill.client_order_id == "c-before-restart" and fill.symbol == "TSLA"
    assert lookups == ["SCHWAB-9"]


def test_in_memory_mapping_wins_over_the_durable_lookup() -> None:
    client = _FakeTradingClient()
    broker = SchwabBroker(
        client,  # type: ignore[arg-type]
        ACCT,
        clock=FakeClock(NOW),
        client_id_for=lambda _id: pytest.fail("durable lookup not needed"),  # type: ignore[arg-type,return-value]
    )
    broker.submit_order(_order())
    client.set_status("SCHWAB-1", _filled("SCHWAB-1", symbol="AAPL"))
    assert broker.get_order("SCHWAB-1").client_order_id == "c1"


def test_get_order_without_any_mapping_has_empty_cid() -> None:
    client = _FakeTradingClient()
    client.set_status("SCHWAB-9", _filled("SCHWAB-9"))
    fill = _broker(client).get_order("SCHWAB-9")
    assert fill.client_order_id == "" and fill.symbol == "TSLA"


def test_fill_reports_schwabs_own_order_id_and_symbol() -> None:
    # If Schwab answers with a different order, the Fill must say so (the poller rejects a
    # mismatched id) instead of relabelling it as the order we asked about.
    client = _FakeTradingClient()
    broker = _broker(client)
    broker.submit_order(_order())  # SCHWAB-1 / AAPL
    client.set_status("SCHWAB-1", _filled("SCHWAB-OTHER", symbol="MSFT"))
    fill = broker.get_order("SCHWAB-1")
    assert fill.broker_order_id == "SCHWAB-OTHER" and fill.symbol == "MSFT"


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


# --- fee estimate (LR12) ---------------------------------------------------------- #


def _fee_broker(client: _FakeTradingClient) -> SchwabBroker:
    from trader.broker.sim import FeesModel

    fees = FeesModel(commission=Decimal("1"), regulatory_bps=10.0)  # 10 bps on sells
    return SchwabBroker(client, ACCT, clock=FakeClock(NOW), fees=fees)  # type: ignore[arg-type]


def _status(side: Side | None, filled: int) -> SchwabOrderStatus:
    status = OrderStatus.FILLED if filled else OrderStatus.WORKING
    return SchwabOrderStatus(
        "SCHWAB-1", status, "AAPL", 10, filled, Decimal("100"), status.value, side=side
    )


def test_buy_fill_pays_commission_only() -> None:
    client = _FakeTradingClient()
    client.set_status("SCHWAB-1", _status(Side.BUY, 10))
    assert _fee_broker(client).get_order("SCHWAB-1").fees == Decimal("1")


def test_sell_fill_pays_commission_plus_regulatory_bps() -> None:
    client = _FakeTradingClient()
    client.set_status("SCHWAB-1", _status(Side.SELL, 10))
    # 10 shares * $100 = $1000 notional; 10 bps = $1.00; + $1 commission
    assert _fee_broker(client).get_order("SCHWAB-1").fees == Decimal("2")


def test_unfilled_order_has_no_fees() -> None:
    client = _FakeTradingClient()
    client.set_status("SCHWAB-1", _status(Side.SELL, 0))
    assert _fee_broker(client).get_order("SCHWAB-1").fees == Decimal("0")


def test_side_falls_back_to_the_order_sent_then_to_commission_only() -> None:
    client = _FakeTradingClient()
    broker = _fee_broker(client)
    broker.submit_order(_order(side=Side.SELL))  # remembered side: SELL
    client.set_status("SCHWAB-1", _status(None, 10))  # Schwab's instruction unrecognized
    assert broker.get_order("SCHWAB-1").fees == Decimal("2")
    fresh = _fee_broker(client)  # after a restart nothing is remembered: commission only
    assert fresh.get_order("SCHWAB-1").fees == Decimal("1")


def test_default_broker_estimates_zero_fees() -> None:
    client = _FakeTradingClient()
    client.set_status("SCHWAB-1", _status(Side.SELL, 10))
    assert _broker(client).get_order("SCHWAB-1").fees == Decimal("0")
