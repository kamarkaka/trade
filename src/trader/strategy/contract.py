"""The Strategy contract (design §4.1/§5/§6) as reusable assertions.

Every registered strategy must be **pure** (no wall clock, no input mutation, deterministic),
**asof-bound** (reads only the injected snapshot/data/clock), and emit **well-formed,
universe-scoped** decisions. These helpers are framework-free so both the conformance suite
(M6.1) and the strategy-development template (M6.3) can reuse them.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from trader.core import Decision, MarketSnapshot, Quote
from trader.core.enums import Action

# Wall-clock tokens forbidden in strategy source (boundary rule 1: strategies read only the
# injected clock). A line carrying the allow-marker comment is exempt (rare, explicit).
_WALLCLOCK_TOKENS = (
    "datetime.now(",
    "datetime.utcnow(",
    "time.time(",
    "time.monotonic(",
    "date.today(",
)
_ALLOW_MARKER = "allow-wallclock"


def assert_decisions_well_formed(decisions: Sequence[Decision], universe: Sequence[str]) -> None:
    """Every decision is a valid, universe-scoped Decision (design §6)."""
    symbols = set(universe)
    for d in decisions:
        assert d.action in (Action.BUY, Action.SELL, Action.HOLD), f"bad action {d.action!r}"
        assert d.symbol in symbols, (
            f"decision symbol {d.symbol!r} not in universe {sorted(symbols)}"
        )
        assert isinstance(d.quantity, int) and d.quantity >= 0, f"bad quantity {d.quantity!r}"
        if d.action is Action.HOLD:
            assert d.quantity == 0, "HOLD must have quantity 0"
        assert d.limit_price is None or (
            isinstance(d.limit_price, Decimal) and d.limit_price > 0
        ), f"bad limit_price {d.limit_price!r}"
        assert isinstance(d.rationale, str), "rationale must be a str"


def assert_no_wallclock(strategy_cls: type) -> None:
    """Static guard: the strategy's module must not read the wall clock (boundary rule 1)."""
    module = inspect.getmodule(strategy_cls)
    assert module is not None, f"cannot resolve module for {strategy_cls!r}"
    for raw in inspect.getsource(module).splitlines():
        if _ALLOW_MARKER in raw:
            continue
        for token in _WALLCLOCK_TOKENS:
            assert token not in raw, (
                f"{strategy_cls.__name__} module reads the wall clock ({token}); strategies must "
                f"use the injected clock/snapshot.asof"
            )


def make_snapshot(asof: datetime, quotes: Mapping[str, Quote]) -> MarketSnapshot:
    """Build a MarketSnapshot from canned quotes (test/template fixture-builder)."""
    return MarketSnapshot(asof=asof, quotes=dict(quotes))


__all__ = ["assert_decisions_well_formed", "assert_no_wallclock", "make_snapshot"]
