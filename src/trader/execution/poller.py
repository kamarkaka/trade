"""Bounded order-status polling (design §4.2: "poll Broker.get_order(id) until
FILLED/PARTIAL/REJECTED (bounded)" — note PARTIAL_FILL is NOT terminal here: a partially
filled order can still fill further, so it is polled on like WORKING).

A real broker accepts an order asynchronously: right after the 201 the order is usually
still WORKING, so reading its status once — correct only for the synchronous SimBroker /
FakeBroker — would record a 0-share fill for an order that fills a moment later.
``poll_until_terminal`` polls ``Broker.get_order`` until a TERMINAL status or a deadline.
On the deadline it cancels the unfilled remainder (the system never leaves resting orders
it is not tracking) and re-reads for a bounded time so the final filled quantity is known.
The caller must treat ``PollResult.terminal`` — not ``timed_out`` — as "resolved": a fill
seen after the cancel yields ``timed_out=True, terminal=True``.

Error handling is deliberately narrow. Only the injected ``retryable`` exception types
(transient transport failures) are retried until the deadline. Anything else — safe mode,
auth, a malformed response, an unknown id, a programming error — will not clear by waiting,
so the poller makes a best-effort cancel and raises ``OrderStatusUnavailableError`` chained
from the cause. Reads that are inconsistent (another order's id, or a filled quantity
below one already seen) are never accepted; if nothing consistent and terminal is seen,
the result stays non-terminal so the caller escalates instead of mis-attributing shares.

Every loop is bounded by a read count as well as by the clock, and time is injected
(``monotonic`` + ``sleep``) so tests are deterministic and never wait. Worst-case duration
is ``timeout_seconds + post_cancel_polls * interval_seconds`` plus however long individual
broker calls block (the Schwab transport retries a GET internally; see schwab/retry.py).
"""

from __future__ import annotations

import math
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

# Generic transient failures. Broker-specific transients (e.g. the Schwab client's 429/5xx
# errors and httpx transport errors) are supplied by the wiring via ``retryable``.
DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)

_log = get_logger("execution.poller")


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_STATUSES


class OrderStatusUnavailableError(Exception):
    """The order's status could not be established (no successful read before the
    deadline, or a non-transient read error). Its state is unknown to the caller."""

    def __init__(
        self,
        broker_order_id: str,
        detail: str,
        *,
        last_fill: Fill | None,
        cancel_attempted: bool,
        cancel_accepted: bool,
    ) -> None:
        super().__init__(f"order {broker_order_id}: status unavailable ({detail})")
        self.broker_order_id = broker_order_id
        self.last_fill = last_fill  # the last consistent read, if any
        self.cancel_attempted = cancel_attempted
        self.cancel_accepted = cancel_accepted


@dataclass(frozen=True)
class PollPolicy:
    timeout_seconds: float  # 0 => read once, then cancel any remainder immediately
    interval_seconds: float = 2.0  # ~30 reads/min per order, well under the API budget
    cancel_on_timeout: bool = True
    post_cancel_polls: int = 5

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds < 0:
            raise ValueError("timeout_seconds must be finite and non-negative")
        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        if self.post_cancel_polls < 0:
            raise ValueError("post_cancel_polls must be non-negative")

    @property
    def max_reads(self) -> int:
        """Hard cap on reads in the main phase, independent of the clock."""
        return math.ceil(self.timeout_seconds / self.interval_seconds) + 1


@dataclass(frozen=True)
class PollResult:
    fill: Fill  # the last consistent status (cumulative filled quantity so far)
    timed_out: bool  # the deadline passed before a terminal status was seen
    cancel_attempted: bool  # a cancel of the remainder was sent
    cancel_accepted: bool  # ...and the broker accepted it (did not raise)

    @property
    def terminal(self) -> bool:
        """True iff the order is resolved: nothing more can fill."""
        return is_terminal(self.fill.status)


