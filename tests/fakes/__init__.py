"""Shared, deterministic test doubles implementing the core Protocols (M0.8).

Importable as ``from fakes import FakeClock, FakeBroker, FakeMarketDataProvider``
(``tests/`` is on the pytest pythonpath). Reused across M1-M7 tests so time,
market data, and the broker are injectable and tests never touch the network or
the wall clock.
"""

from __future__ import annotations

from .broker import FakeBroker
from .clock import FakeClock
from .market_data import FakeMarketDataProvider

__all__ = ["FakeBroker", "FakeClock", "FakeMarketDataProvider"]
