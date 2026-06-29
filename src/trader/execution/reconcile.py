"""Reconciliation: true local state to broker truth (design §10).

The broker is the source of truth for positions; local attribution is the source of
truth for *intent*. ``reconcile`` diffs the broker's positions against the per-strategy
attributed sums, parks any unattributed delta under the ``'unknown'`` strategy (so the
books tie out), and returns a discrepancy report. Any non-clean result flags
``requires_attention`` — the hook the kill switch (M5) escalates on unexplained
divergence. Runs on startup (before acting), after submits, and at EOD.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trader.core.protocols import Broker
from trader.state.attribution import AttributionLedger


@dataclass(frozen=True)
class Discrepancy:
    """A symbol where the broker's quantity didn't match the attributed total."""

    symbol: str
    broker_qty: int
    attributed_qty: int  # real (non-'unknown') attribution before reconciling
    parked_qty: int  # delta moved into the 'unknown' bucket (broker - attributed)


@dataclass(frozen=True)
class ReconcileReport:
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    @property
    def requires_attention(self) -> bool:
        # Any divergence needs a human/kill-switch look (escalation wired in M5).
        return bool(self.discrepancies)


def reconcile(broker: Broker, attribution: AttributionLedger) -> ReconcileReport:
    """True attribution to broker positions; park deltas in 'unknown'; report divergence."""
    broker_positions = list(broker.get_positions())
    broker_qty = {p.symbol: p.quantity for p in broker_positions}
    parked = attribution.reconcile_total(broker_positions)  # mutates 'unknown' to the residual
    discrepancies = [
        Discrepancy(
            symbol=ap.symbol,
            broker_qty=broker_qty.get(ap.symbol, 0),
            attributed_qty=broker_qty.get(ap.symbol, 0) - ap.quantity,  # real = broker - delta
            parked_qty=ap.quantity,
        )
        for ap in parked
    ]
    return ReconcileReport(discrepancies=discrepancies)
