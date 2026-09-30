"""Bounded order-status polling (LR2): terminal detection, timeout -> cancel -> re-poll,
transient read errors, and deterministic time (no real sleeping)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trader.core import Account, Fill, Order, Position
from trader.core.enums import OrderStatus
from trader.execution.poller import (
    OrderStatusUnavailableError,
    PollPolicy,
    is_terminal,
    poll_until_terminal,
)

TS = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Time:
    """Virtual monotonic clock whose sleep advances time instantly."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _fill(status: OrderStatus, qty: int = 0) -> Fill:
    price = Decimal("100") if qty else Decimal("0")
    return Fill("c1", "b1", "AAPL", qty, price, Decimal("0"), TS, status)


class _ScriptedBroker:
    """Returns scripted get_order results (a Fill or an Exception to raise); the last
    entry repeats. Records cancels; cancel may be told to fail."""

    def __init__(self, script: Sequence[Fill | Exception], *, cancel_fails: bool = False) -> None:
        self._script = list(script)
        self.reads = 0
        self.cancels: list[str] = []
        self._cancel_fails = cancel_fails
        self.after_cancel: Fill | None = None  # status reported once a cancel was accepted

    def get_order(self, broker_order_id: str) -> Fill:
        self.reads += 1
        if self.cancels and self.after_cancel is not None:
            return self.after_cancel
        item = self._script[min(self.reads - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def cancel_order(self, broker_order_id: str) -> None:
        if self._cancel_fails:
            raise ConnectionError("cancel failed")
        self.cancels.append(broker_order_id)

    def submit_order(self, order: Order) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    def get_positions(self) -> Sequence[Position]:  # pragma: no cover - unused
        return []

    def get_account(self) -> Account:  # pragma: no cover - unused
        raise NotImplementedError


def _poll(broker: _ScriptedBroker, clock: _Time, **policy: object):  # type: ignore[no-untyped-def]
    kwargs = {"timeout_seconds": 10.0, "interval_seconds": 2.0, **policy}
    return poll_until_terminal(
        broker,  # type: ignore[arg-type]
        "b1",
        PollPolicy(**kwargs),  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def test_terminal_statuses() -> None:
    assert all(
        is_terminal(s)
        for s in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )
    )
    assert not is_terminal(OrderStatus.WORKING)
    assert not is_terminal(OrderStatus.PARTIAL_FILL)


def test_immediate_fill_returns_without_sleeping() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.FILLED, 10)])
    result = _poll(broker, clock)
    assert result.fill.status is OrderStatus.FILLED and result.fill.quantity == 10
    assert not result.timed_out and result.terminal and not result.cancel_requested
    assert clock.sleeps == [] and broker.cancels == []  # synchronous brokers pay nothing


def test_fills_after_a_few_polls() -> None:
    clock = _Time()
    broker = _ScriptedBroker(
        [
            _fill(OrderStatus.WORKING),
            _fill(OrderStatus.PARTIAL_FILL, 4),
            _fill(OrderStatus.FILLED, 10),
        ]
    )
    result = _poll(broker, clock)
    assert result.fill.quantity == 10 and not result.timed_out
    assert broker.reads == 3 and clock.sleeps == [2.0, 2.0] and broker.cancels == []


def test_timeout_cancels_remainder_and_reports_final_partial_fill() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.PARTIAL_FILL, 4)])
    broker.after_cancel = _fill(OrderStatus.CANCELED, 4)  # remainder cancelled, 4 filled
    result = _poll(broker, clock)
    assert result.timed_out and result.cancel_requested and result.terminal
    assert result.fill.status is OrderStatus.CANCELED and result.fill.quantity == 4
    assert broker.cancels == ["b1"]


def test_deadline_is_respected() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)])
    _poll(broker, clock, timeout_seconds=5.0, interval_seconds=2.0, post_cancel_polls=0)
    # sleeps are clipped to the remaining budget: 2 + 2 + 1 == the 5s timeout, never beyond
    assert clock.sleeps == [2.0, 2.0, 1.0] and clock.t == pytest.approx(5.0)


def test_cancel_failure_still_repolls_and_reports_non_terminal() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)], cancel_fails=True)
    result = _poll(broker, clock, post_cancel_polls=2)
    assert result.timed_out and not result.cancel_requested
    assert not result.terminal and result.fill.status is OrderStatus.WORKING


def test_no_cancel_when_disabled() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)])
    result = _poll(broker, clock, cancel_on_timeout=False)
    assert result.timed_out and not result.cancel_requested and broker.cancels == []


def test_transient_read_errors_are_retried_until_success() -> None:
    clock = _Time()
    broker = _ScriptedBroker(
        [TimeoutError("t1"), ConnectionError("t2"), _fill(OrderStatus.FILLED, 10)]
    )
    result = _poll(broker, clock)
    assert result.fill.status is OrderStatus.FILLED and not result.timed_out
    assert broker.reads == 3


def test_never_readable_raises_after_best_effort_cancel() -> None:
    clock = _Time()
    broker = _ScriptedBroker([TimeoutError("down")])
    with pytest.raises(OrderStatusUnavailableError, match="b1"):
        _poll(broker, clock, post_cancel_polls=1)
    assert broker.cancels == ["b1"]  # the remainder is still cancelled (safe direction)


def test_policy_validation() -> None:
    with pytest.raises(ValueError, match="timeout"):
        PollPolicy(timeout_seconds=0)
    with pytest.raises(ValueError, match="interval"):
        PollPolicy(timeout_seconds=1, interval_seconds=0)
    with pytest.raises(ValueError, match="post_cancel"):
        PollPolicy(timeout_seconds=1, post_cancel_polls=-1)
