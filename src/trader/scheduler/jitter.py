"""Seeded schedule jitter (design §7.2, Appendix C). Deterministic, isolated per-slot
drift keyed by (base_seed, date, strategy_id, slot_id) — the SAME code in live and
backtest.

CRITICAL invariants:
* **Never touch the global RNG.** Always build a dedicated ``numpy`` Generator from
  the derived seed, so strategies/runs can't interfere and a backtest is reproducible.
* **Stable hashing.** Use ``blake2b`` (Python's built-in ``hash()`` is salted per
  process — unusable for cross-process reproducibility, §9.5).
* The seed includes ``strategy_id`` AND ``slot_id`` so each strategy's drift is
  independent. With ``base_seed=None`` (live) fresh OS entropy is drawn so two days
  differ; ``compute_drift`` returns the concrete seed so the realized drift can be
  persisted per trigger for replay.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date

import numpy as np

from trader.core.enums import Distribution, DriftDirection
from trader.core.types import SlotSpec


def stable_seed(base_seed: int | None, slot_date: date, strategy_id: str, slot_id: str) -> int:
    """A 64-bit seed, deterministic for a fixed ``base_seed`` and fresh entropy when None."""
    if base_seed is None:
        return secrets.randbits(64)  # live: unpredictable, different each day
    joined = "|".join((str(base_seed), slot_date.isoformat(), strategy_id, slot_id))
    digest = hashlib.blake2b(joined.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _bounds(direction: DriftDirection, max_seconds: int) -> tuple[int, int]:
    if direction is DriftDirection.FORWARD:
        return 0, max_seconds
    if direction is DriftDirection.BACKWARD:
        return -max_seconds, 0
    return -max_seconds, max_seconds  # SYMMETRIC


def _sample(rng: np.random.Generator, distribution: Distribution, lo: int, hi: int) -> float:
    mid = (lo + hi) / 2
    if distribution is Distribution.TRIANGULAR:
        return float(rng.triangular(lo, mid, hi))
    if distribution is Distribution.TRUNCNORM:
        # No scipy dependency: normal centered at mid, ~4 sigma across the range, clipped.
        value = rng.normal(mid, (hi - lo) / 4 or 1.0)
        return float(min(hi, max(lo, value)))
    return float(rng.uniform(lo, hi))  # UNIFORM (default)


def compute_drift(
    slot: SlotSpec, base_seed: int | None, slot_date: date, strategy_id: str
) -> tuple[int, int]:
    """Return ``(drift_seconds, seed)`` for ``slot`` — bounded, direction-aware, seeded."""
    seed = stable_seed(base_seed, slot_date, strategy_id, slot.slot_id)
    max_seconds = slot.drift_max_minutes * 60
    lo, hi = _bounds(slot.drift_direction, max_seconds)
    if max_seconds == 0:
        return 0, seed
    rng = np.random.default_rng(seed)
    drift = round(_sample(rng, slot.distribution, lo, hi))  # round(float) -> int
    drift = max(lo, min(hi, drift))  # clamp rounding back into [lo, hi]
    return drift, seed
