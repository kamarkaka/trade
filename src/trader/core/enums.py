"""Core enumerations shared across the trading system.

Two families of values, chosen so they coerce directly from their external
representation:

* **Domain / Schwab-facing** enums use UPPER-CASE values that match the Schwab
  API order payloads (``BUY``, ``LIMIT``, ``FILLED`` …): ``Side``, ``Action``,
  ``OrderType``, ``TimeInForce``, ``OrderStatus``.
* **Config-facing** enums use lower-case values that match the YAML config in
  design §11 (``paper``, ``net``, ``forward`` …): ``Mode``, ``ConflictPolicy``,
  ``DriftDirection``, ``Distribution``, ``OnOvershoot``.

Each is a ``StrEnum``, so members compare equal to and stringify as their value,
and ``EnumClass(value)`` accepts either a raw string or an existing member.
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Order side (Schwab-facing)."""

    BUY = "BUY"
    SELL = "SELL"


class Action(StrEnum):
    """A strategy decision's intent."""

    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderType(StrEnum):
    """Order type (Schwab-facing)."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    """Order time-in-force (Schwab-facing)."""

    DAY = "DAY"
    GTC = "GTC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    """Normalized order/fill status (Schwab statuses map onto these)."""

    WORKING = "WORKING"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Mode(StrEnum):
    """Run mode (config-facing; default is the safe paper mode)."""

    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class ConflictPolicy(StrEnum):
    """Same-ticker cross-strategy conflict policy (config-facing)."""

    NET = "net"
    INDEPENDENT = "independent"
    PRIORITY = "priority"


class DriftDirection(StrEnum):
    """Scheduler jitter direction (config-facing)."""

    FORWARD = "forward"
    SYMMETRIC = "symmetric"
    BACKWARD = "backward"


class Distribution(StrEnum):
    """Scheduler jitter distribution (config-facing)."""

    UNIFORM = "uniform"
    TRUNCNORM = "truncnorm"
    TRIANGULAR = "triangular"


class OnOvershoot(StrEnum):
    """Policy when a drifted fire time overshoots the session edge (config-facing)."""

    CLAMP = "clamp"
    SKIP = "skip"