class _Poll:
    """One polling session's state: the last consistent read and the cancel outcome."""

    def __init__(
        self,
        broker: Broker,
        broker_order_id: str,
        retryable: tuple[type[BaseException], ...],
    ) -> None:
        self.broker = broker
        self.broker_order_id = broker_order_id
        self.retryable = retryable
        self.last: Fill | None = None
        self.cancel_attempted = False
        self.cancel_accepted = False

    def read(self) -> Fill | None:
        """One read: the fill if it is consistent, None on a transient failure or an
        inconsistent read. A non-transient error cancels (best effort) and raises."""
        try:
            fill = self.broker.get_order(self.broker_order_id)
        except self.retryable as exc:
            _log.warning(
                "transient order status read failure; will retry",
                broker_order_id=self.broker_order_id,
                error=type(exc).__name__,
            )
            return None
        except Exception as exc:
            self.cancel()
            raise self.unavailable(f"non-transient read error {type(exc).__name__}") from exc
        if fill.broker_order_id != self.broker_order_id:
            _log.error(
                "status read returned a different order; ignored",
                broker_order_id=self.broker_order_id,
                got=fill.broker_order_id,
            )
            return None
        if self.last is not None and fill.quantity < self.last.quantity:
            _log.error(
                "filled quantity went backwards; read ignored",
                broker_order_id=self.broker_order_id,
                seen=self.last.quantity,
                got=fill.quantity,
            )
            return None
        self.last = fill
        return fill

    def cancel(self) -> None:
        if self.cancel_attempted:
            return
        self.cancel_attempted = True
        try:
            self.broker.cancel_order(self.broker_order_id)
        except Exception as exc:
            # Often benign (the order filled or expired first); the next read decides.
            _log.warning(
                "cancel of timed-out order failed",
                broker_order_id=self.broker_order_id,
                error=type(exc).__name__,
            )
            return
        self.cancel_accepted = True

    def unavailable(self, detail: str) -> OrderStatusUnavailableError:
        return OrderStatusUnavailableError(
            self.broker_order_id,
            detail,
            last_fill=self.last,
            cancel_attempted=self.cancel_attempted,
            cancel_accepted=self.cancel_accepted,
        )

    def result(self, *, timed_out: bool) -> PollResult:
        if self.last is None:  # pragma: no cover - callers check a read succeeded first
            raise RuntimeError("no consistent read to report")
        return PollResult(self.last, timed_out, self.cancel_attempted, self.cancel_accepted)


def poll_until_terminal(
    broker: Broker,
    broker_order_id: str,
    policy: PollPolicy,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    retryable: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
) -> PollResult:
    """Poll ``broker_order_id`` until a terminal status or ``policy.timeout_seconds``.

    The first read is immediate (a synchronous broker returns at once with no sleep). On
    timeout the remainder is cancelled (when ``policy.cancel_on_timeout``), then the order
    is re-read immediately and up to ``policy.post_cancel_polls`` more times within a
    bounded window. Raises ``OrderStatusUnavailableError`` on a non-transient read error or
    if no consistent read ever succeeded (after a best-effort cancel)."""
    poll = _Poll(broker, broker_order_id, retryable)

    deadline = monotonic() + policy.timeout_seconds
    for _ in range(policy.max_reads):
        fill = poll.read()
        if fill is not None and is_terminal(fill.status):
            return poll.result(timed_out=False)
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(policy.interval_seconds, remaining))

    if policy.cancel_on_timeout:
        poll.cancel()
        post_deadline = monotonic() + policy.post_cancel_polls * policy.interval_seconds
        for attempt in range(policy.post_cancel_polls + 1):  # first re-read is immediate
            if attempt:
                remaining = post_deadline - monotonic()
                if remaining <= 0:
                    break
                sleep(min(policy.interval_seconds, remaining))
            fill = poll.read()
            if fill is not None and is_terminal(fill.status):
                break

    if poll.last is None:
        raise poll.unavailable(f"no status read within {policy.timeout_seconds}s")
    result = poll.result(timed_out=True)
    _log.warning(
        "order poll timed out",
        broker_order_id=broker_order_id,
        status=result.fill.status.value,
        filled=result.fill.quantity,
        terminal=result.terminal,
        cancel_accepted=result.cancel_accepted,
    )
    return result


__all__ = [
    "DEFAULT_RETRYABLE",
    "TERMINAL_STATUSES",
    "OrderStatusUnavailableError",
    "PollPolicy",
    "PollResult",
    "is_terminal",
    "poll_until_terminal",
]
