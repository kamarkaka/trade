"""Strategy registry + built-in strategies. Importing this package registers the
built-ins (threshold, zscore_revert, template, canary) into ``REGISTRY``."""

from .bindings import load_bindings
from .registry import REGISTRY, StrategyRegistry
from .strategies import (  # noqa: F401 - register on import
    canary,
    template,
    threshold,
    zscore_revert,
)

__all__ = ["REGISTRY", "StrategyRegistry", "load_bindings"]
