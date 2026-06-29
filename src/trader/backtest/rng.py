"""Seeded RNG for reproducible backtests (design §9.5).

Always returns a *local* numpy ``Generator`` seeded explicitly — never touches the
global RNG — so a run is exactly reproducible from its manifest's seed and two runs
can't interfere with each other.
"""

from __future__ import annotations

import numpy as np


def make_rng(seed: int) -> np.random.Generator:
    """A fresh, independent PCG64 generator seeded with ``seed``."""
    return np.random.Generator(np.random.PCG64(seed))
