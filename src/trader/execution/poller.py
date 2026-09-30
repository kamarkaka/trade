"""Bounded order-status polling (design §4.2: "poll Broker.get_order(id) until
FILLED/PARTIAL/REJECTED (bounded)").

A real broker accepts an order asynchronously: right after the 201 the order is usually
still WORKING, so reading its status once — correct only for the synchronous SimBroker /
FakeBroker — would record a 0-share fill for an order that fills a moment later.
``poll_until_terminal`` polls ``Broker.get_order`` until a TERMINAL status or a deadline.
On the deadline it cancels the unfilled remainder (the system never leaves resting orders
it is not tracking) and re-polls a bounded number of times so the final filled quantity is
known. A transient read failure is retried until the deadline; if no status could be read
at all, ``OrderStatusUnavailableError`` tells the caller the order's state is unknown.

Time is injected (``monotonic`` + ``sleep``) so tests are deterministic and never wait.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from trader.core import Fill
from trader.core.enums import OrderStatus
from trader.core.protocols import Broker
from trader.observability.logging import get_logger

# Statuses after which an order can no longer fill further.
TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
)

_log = get_logger("execution.poller")


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES


class OrderStatusUnavailableError(Exception):
    """No status read succeeded before the deadline — the order's state is unknown."""


@dataclass(frozen=True)
class PollPolicy:
    timeout_seconds: float
    interval_seconds: float = 2.0  # ~30 reads/min per order, well under the API budget
    cancel_on_timeout: bool = True
    post_cancel_polls: int = 5

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.post_cancel_polls < 0:
            raise ValueError("post_cancel_polls must be non-negative")


@dataclass(frozen=True)
class PollResult:
    fill: Fill  # the last observed status (cumulative filled quantity so far)
    timed_out: bool  # the deadline passed before a terminal status was seen
    cancel_requested: bool  # a cancel of the remainder was sent and accepted

    @property
    def terminal(self) -> bool:
        return is_terminal(self.fill.status)


def _read(broker: Broker, broker_order_id: str) -> Fill | None:
    try:
        return broker.get_order(broker_order_id)
    except Exception as exc:  # transient read failure: the caller retries within its budget
        _log.warning(
            "order status read failed", broker_order_id=broker_order_id, error=type(exc).__name__
        )
        return None


def _cancel(broker: Broker, broker_order_id: str) -> bool:
    try:
        broker.cancel_order(broker_order_id)
    except Exception as exc:
        _log.error(
            "cancel of timed-out order failed",
            broker_order_id=broker_order_id,
            error=type(exc).__name__,
        )
        return False
    return True


def poll_until_terminal(
    broker: Broker,
    broker_order_id: str,
    policy: PollPolicy,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PollResult:
    """Poll ``broker_order_id`` until a terminal status or ``policy.timeout_seconds``.

    The first read is immediate (a synchronous broker returns at once with no sleep). On
    timeout the remainder is cancelled (when ``policy.cancel_on_timeout``) and the order is
    re-read up to ``policy.post_cancel_polls`` times. Raises ``OrderStatusUnavailableError``
    if no read ever succeeded (after a best-effort cancel)."""
    deadline = monotonic() + policy.timeout_seconds
    last: Fill | None = None
    while True:
        fill = _read(broker, broker_order_id)
        if fill is not None:
            last = fill
            if is_terminal(fill.status):
                return PollResult(fill, timed_out=False, cancel_requested=False)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(policy.interval_seconds, remaining))

    cancel_requested = policy.cancel_on_timeout and _cancel(broker, broker_order_id)
    if policy.cancel_on_timeout:
        for _ in range(policy.post_cancel_polls):
            sleep(policy.interval_seconds)
            fill = _read(broker, broker_order_id)
            if fill is not None:
                last = fill
                if is_terminal(fill.status):
                    break
    if last is None:
        raise OrderStatusUnavailableError(
            f"no status read for order {broker_order_id} within {policy.timeout_seconds}s"
        )
    _log.warning(
        "order poll timed out",
        broker_order_id=broker_order_id,
        status=last.status.value,
        filled=last.quantity,
        cancel_requested=cancel_requested,
    )
    return PollResult(last, timed_out=True, cancel_requested=cancel_requested)


__all__ = [
    "TERMINAL_STATUSES",
    "OrderStatusUnavailableError",
    "PollPolicy",
    "PollResult",
    "is_terminal",
    "poll_until_terminal",
]
