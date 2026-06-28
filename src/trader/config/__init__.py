"""Configuration: the validated pydantic schema (`models`) and the layered
loader (`loader`) that assembles defaults < file < env < CLI into an
:class:`AppConfig` (design §11)."""

from __future__ import annotations

from .loader import DEFAULT_CONFIG_PATH, load_config, resolved_sources
from .models import (
    AccountConfig,
    AlertingConfig,
    AppConfig,
    BacktestConfig,
    ExecutionConfig,
    FeesModelConfig,
    ObservabilityConfig,
    RiskConfig,
    ScheduleConfig,
    SlippageModelConfig,
    SlotConfig,
    StrategyBindingConfig,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "AccountConfig",
    "AlertingConfig",
    "AppConfig",
    "BacktestConfig",
    "ExecutionConfig",
    "FeesModelConfig",
    "ObservabilityConfig",
    "RiskConfig",
    "ScheduleConfig",
    "SlippageModelConfig",
    "SlotConfig",
    "StrategyBindingConfig",
    "load_config",
    "resolved_sources",
]
