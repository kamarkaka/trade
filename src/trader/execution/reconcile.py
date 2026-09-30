"""Position reconciliation: compare local attribution with broker truth (design §10).

The broker is the source of truth for positions; local attribution is the source of truth
for *intent*. ``reconcile`` compares the broker's position in each symbol with the sum the
strategies are attributed; the difference is *unattributed*. Unattributed holdings are
checked against an operator-acknowledged **baseline** — the ``'unknown'`` bucket — which
only the operator moves (``accept``, i.e. ``trader reconcile --accept-positions``):

- unattributed equal to the baseline (and non-zero) is **standing** — e.g. the owner's own
  long-term holdings in the same account — and does not make the report unclean;
- unattributed different from the baseline is a **discrepancy**: new, unexplained divergence
  (a fill nobody recorded, a manual trade, an order wrongly marked not placed).

``reconcile`` never writes, so a discrepancy stays one — on a retry, and on every later
start — until it is explained (the order settled) or acknowledged. On the first run against
an account with holdings, review them, then accept them once.

Scope: positions only. Open orders are settled before positions are compared
(``execution.account_reconcile``); account totals (cash, equity) are not compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trader.core import Position
from trader.core.protocols import Broker
from trader.state.attribution import UNKNOWN, AttributionLedger, BaselineChange


@dataclass(frozen=True)
class Discrepancy:
    """A symbol's broker quantity against its attribution and acknowledged baseline."""

    symbol: str
    broker_qty: int
    attributed_qty: int  # the strategies' (non-'unknown') attributed sum
    baseline_qty: int  # the acknowledged unattributed quantity (the 'unknown' bucket)

    @property
    def unattributed_qty(self) -> int:
        return self.broker_qty - self.attributed_qty


@dataclass(frozen=True)
class ReconcileReport:
    discrepancies: list[Discrepancy] = field(default_factory=list)  # differs from the baseline
    standing: list[Discrepancy] = field(default_factory=list)  # matches it (acknowledged)
    broker_positions: tuple[Position, ...] = ()  # the snapshot compared (what ``accept`` takes)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    @property
    def requires_attention(self) -> bool:
        return bool(self.discrepancies)


def reconcile(broker: Broker, attribution: AttributionLedger) -> ReconcileReport:
    """Compare broker positions with attribution and the acknowledged baseline. Read-only."""
    broker_positions = tuple(broker.get_positions())
    broker_qty = {p.symbol: p.quantity for p in broker_positions}
    baseline = {p.symbol: p.quantity for p in attribution.get_attributed(UNKNOWN)}
    unattributed = attribution.unattributed(broker_positions)
    changed: list[Discrepancy] = []
    standing: list[Discrepancy] = []
    for symbol in sorted(set(baseline) | set(unattributed)):
        held = broker_qty.get(symbol, 0)
        entry = Discrepancy(
            symbol=symbol,
            broker_qty=held,
            attributed_qty=held - unattributed.get(symbol, 0),
            baseline_qty=baseline.get(symbol, 0),
        )
        (standing if entry.unattributed_qty == entry.baseline_qty else changed).append(entry)
    return ReconcileReport(changed, standing, broker_positions)


def accept(report: ReconcileReport, attribution: AttributionLedger) -> list[BaselineChange]:
    """Acknowledge the unattributed holdings in ``report``'s broker snapshot as the new
    baseline (atomically). The caller makes sure nothing unresolved could explain them."""
    return attribution.accept_unattributed(report.broker_positions)
