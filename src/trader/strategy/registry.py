"""Strategy registry (design §6): a name -> class map so no strategy logic is
hardcoded in the engine. Strategies self-register via the ``@REGISTRY.register(name)``
decorator; the orchestrator/loader resolves a config ``name`` to an instance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from trader.core.protocols import Strategy

_S = TypeVar("_S", bound=Strategy)


class StrategyRegistry:
    """A name -> Strategy-class registry with a constructing ``create``."""

    def __init__(self) -> None:
        self._classes: dict[str, type[Strategy]] = {}

    def register(self, name: str) -> Callable[[type[_S]], type[_S]]:
        def decorator(cls: type[_S]) -> type[_S]:
            self._classes[name] = cls
            return cls

        return decorator

    def get(self, name: str) -> type[Strategy]:
        try:
            return self._classes[name]
        except KeyError as exc:
            raise KeyError(
                f"unknown strategy {name!r}; available: {sorted(self._classes)}"
            ) from exc

    def create(self, name: str, params: dict[str, object]) -> Strategy:
        """Instantiate the registered strategy ``name`` with ``**params``."""
        return self.get(name)(**params)

    def names(self) -> list[str]:
        return sorted(self._classes)


# Module-level default registry the built-in strategies register into on import.
REGISTRY = StrategyRegistry()
