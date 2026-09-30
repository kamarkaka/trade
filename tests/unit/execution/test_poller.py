"""Bounded order-status polling (LR2): terminal detection, timeout -> cancel -> re-read,
narrow retry of transient errors only, consistency checks on every read, hard read caps,
and deterministic time (no real sleeping)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fakes import FakeClock, FakeMarketDataProvider
from trader.broker import SimBroker
from trader.core import Account, Bar, Fill, Order, Position, Quote
from trader.core.enums import OrderStatus, OrderType, Side
from trader.execution.poller import (
    OrderStatusUnavailableError,
    PollPolicy,
    is_terminal,
    poll_until_terminal,
)

TS = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)


class _Time:
    """Virtual monotonic clock whose sleep advances time instantly (unless frozen)."""

    def __init__(self, *, frozen: bool = False) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self._frozen = frozen

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if not self._frozen:
            self.t += seconds


def _fill(status: OrderStatus, qty: int = 0, broker_order_id: str = "b1") -> Fill:
    price = Decimal("100") if qty else Decimal("0")
    return Fill("c1", broker_order_id, "AAPL", qty, price, Decimal("0"), TS, status)


class _ScriptedBroker:
    """Returns scripted get_order results (a Fill, or an Exception to raise); the last entry
    repeats. After an accepted cancel it returns ``after_cancel`` (when set). Records the
    read count at which each cancel happened."""

    def __init__(
        self, script: Sequence[Fill | Exception], *, cancel_error: Exception | None = None
    ):
        self._script = list(script)
        self.reads = 0
        self.cancel_at_read: list[int] = []
        self._cancel_error = cancel_error
        self.after_cancel: Sequence[Fill | Exception] | None = None
        self._post = 0

    def get_order(self, broker_order_id: str) -> Fill:
        self.reads += 1
        if self.cancel_at_read and self.after_cancel is not None:
            item = self.after_cancel[min(self._post, len(self.after_cancel) - 1)]
            self._post += 1
        else:
            item = self._script[min(self.reads - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def cancel_order(self, broker_order_id: str) -> None:
        self.cancel_at_read.append(self.reads)
        if self._cancel_error is not None:
            raise self._cancel_error

    def submit_order(self, order: Order) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    def get_positions(self) -> Sequence[Position]:  # pragma: no cover - unused
        return []

    def get_account(self) -> Account:  # pragma: no cover - unused
        raise NotImplementedError


def _poll(broker: object, clock: _Time, **policy: object):  # type: ignore[no-untyped-def]
    kwargs = {"timeout_seconds": 10.0, "interval_seconds": 2.0, **policy}
    return poll_until_terminal(
        broker,  # type: ignore[arg-type]
        "b1",
        PollPolicy(**kwargs),  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def test_terminal_statuses() -> None:
    for status in (
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    ):
        assert is_terminal(status)
    assert not is_terminal(OrderStatus.WORKING)
    assert not is_terminal(OrderStatus.PARTIAL_FILL)  # can still fill further


def test_immediate_fill_returns_without_sleeping() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.FILLED, 10)])
    result = _poll(broker, clock)
    assert result.fill.status is OrderStatus.FILLED and result.fill.quantity == 10
    assert result.terminal and not result.timed_out and not result.cancel_attempted
    assert clock.sleeps == [] and broker.reads == 1  # synchronous brokers pay nothing


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.EXPIRED])
def test_other_terminal_statuses_stop_polling(status: OrderStatus) -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING), _fill(status)])
    result = _poll(broker, clock)
    assert result.fill.status is status and result.terminal and not result.cancel_attempted
    assert broker.reads == 2


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
    assert broker.reads == 3 and clock.sleeps == [2.0, 2.0] and broker.cancel_at_read == []


def test_timeout_cancels_remainder_and_rereads_immediately() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.PARTIAL_FILL, 4)])
    broker.after_cancel = [_fill(OrderStatus.CANCELED, 4)]  # remainder cancelled; 4 filled
    result = _poll(broker, clock)
    assert result.timed_out and result.terminal  # resolved even though it timed out
    assert result.cancel_attempted and result.cancel_accepted
    assert result.fill.status is OrderStatus.CANCELED and result.fill.quantity == 4
    assert broker.cancel_at_read == [6]  # after the 6 main-phase reads (t = 0..10)
    assert broker.reads == 7 and len(clock.sleeps) == 5  # no sleep before the first re-read


def test_total_duration_is_bounded_including_the_post_cancel_phase() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)])  # never resolves
    result = _poll(broker, clock, timeout_seconds=5.0, post_cancel_polls=2)
    assert not result.terminal and result.fill.status is OrderStatus.WORKING
    # main phase clipped to the budget (2 + 2 + 1), then two re-read intervals after cancel
    assert clock.sleeps == [2.0, 2.0, 1.0, 2.0, 2.0]
    assert clock.t == pytest.approx(5.0 + 2 * 2.0)


def test_read_count_is_capped_even_if_the_clock_never_advances() -> None:
    clock = _Time(frozen=True)
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)])
    policy = PollPolicy(timeout_seconds=10.0, interval_seconds=2.0, post_cancel_polls=3)
    poll_until_terminal(broker, "b1", policy, monotonic=clock.monotonic, sleep=clock.sleep)  # type: ignore[arg-type]
    assert broker.reads == policy.max_reads + 1 + 3  # main cap + immediate re-read + 3 more


def test_cancel_that_loses_a_race_with_the_fill_still_resolves() -> None:
    clock = _Time()
    broker = _ScriptedBroker(
        [_fill(OrderStatus.WORKING)] * 6 + [_fill(OrderStatus.FILLED, 10)],
        cancel_error=RuntimeError("order already filled"),
    )
    result = _poll(broker, clock)
    assert result.terminal and result.fill.status is OrderStatus.FILLED
    assert result.cancel_attempted and not result.cancel_accepted
    assert broker.reads == 7  # the re-read after the failed cancel saw the fill


def test_cancel_failure_still_rereads_and_reports_non_terminal() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)], cancel_error=ConnectionError("x"))
    result = _poll(broker, clock, post_cancel_polls=2)
    assert result.timed_out and not result.terminal
    assert result.cancel_attempted and not result.cancel_accepted
    assert broker.reads == 6 + 3  # the post-cancel re-reads happened despite the failure


def test_no_cancel_when_disabled() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)])
    result = _poll(broker, clock, cancel_on_timeout=False)
    assert result.timed_out and not result.cancel_attempted and broker.cancel_at_read == []


def test_timeout_zero_reads_once_then_cancels() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.WORKING)])
    broker.after_cancel = [_fill(OrderStatus.CANCELED)]
    result = _poll(broker, clock, timeout_seconds=0.0)
    assert result.terminal and result.fill.status is OrderStatus.CANCELED
    assert broker.reads == 2 and clock.sleeps == []  # no waiting at all


def test_transient_read_errors_are_retried_until_success() -> None:
    clock = _Time()
    broker = _ScriptedBroker(
        [TimeoutError("t1"), ConnectionError("t2"), _fill(OrderStatus.FILLED, 10)]
    )
    result = _poll(broker, clock)
    assert result.fill.status is OrderStatus.FILLED and not result.timed_out
    assert broker.reads == 3


def test_custom_retryable_types() -> None:
    class _Flaky(Exception):
        pass

    clock = _Time()
    broker = _ScriptedBroker([_Flaky("429"), _fill(OrderStatus.FILLED, 10)])
    result = poll_until_terminal(
        broker,  # type: ignore[arg-type]
        "b1",
        PollPolicy(timeout_seconds=10.0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        retryable=(_Flaky,),
    )
    assert result.fill.status is OrderStatus.FILLED


def test_non_transient_error_cancels_and_raises_with_the_cause() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.PARTIAL_FILL, 4), PermissionError("safe mode")])
    with pytest.raises(OrderStatusUnavailableError, match="PermissionError") as excinfo:
        _poll(broker, clock)
    err = excinfo.value
    assert isinstance(err.__cause__, PermissionError)  # the fatal cause is preserved
    assert err.cancel_attempted and err.cancel_accepted
    assert err.last_fill is not None and err.last_fill.quantity == 4
    assert broker.reads == 2 and clock.sleeps == [2.0]  # no pointless retrying


def test_never_readable_raises_after_best_effort_cancel() -> None:
    clock = _Time()
    broker = _ScriptedBroker([TimeoutError("down")])
    with pytest.raises(OrderStatusUnavailableError, match="no status read") as excinfo:
        _poll(broker, clock, post_cancel_polls=1)
    assert excinfo.value.cancel_attempted and excinfo.value.last_fill is None
    assert broker.cancel_at_read == [6]  # the remainder is still cancelled (safe direction)


def test_never_readable_without_cancel() -> None:
    clock = _Time()
    broker = _ScriptedBroker([TimeoutError("down")])
    with pytest.raises(OrderStatusUnavailableError) as excinfo:
        _poll(broker, clock, cancel_on_timeout=False)
    assert not excinfo.value.cancel_attempted and broker.cancel_at_read == []


def test_failed_post_cancel_read_keeps_the_last_good_fill() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.PARTIAL_FILL, 4)])
    broker.after_cancel = [TimeoutError("blip")]
    result = _poll(broker, clock, post_cancel_polls=1)
    assert result.fill.quantity == 4 and result.fill.status is OrderStatus.PARTIAL_FILL
    assert not result.terminal


def test_read_for_a_different_order_is_never_accepted() -> None:
    clock = _Time()
    broker = _ScriptedBroker(
        [_fill(OrderStatus.WORKING), _fill(OrderStatus.FILLED, 10, broker_order_id="b-other")]
    )
    result = _poll(broker, clock, post_cancel_polls=1)
    assert result.fill.broker_order_id == "b1" and not result.terminal


def test_filled_quantity_going_backwards_is_never_accepted() -> None:
    clock = _Time()
    broker = _ScriptedBroker([_fill(OrderStatus.PARTIAL_FILL, 4)])
    broker.after_cancel = [_fill(OrderStatus.CANCELED, 0)]  # inconsistent: 4 shares vanish
    result = _poll(broker, clock, post_cancel_polls=1)
    assert result.fill.quantity == 4 and not result.terminal  # escalate, don't drop shares


def test_policy_validation() -> None:
    assert PollPolicy(timeout_seconds=0).max_reads == 1  # read once
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(ValueError, match="timeout"):
            PollPolicy(timeout_seconds=bad)
    for bad in (0.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="interval"):
            PollPolicy(timeout_seconds=1, interval_seconds=bad)
    with pytest.raises(ValueError, match="post_cancel"):
        PollPolicy(timeout_seconds=1, post_cancel_polls=-1)


# --- against the real SimBroker ------------------------------------------------ #


def _sim() -> SimBroker:
    price = Decimal("100")
    quote = Quote("AAPL", TS, price, price, price, 1000)
    bar = Bar("AAPL", TS, price, Decimal("101"), Decimal("99"), price, 1000)
    data = FakeMarketDataProvider(quotes={"AAPL": [quote]}, bars={"AAPL": [bar]})
    return SimBroker(data, FakeClock(TS), starting_cash=Decimal("100000"))


def test_simbroker_market_order_resolves_on_the_first_read() -> None:
    broker = _sim()
    broker_order_id = broker.submit_order(Order("m1", "s1", "AAPL", Side.BUY, 5, OrderType.MARKET))
    clock = _Time()
    result = poll_until_terminal(
        broker, broker_order_id, PollPolicy(timeout_seconds=0), monotonic=clock.monotonic
    )
    assert result.terminal and result.fill.quantity == 5 and not result.cancel_attempted


def test_simbroker_resting_limit_order_is_cancelled_not_left_working() -> None:
    broker = _sim()
    order = Order("l1", "s1", "AAPL", Side.BUY, 5, OrderType.LIMIT, Decimal("90"))  # won't cross
    broker_order_id = broker.submit_order(order)
    clock = _Time()
    result = poll_until_terminal(
        broker,
        broker_order_id,
        PollPolicy(timeout_seconds=0),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result.terminal and result.fill.status is OrderStatus.CANCELED
    assert result.fill.quantity == 0 and result.cancel_accepted and clock.sleeps == []
