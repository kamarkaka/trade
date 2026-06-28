"""Core Protocol interfaces — the injection seams that make live/backtest parity
*structural* (design §5, Appendix B).

Strategies and the orchestrator depend only on these abstractions; concrete
implementations are injected: a real vs. virtual ``Clock``, a Schwab vs.
historical ``MarketDataProvider``, a Schwab vs. simulated ``Broker``. Nothing
behind these protocols imports a broker SDK, opens a socket, or reads the wall
clock — that is what keeps the same strategy code correct live and in backtest.

Every ``MarketDataProvider`` method takes ``asof`` so no-lookahead is enforced at
the boundary, not by discipline (Appendix B).

All protocols are ``@runtime_checkable`` so ``isinstance`` confirms an object
exposes the methods; mypy additionally checks the full signatures structurally
when concrete implementations are declared against these types in later
milestones.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from .types import (
    Account,
    Bar,
    DayState,
    Decision,
    Fill,
    MarketSnapshot,
    Order,
    Position,
    Quote,
    RiskVerdict,
    TriggerSlot,
)


@runtime_checkable
class Clock(Protocol):
    """Time source. Wall-clock live; a controllable virtual clock in backtest."""

    def now(self) -> datetime: ...

    def is_market_open(self, at: datetime | None = None) -> bool: ...


@runtime_checkable
class MarketDataProvider(Protocol):
    """Point-in-time market data. Every read is bound to ``asof`` and must return
    only data available at or before it (no lookahead)."""

    def get_quote(self, symbol: str, asof: datetime) -> Quote: ...

    def get_bars(
        self, symbol: str, start: datetime, end: datetime, freq: str, asof: datetime
    ) -> Sequence[Bar]: ...


@runtime_checkable
class Broker(Protocol):
    """Order execution + account/position access. Schwab live; simulated in
    paper/backtest. The only seam through which orders ever leave the system."""

    def submit_order(self, order: Order) -> str: ...  # returns broker_order_id

    def get_order(self, broker_order_id: str) -> Fill: ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def get_positions(self) -> Sequence[Position]: ...

    def get_account(self) -> Account: ...


@runtime_checkable
class Strategy(Protocol):
    """The pluggable calculation. Pure: reads only the injected data/clock and the
    given snapshot/positions/account, and returns decisions (design §6)."""

    def decide(
        self,
        snapshot: MarketSnapshot,
        positions: Sequence[Position],
        account: Account,
        data: MarketDataProvider,
        clock: Clock,
    ) -> Sequence[Decision]: ...


@runtime_checkable
class RiskManager(Protocol):
    """The single, non-bypassable gate every order traverses before the broker
    (design §10). Returns an approve/clamp/reject verdict."""

    def check(
        self,
        order: Order,
        positions: Sequence[Position],
        account: Account,
        quote: Quote,
        day_state: DayState,
    ) -> RiskVerdict: ...


@runtime_checkable
class Scheduler(Protocol):
    """Produces the merged, time-sorted triggers for a trading date — used
    identically in live (APScheduler jobs) and backtest (walked in order)."""

    def triggers_for(self, on_date: date) -> Sequence[TriggerSlot]: ...


__all__ = [
    "Broker",
    "Clock",
    "MarketDataProvider",
    "RiskManager",
    "Scheduler",
    "Strategy",
]
