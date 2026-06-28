"""Conformance tests for the core Protocols.

These assert at runtime (via ``@runtime_checkable``) that a minimal implementation
of each protocol is recognized, and that an object missing a method is *not*.
Note: ``isinstance`` only checks method presence, not signatures — full signature
conformance is enforced by mypy when the real adapters (SchwabBroker, SimBroker,
HistoricalDataProvider, Real/VirtualClock, …) are typed against these protocols in
later milestones. (mypy does not type-check this tests/ tree.)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

from trader.core import (
    Account,
    Bar,
    DayState,
    Decision,
    Fill,
    MarketSnapshot,
    Order,
    OrderStatus,
    Position,
    Quote,
    RiskVerdict,
    TriggerSlot,
)
from trader.core.protocols import (
    Broker,
    Clock,
    MarketDataProvider,
    RiskManager,
    Scheduler,
    Strategy,
)

D = Decimal
T = datetime(2026, 6, 28, 14, 30, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return T

    def is_market_open(self, at: datetime | None = None) -> bool:
        return True


class FakeMarketData:
    def get_quote(self, symbol: str, asof: datetime) -> Quote:
        return Quote(symbol=symbol, ts=asof, last=D("1"), bid=D("1"), ask=D("1"), volume=0)

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, freq: str, asof: datetime
    ) -> Sequence[Bar]:
        return []


class FakeBroker:
    def submit_order(self, order: Order) -> str:
        return "b-1"

    def get_order(self, broker_order_id: str) -> Fill:
        return Fill(
            client_order_id="c-1",
            broker_order_id=broker_order_id,
            symbol="AAPL",
            quantity=1,
            price=D("1"),
            fees=D("0"),
            ts=T,
            status=OrderStatus.FILLED,
        )

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def get_positions(self) -> Sequence[Position]:
        return []

    def get_account(self) -> Account:
        return Account(cash=D("0"), buying_power=D("0"), equity=D("0"))


class FakeStrategy:
    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]:
        return []


class FakeRisk:
    def check(
        self,
        order: Order,
        positions: Sequence[Position],
        account: Account,
        quote: Quote,
        day_state: DayState,
    ) -> RiskVerdict:
        return RiskVerdict(approved=True)


class FakeScheduler:
    def triggers_for(self, on_date: date) -> Sequence[TriggerSlot]:
        return []


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeClock(), Clock)
    assert isinstance(FakeMarketData(), MarketDataProvider)
    assert isinstance(FakeBroker(), Broker)
    assert isinstance(FakeStrategy(), Strategy)
    assert isinstance(FakeRisk(), RiskManager)
    assert isinstance(FakeScheduler(), Scheduler)


def test_incomplete_impl_not_recognized() -> None:
    class NotAClock:
        def now(self) -> datetime:  # missing is_market_open
            return T

    class NotABroker:
        def submit_order(self, order: Order) -> str:  # missing the rest
            return "x"

    assert not isinstance(NotAClock(), Clock)
    assert not isinstance(NotABroker(), Broker)


def test_protocols_support_isinstance() -> None:
    # All protocols are runtime_checkable: isinstance must not raise, and a bare
    # object satisfies none of them.
    for proto in (Clock, MarketDataProvider, Broker, Strategy, RiskManager, Scheduler):
        assert not isinstance(object(), proto)
