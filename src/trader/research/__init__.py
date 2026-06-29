"""OFFLINE-ONLY research package (design Appendix A, M6.9).

Strictly for offline parameter research over CACHED data. It is **structurally
incapable of trading**: nothing in this package may import ``trader.broker``,
``trader.schwab``, ``trader.auth``, ``trader.execution``, or ``trader.orchestrator``
(enforced by ``tests/unit/test_param_sweep.py`` via a subprocess ``sys.modules`` scan).
It reads only the on-disk Parquet cache — never the network, never a broker.

The vectorized sweep here is an APPROXIMATION used to *rank* parameter combinations
quickly; it is NOT the parity path. Any promising parameters MUST be re-validated through
the real event-driven backtest (``trader backtest`` / M6.7) before any live use.
"""
