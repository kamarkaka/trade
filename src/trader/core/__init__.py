"""Core domain vocabulary: immutable value types and enums shared across the
system (design §5). Importing from ``trader.core`` is the supported surface."""

from __future__ import annotations

from .enums import (
    Action,
    ConflictPolicy,
    Distribution,
    DriftDirection,
    Mode,
    OnOvershoot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from .types import (
    Account,
    Bar,
    Decision,
    Fill,
    MarketSnapshot,
    Order,
    Position,
    Quote,
    RiskVerdict,
    SlotSpec,
    StrategyBinding,
    TriggerSlot,
)

__all__ = [
    "Account",
    "Action",
    "Bar",
    "ConflictPolicy",
    "Decision",
    "Distribution",
    "DriftDirection",
    "Fill",
    "MarketSnapshot",
    "Mode",
    "OnOvershoot",
    "Order",
    "OrderStatus",
    "OrderType",
    "Position",
    "Quote",
    "RiskVerdict",
    "Side",
    "SlotSpec",
    "StrategyBinding",
    "TimeInForce",
    "TriggerSlot",
]
