"""A deterministic, in-memory Broker test double with configurable behaviors.

Supports the failure modes later milestones need: a one-shot submit failure
(simulating a timeout / unknown response for idempotency tests) and optional
broker-side dedup by client order id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from trader.core import Account, Fill, Order, OrderStatus, Position

_TS = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
_DEFAULT_CASH = Decimal("100000")


class FakeBroker:
    """Implements ``trader.core.protocols.Broker`` in memory."""

    def __init__(
        self,
        account: Account | None = None,
        *,
        default_fill_price: Decimal = Decimal("100"),
        fill_status: OrderStatus = OrderStatus.FILLED,
    ) -> None:
        self._account = account or Account(
            cash=_DEFAULT_CASH, buying_power=_DEFAULT_CASH, equity=_DEFAULT_CASH
        )
        self._positions: dict[str, Position] = {}
        self._fills: dict[str, Fill] = {}  # broker_order_id -> Fill
        self._by_client: dict[str, str] = {}  # client_order_id -> broker_order_id
        self._seq = 0
        # observable + configurable knobs for tests
        self.submitted: list[Order] = []
        self.cancelled: list[str] = []
        self.default_fill_price = default_fill_price
        self.fill_status = fill_status
        self.fill_quantity: int | None = None  # < order qty => PARTIAL_FILL; None => full
        self.fail_next_submit = False  # raise once (simulate timeout / unknown outcome)
        self.record_on_timeout = False  # on timeout, still record (request landed, response lost)
        self.dedupe_by_client_id = False  # broker-side idempotency
        self.ts = _TS

    def submit_order(self, order: Order) -> str:
        self.submitted.append(order)
        if self.dedupe_by_client_id and order.client_order_id in self._by_client:
            return self._by_client[order.client_order_id]
        if self.fail_next_submit:
            self.fail_next_submit = False
            if self.record_on_timeout:
                self._record_fill(order)  # the order reached the broker; the response was lost
            raise TimeoutError("simulated broker timeout (outcome unknown)")
        return self._record_fill(order)

    def _record_fill(self, order: Order) -> str:
        self._seq += 1
        broker_order_id = f"b-{self._seq}"
        price = order.limit_price or self.default_fill_price
        filled = self.fill_quantity if self.fill_quantity is not None else order.quantity
        status = OrderStatus.PARTIAL_FILL if filled < order.quantity else self.fill_status
        self._fills[broker_order_id] = Fill(
            client_order_id=order.client_order_id,
            broker_order_id=broker_order_id,
            symbol=order.symbol,
            quantity=filled,
            price=price,
            fees=Decimal("0"),
            ts=self.ts,
            status=status,
        )
        self._by_client[order.client_order_id] = broker_order_id
        return broker_order_id

    def get_order(self, broker_order_id: str) -> Fill:
        return self._fills[broker_order_id]

    def find_by_client_id(self, client_order_id: str) -> Fill | None:
        """Look up a recorded fill by client order id (reconciliation/idempotency)."""
        broker_order_id = self._by_client.get(client_order_id)
        return self._fills.get(broker_order_id) if broker_order_id else None

    def cancel_order(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)

    def get_positions(self) -> Sequence[Position]:
        return list(self._positions.values())

    def get_account(self) -> Account:
        return self._account

    # --- test controls ---
    def set_position(self, position: Position) -> None:
        self._positions[position.symbol] = position

    def set_account(self, account: Account) -> None:
        self._account = account
