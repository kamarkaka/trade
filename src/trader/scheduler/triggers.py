"""Merged, time-sorted trigger generation (design §4.3, Appendix C #3).

``SlotScheduler`` implements the core ``Scheduler`` protocol: for a date it combines
every enabled binding's slots into one chronologically sorted, calendar-gated,
jittered list of ``TriggerSlot``s — used identically in live and backtest, so the
multi-strategy interleave is structural parity.

It is pure given (bindings, calendar, base_seed, date): reuses ``compute_drift``
(M3.2) and ``calendar.resolve_fire``/``localize`` (M3.3). The stable tie-break key is
exactly ``(fire_ts, strategy_id, slot_id)`` so identical fire times are deterministically
ordered. Skipped slots (closed/overshoot) are recorded for later alerting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from trader.core.types import StrategyBinding, TriggerSlot

from .calendar import TradingCalendar
from .jitter import compute_drift


@dataclass(frozen=True)
class SkippedSlot:
    """A slot the calendar gate dropped, kept for alerting (M3.11)."""

    strategy_id: str
    slot_id: str
    reason: str


class SlotScheduler:
    """Generates the merged, sorted triggers for a date (Scheduler protocol impl)."""

    def __init__(
        self,
        bindings: Sequence[StrategyBinding],
        calendar: TradingCalendar,
        base_seed: int | None,
    ) -> None:
        self._bindings = tuple(bindings)
        self._calendar = calendar
        self._base_seed = base_seed
        self._skipped: list[SkippedSlot] = []

    def triggers_for(self, on_date: date) -> list[TriggerSlot]:
        self._skipped = []
        if not self._calendar.is_session(on_date):
            return []
        triggers: list[TriggerSlot] = []
        for binding in self._bindings:
            if not binding.enabled:
                continue
            for slot in binding.slots:
                drift_seconds, seed = compute_drift(
                    slot, self._base_seed, on_date, binding.strategy_id
                )
                nominal = self._calendar.localize(on_date, slot.at)
                resolved = self._calendar.resolve_fire(
                    nominal + timedelta(seconds=drift_seconds), slot
                )
                if resolved is None:
                    self._skipped.append(
                        SkippedSlot(binding.strategy_id, slot.slot_id, "calendar gate skipped")
                    )
                    continue
                if self._calendar.session_date_of(resolved) != on_date:
                    # A drift large enough to cross midnight would land the trigger on a
                    # different session (only reachable via an uncapped SlotSpec — config
                    # caps drift). Drop it rather than fire on the wrong day.
                    self._skipped.append(
                        SkippedSlot(binding.strategy_id, slot.slot_id, "drift crossed session")
                    )
                    continue
                triggers.append(
                    TriggerSlot(
                        strategy_id=binding.strategy_id,
                        slot_id=slot.slot_id,
                        fire_ts=resolved,
                        drift_seconds=drift_seconds,
                        seed=seed,
                    )
                )
        # Stable interleave: identical fire_ts ordered by (strategy_id, slot_id).
        triggers.sort(key=lambda t: (t.fire_ts, t.strategy_id, t.slot_id))
        return triggers

    @property
    def skipped(self) -> list[SkippedSlot]:
        """Slots dropped by the calendar gate during the last ``triggers_for`` call."""
        return list(self._skipped)
