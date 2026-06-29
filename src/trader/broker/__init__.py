"""Broker implementations: the simulated broker (backtest/paper) and the live Schwab
broker. Both satisfy the core ``Broker`` protocol; the orchestrator is agnostic to which.

SAFETY: ``SchwabBroker`` is the real-money order path. It is only constructed by the
go-live wiring (M5.6) after the double-confirm; paper/backtest use ``SimBroker``."""

from .schwab_broker import SchwabBroker
from .sim import FeesModel, SimBroker, SlippageModel

__all__ = ["FeesModel", "SchwabBroker", "SimBroker", "SlippageModel"]
