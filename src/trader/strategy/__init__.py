"""Strategy registry + built-in strategies. Importing this package registers the
built-ins (threshold, zscore_revert) into ``REGISTRY``."""

from .registry import REGISTRY, StrategyRegistry
from .strategies import threshold, zscore_revert  # noqa: F401 - register built-ins on import

__all__ = ["REGISTRY", "StrategyRegistry"]
