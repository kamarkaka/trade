"""Strategy registry + built-in strategies. Importing this package registers the
built-ins (threshold, zscore_revert, template) into ``REGISTRY``."""

from .bindings import load_bindings
from .registry import REGISTRY, StrategyRegistry
from .strategies import template, threshold, zscore_revert  # noqa: F401 - register on import

__all__ = ["REGISTRY", "StrategyRegistry", "load_bindings"]
