"""Clock implementations: wall-clock (live) and a controllable virtual clock
(backtest). Both satisfy the core ``Clock`` protocol so the same code runs in
production and in a deterministic backtest (design §5, Appendix B)."""

from .real import RealClock
from .virtual import VirtualClock

__all__ = ["RealClock", "VirtualClock"]
