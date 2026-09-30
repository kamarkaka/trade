"""Reconciliation: true local state to broker truth (design §10).

The broker is the source of truth for positions; local attribution is the source of
truth for *intent*. ``reconcile`` diffs the broker's positions against the per-strategy
attributed sums and parks any unattributed delta under the ``'unknown'`` strategy (so the
books tie out). Only a CHANGE in that parked bucket is a discrepancy (new unexplained
divergence — e.g. a fill nobody recorded); holdings already parked and unchanged since the
last run (e.g. the owner's own long-term positions in the same account) are reported as
``standing`` and do not make the report unclean. The first run on an account with such
holdings therefore reports them once; re-running confirms them.

Scope (M4.1): position/attribution reconciliation only. Open-order vs broker-fill
reconciliation (idempotent re-submit recovery) is M5.3; account-total (cash/equity)
diff is layered on alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trader.core.protocols import Broker
from trader.state.attribution import UNKNOWN, AttributionLedger


@dataclass(frozen=True)
class Discrepancy:
    """A symbol where the broker's quantity didn't match the attributed total."""

    symbol: str
    broker_qty: int
    attributed_qty: int  # real (non-'unknown') attribution before reconciling
    parked_qty: int  # delta moved into the 'unknown' bucket (broker - attributed)


@dataclass(frozen=True)
class ReconcileReport:
    discrepancies: list[Discrepancy] = field(default_factory=list)  # changed since last run
    standing: list[Discrepancy] = field(default_factory=list)  # parked earlier, unchanged

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    @property
    def requires_attention(self) -> bool:
        # Any divergence needs a human/kill-switch look (escalation wired in M5).
        return bool(self.discrepancies)


def reconcile(broker: Broker, attribution: AttributionLedger) -> ReconcileReport:
    """True attribution to broker positions; park deltas in 'unknown'; report what changed."""
    broker_positions = list(broker.get_positions())
    broker_qty = {p.symbol: p.quantity for p in broker_positions}
    before = {p.symbol: p.quantity for p in attribution.get_attributed(UNKNOWN)}
    # reconcile_total mutates 'unknown' to the residual and returns the non-zero residuals.
    parked = {ap.symbol: ap.quantity for ap in attribution.reconcile_total(broker_positions)}
    changed: list[Discrepancy] = []
    standing: list[Discrepancy] = []
    for symbol in sorted(set(before) | set(parked)):
        now_parked = parked.get(symbol, 0)
        entry = Discrepancy(
            symbol=symbol,
            broker_qty=broker_qty.get(symbol, 0),
            attributed_qty=broker_qty.get(symbol, 0) - now_parked,  # real = broker - delta
            parked_qty=now_parked,
        )
        (standing if before.get(symbol, 0) == now_parked else changed).append(entry)
    return ReconcileReport(discrepancies=changed, standing=standing)
