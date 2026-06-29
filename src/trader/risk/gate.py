"""RiskManager gate (design §10): the single, fail-closed chokepoint that composes the
M4.2 rules with dual limit scopes and the same-ticker conflict policy.

**Two limit scopes (design §10).** Every order is checked under both:

- *Per-strategy* — every rule under ``merged_config`` (the account defaults updated with
  this strategy's ``risk_overrides``). "Checked first."
- *Account-wide hard guardrail* — the account-wide rules (gross exposure, daily loss,
  trades/day) plus the non-overridable denylist, under ``account_config``. Overrides can
  never *loosen* these.

An order is approved only if it passes **both**. The per-strategy notional clamp is
applied first and the quantity-dependent caps are evaluated on the post-clamp order, so
a clamp-to-fit order is not spuriously rejected.

**Conflict policy (design §10).** ``resolve_conflicts`` reconciles same-ticker decisions
from different strategies in one cycle before sizing: ``net`` (default) sums signed
deltas per symbol into one order (contributors retained for pro-rata fill attribution),
``independent`` sends each on its own, ``priority`` keeps the highest-priority strategy's
decisions per symbol and drops the rest.

The gate is fail-closed: missing/stale/uncertain data rejects (inherited from the rules).
The per-strategy vs account-wide *day-state / attributed-position* split is supplied by
the orchestrator wiring in M4.4; here both scopes differ by config.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from trader.config.models import RiskConfig
from trader.core import Account, DayState, Decision, Order, Position, Quote, RiskVerdict
from trader.core.enums import Action, ConflictPolicy
from trader.core.protocols import Clock
from trader.observability.logging import get_logger
from trader.risk import rules
from trader.risk.rules import RuleContext, RuleResult


@dataclass(frozen=True)
class ResolvedDecision:
    """One post-conflict-resolution intent for a symbol. ``contributors`` records each
    originating strategy's signed share delta so a netted fill can be split back
    pro-rata (attribution, M3.9)."""

    symbol: str
    action: Action  # BUY or SELL (HOLD/zero nets are dropped)
    quantity: int
    contributors: tuple[tuple[str, int], ...]
    limit_price: Decimal | None = None


def _signed(decision: Decision) -> int:
    return decision.quantity if decision.action is Action.BUY else -decision.quantity


class RiskManager:
    """The single fail-closed risk gate (implements the core ``RiskManager`` Protocol)."""

    def __init__(
        self,
        *,
        account_config: RiskConfig,
        clock: Clock,
        overrides_by_strategy: dict[str, dict[str, object]] | None = None,
        default_policy: ConflictPolicy | None = None,
        priority_order: Sequence[str] = (),
        seen_client_order_ids: Iterable[str] = (),
    ) -> None:
        self._account_config = account_config
        self._clock = clock
        self._overrides = dict(overrides_by_strategy or {})
        self._default_policy = default_policy or account_config.conflict_policy
        self._priority_order = tuple(priority_order)
        self._seen: set[str] = set(seen_client_order_ids)
        self._log = get_logger("risk")

    # -- limit scopes -------------------------------------------------------- #

    def _merged_config(self, strategy_id: str) -> RiskConfig:
        """Account defaults with this strategy's ``risk_overrides`` applied (re-validated)."""
        overrides = self._overrides.get(strategy_id)
        if not overrides:
            return self._account_config
        return RiskConfig(**{**self._account_config.model_dump(), **overrides})

    def check(
        self,
        order: Order,
        positions: Sequence[Position],
        account: Account,
        quote: Quote,
        day_state: DayState,
    ) -> RiskVerdict:
        merged = self._merged_config(order.strategy_id)
        acct = self._account_config
        now = self._clock.now()
        seen = frozenset(self._seen)

        def ctx(cfg: RiskConfig) -> RuleContext:
            return RuleContext(cfg, positions, account, quote, day_state, now, seen)

        reasons: list[str] = []
        notes: list[str] = []

        # 1) Per-strategy notional clamp first (may reject outright or clamp the size).
        notional = rules.max_order_notional(order, ctx(merged))
        if not notional.ok:
            reasons.append(f"per-strategy max_order_notional: {notional.reason}")
        eff_qty = order.quantity if notional.clamped_quantity is None else notional.clamped_quantity
        eff_order = order if eff_qty == order.quantity else replace(order, quantity=eff_qty)
        if eff_order is not order:
            notes.append(f"clamped {order.quantity}->{eff_qty}: {notional.reason}")

        # 2) Remaining rules, dual scope, evaluated on the POST-CLAMP order. Account-wide
        #    entries are the hard guardrail (overrides cannot loosen them).
        checks: tuple[tuple[Callable[[Order, RuleContext], RuleResult], RiskConfig, str], ...] = (
            (rules.allowlist_denylist, merged, "per-strategy"),
            (rules.allowlist_denylist, acct, "account-wide"),
            (rules.duplicate_order_guard, merged, "per-strategy"),
            (rules.price_sanity, merged, "per-strategy"),
            (rules.daily_loss_limit, merged, "per-strategy"),
            (rules.daily_loss_limit, acct, "account-wide"),
            (rules.max_trades_per_day, merged, "per-strategy"),
            (rules.max_trades_per_day, acct, "account-wide"),
            (rules.max_position_size, merged, "per-strategy"),
            (rules.max_gross_exposure, merged, "per-strategy"),
            (rules.max_gross_exposure, acct, "account-wide"),
        )
        for rule, cfg, scope in checks:
            res = rule(eff_order, ctx(cfg))
            if not res.ok:
                reasons.append(f"{scope} {rule.__name__}: {res.reason}")

        reasons = list(dict.fromkeys(reasons))  # account==merged can duplicate a reason
        approved = not reasons
        final_reasons = tuple(reasons) if reasons else tuple(notes)
        adjusted = eff_order if (approved and eff_order is not order) else None
        verdict = RiskVerdict(approved=approved, adjusted_order=adjusted, reasons=final_reasons)
        self._log_verdict(order, eff_qty, verdict)
        if approved:
            self._seen.add(order.client_order_id)
        return verdict

    def _log_verdict(self, order: Order, eff_qty: int, verdict: RiskVerdict) -> None:
        if not verdict.approved:
            self._log.warning(
                "risk rejected",
                cid=order.client_order_id,
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                reasons=list(verdict.reasons),
            )
        elif eff_qty != order.quantity:
            self._log.info(
                "risk clamped",
                cid=order.client_order_id,
                symbol=order.symbol,
                from_qty=order.quantity,
                to_qty=eff_qty,
            )
        else:
            self._log.info("risk approved", cid=order.client_order_id, symbol=order.symbol)

    # -- conflict policy ----------------------------------------------------- #

    def resolve_conflicts(
        self,
        decisions: Sequence[tuple[str, Decision]],
        policy: ConflictPolicy | None = None,
    ) -> list[ResolvedDecision]:
        """Reconcile same-ticker decisions across strategies for one cycle (design §10)."""
        policy = policy or self._default_policy
        active = [
            (sid, d) for sid, d in decisions if d.action is not Action.HOLD and d.quantity > 0
        ]
        if policy is ConflictPolicy.INDEPENDENT:
            return [
                ResolvedDecision(
                    d.symbol, d.action, d.quantity, ((sid, _signed(d)),), d.limit_price
                )
                for sid, d in active
            ]
        if policy is ConflictPolicy.PRIORITY:
            return self._resolve_priority(active)
        return self._resolve_net(active)

    def _grouped(
        self, active: Sequence[tuple[str, Decision]]
    ) -> dict[str, list[tuple[str, Decision]]]:
        groups: dict[str, list[tuple[str, Decision]]] = {}
        for sid, d in active:
            groups.setdefault(d.symbol, []).append((sid, d))
        return groups

    def _resolve_net(self, active: Sequence[tuple[str, Decision]]) -> list[ResolvedDecision]:
        resolved: list[ResolvedDecision] = []
        for symbol, group in self._grouped(active).items():
            net = sum(_signed(d) for _, d in group)
            if net == 0:
                self._log.info("conflict netted to flat", symbol=symbol)
                continue  # offsetting decisions cancel -> no order, never cross our own spread
            contributors = tuple((sid, _signed(d)) for sid, d in group)
            limits = {d.limit_price for _, d in group}
            limit_price = limits.pop() if len(limits) == 1 else None  # mixed limits -> market
            action = Action.BUY if net > 0 else Action.SELL
            resolved.append(ResolvedDecision(symbol, action, abs(net), contributors, limit_price))
        return resolved

    def _resolve_priority(self, active: Sequence[tuple[str, Decision]]) -> list[ResolvedDecision]:
        resolved: list[ResolvedDecision] = []
        for symbol, group in self._grouped(active).items():
            winner = min(group, key=lambda item: self._rank(item[0]))[0]
            for sid, d in group:
                if sid == winner:
                    resolved.append(
                        ResolvedDecision(
                            symbol, d.action, d.quantity, ((sid, _signed(d)),), d.limit_price
                        )
                    )
                else:
                    self._log.info(
                        "conflict dropped by priority", symbol=symbol, dropped=sid, winner=winner
                    )
        return resolved

    def _rank(self, strategy_id: str) -> int:
        # Lower rank wins; strategies absent from the configured order rank last (stable
        # by first appearance because min() is stable on equal keys).
        try:
            return self._priority_order.index(strategy_id)
        except ValueError:
            return len(self._priority_order)


__all__ = ["ResolvedDecision", "RiskManager"]
