"""SchwabBroker — the live counterpart of SimBroker (design §5).

Adapts the first-party Schwab trading client to the core ``Broker`` protocol, so the
orchestrator places real orders through the SAME abstraction as the simulator. It is
**safe-mode aware**: in READ-ONLY safe mode (dead refresh token) ``submit_order`` refuses
and raises a typed error rather than silently dropping the order.

Idempotency (write-ahead row, one send per client_order_id, reconciliation of unknown
outcomes) is layered ABOVE this broker (execution.idempotency) — and the transport already
refuses to auto-retry the order POST (M5.1), so a duplicate order is never created at this
layer. Schwab does not echo our ``client_order_id``,
so each returned broker order id is mapped back to the originating id: from memory for orders
placed by this process, and through an injected durable lookup (the ``orders`` table) for
orders placed before a restart. A ``Fill`` reports Schwab's own order id and symbol, so a
response for a different order is visible to the caller rather than masked.

SAFETY: this is the real-money order path. It is only constructed by the go-live wiring
(M5.6) after the double-confirm; the paper daemon still uses SimBroker and refuses mode=live.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from trader.broker.sim import FeesModel
from trader.core import Account, Fill, Order, Position
from trader.core.enums import Side
from trader.core.protocols import Clock
from trader.observability.logging import get_logger
from trader.schwab.errors import SchwabReadOnlyModeError
from trader.schwab.orders import SchwabTradingClient, build_order_json


class SchwabBroker:
    """Live broker over the Schwab trading client (implements the core ``Broker`` protocol)."""

    def __init__(
        self,
        client: SchwabTradingClient,
        account_hash: str,
        *,
        clock: Clock,
        client_id_for: Callable[[str], str | None] | None = None,
        fees: FeesModel | None = None,
    ) -> None:
        self._client = client
        self._account = account_hash
        self._clock = clock
        # broker_order_id -> (client_order_id, symbol, side) for orders placed by THIS
        # process. Schwab doesn't echo our client id; ``client_id_for`` (a durable lookup
        # over the orders table) answers for orders placed before a restart.
        self._submitted: dict[str, tuple[str, str, Side]] = {}
        self._client_id_for = client_id_for
        # Fees are ESTIMATED with the same model as the simulator (commission + sell-side
        # regulatory bps); a true-up from Schwab's transaction records is not implemented.
        self._fees = fees or FeesModel()
        self._log = get_logger("broker.schwab")

    def submit_order(self, order: Order) -> str:
        if self._client.is_read_only:
            # Never silently drop: refuse loudly so the caller/alerting sees it.
            raise SchwabReadOnlyModeError(
                f"refusing to submit {order.client_order_id}: client is in READ-ONLY safe mode"
            )
        order_json = build_order_json(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            limit_price=order.limit_price,
        )
        broker_order_id = self._client.place_order(self._account, order_json)
        self._submitted[broker_order_id] = (order.client_order_id, order.symbol, order.side)
        self._log.info(
            "order submitted",
            cid=order.client_order_id,
            broker_order_id=broker_order_id,
            symbol=order.symbol,
        )
        return broker_order_id

    def get_order(self, broker_order_id: str) -> Fill:
        status = self._client.get_order(self._account, broker_order_id)
        client_order_id, sent_symbol, sent_side = self._submitted.get(
            broker_order_id, ("", "", None)
        )
        if not client_order_id and self._client_id_for is not None:
            client_order_id = self._client_id_for(broker_order_id) or ""
        side = status.side or sent_side  # Schwab's instruction survives a restart
        if sent_symbol and status.symbol and sent_symbol != status.symbol:
            self._log.error(
                "order status symbol differs from the order sent",
                broker_order_id=broker_order_id,
                sent=sent_symbol,
                reported=status.symbol,
            )
        return Fill(
            client_order_id=client_order_id,
            # Report Schwab's own id and symbol: a response for another order must stay
            # visible (the poller rejects a mismatched id) rather than be relabelled.
            broker_order_id=status.order_id or broker_order_id,
            symbol=status.symbol or sent_symbol,
            quantity=status.filled_quantity,  # 0 while still WORKING (valid; no fill yet)
            price=status.average_price,  # 0 until something fills
            fees=self._estimate_fees(status.filled_quantity, status.average_price, side),
            ts=self._clock.now(),
            status=status.status,
        )

    def _estimate_fees(self, quantity: int, price: Decimal, side: Side | None) -> Decimal:
        if quantity == 0:
            return Decimal(0)
        if side is None:  # unknown instruction: commission only, never guess a side
            self._log.warning("order side unknown; regulatory fees not estimated")
            return self._fees.commission
        return self._fees.fee(Decimal(quantity) * price, side)

    def cancel_order(self, broker_order_id: str) -> None:
        self._client.cancel_order(self._account, broker_order_id)

    def get_positions(self) -> list[Position]:
        return [
            Position(p.symbol, p.quantity, p.average_price, p.market_value)
            for p in self._client.get_positions(self._account)
        ]

    def get_account(self) -> Account:
        snap = self._client.get_account(self._account)
        return Account(cash=snap.cash, buying_power=snap.buying_power, equity=snap.equity)


__all__ = ["SchwabBroker"]
