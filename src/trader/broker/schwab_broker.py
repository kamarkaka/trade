"""SchwabBroker — the live counterpart of SimBroker (design §5).

Adapts the first-party Schwab trading client to the core ``Broker`` protocol, so the
orchestrator places real orders through the SAME abstraction as the simulator. It is
**safe-mode aware**: in READ-ONLY safe mode (dead refresh token) ``submit_order`` refuses
and raises a typed error rather than silently dropping the order.

Idempotency (write-ahead client_order_id + reconcile-before-resend) is layered ABOVE this
broker in M5.3 — and the transport already refuses to auto-retry the order POST (M5.1), so a
duplicate order is never created at this layer. Schwab does not echo our ``client_order_id``,
so we map each returned broker order id back to the originating id + symbol to build a
complete ``Fill``.

SAFETY: this is the real-money order path. It is only constructed by the go-live wiring
(M5.6) after the double-confirm; the paper daemon still uses SimBroker and refuses mode=live.
"""

from __future__ import annotations

from decimal import Decimal

from trader.core import Account, Fill, Order, Position
from trader.core.protocols import Clock
from trader.observability.logging import get_logger
from trader.schwab.errors import SchwabReadOnlyModeError
from trader.schwab.orders import SchwabTradingClient, build_order_json


class SchwabBroker:
    """Live broker over the Schwab trading client (implements the core ``Broker`` protocol)."""

    def __init__(self, client: SchwabTradingClient, account_hash: str, *, clock: Clock) -> None:
        self._client = client
        self._account = account_hash
        self._clock = clock
        # broker_order_id -> (client_order_id, symbol). Schwab doesn't echo our client id, so
        # we remember it (+ the symbol we sent) to assemble a complete Fill. In-memory only;
        # crash-safe recovery is M5.3's job.
        self._submitted: dict[str, tuple[str, str]] = {}
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
        self._submitted[broker_order_id] = (order.client_order_id, order.symbol)
        self._log.info(
            "order submitted",
            cid=order.client_order_id,
            broker_order_id=broker_order_id,
            symbol=order.symbol,
        )
        return broker_order_id

    def get_order(self, broker_order_id: str) -> Fill:
        status = self._client.get_order(self._account, broker_order_id)
        client_order_id, symbol = self._submitted.get(broker_order_id, ("", status.symbol))
        return Fill(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=symbol or status.symbol,
            quantity=status.filled_quantity,  # 0 while still WORKING (valid; no fill yet)
            price=status.average_price,  # 0 until something fills
            fees=Decimal(0),  # real fees come from the transaction record (reconciled later)
            ts=self._clock.now(),
            status=status.status,
        )

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
