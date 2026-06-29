"""Go-live guard rails (design §10).

Switching to **live** (real money) requires TWO explicit signals — ``mode: live`` in config
AND an out-of-band confirmation (``TRADER_CONFIRM_LIVE=I_UNDERSTAND`` or ``--confirm-live``) —
so live can never be entered silently or by a single typo. Before the daemon starts it must
pass ``live_preflight``: a conservative-rollout gate that refuses to start unless the rollout
is safe.

IMPORTANT (M5.6): the live submit path is **not yet idempotent** — the at-most-once layer
(``submit_idempotent`` + a production reconciler) is wired during guarded live verification
(M5.7). Until ``LIVE_ORDER_PATH_READY`` is flipped on there, ``live_preflight`` returns an
unconditional blocker so ``trader run`` **refuses every live start** and no real order can be
placed. M5.6 ships the gate machinery; M5.7 turns it on with the first real order.

These functions are pure/inspectable so the safety gate is CI-enforced, not manual.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from trader.config.models import AppConfig
from trader.core.types import StrategyBinding
from trader.observability.alerting import Alerter, AlertEvent, AlertKind

# The out-of-band confirmation signals (the SECOND signal beyond mode: live).
CONFIRM_ENV_VAR = "TRADER_CONFIRM_LIVE"
CONFIRM_PHRASE = "I_UNDERSTAND"

# Guarded first-rollout ceilings (design §10: "start with the smallest possible exposure").
# Live preflight refuses if the EFFECTIVE caps (incl. per-strategy overrides) exceed these.
MAX_LIVE_ORDER_NOTIONAL_USD = Decimal("1000")
MAX_LIVE_POSITION_SIZE_PCT = 5.0
MAX_LIVE_GROSS_EXPOSURE_USD = Decimal("5000")

# Flipped ON in M5.7 once the live submit path is idempotent (write-ahead + reconcile-before-
# resend wired into the orchestrator). While False, live preflight refuses to start.
LIVE_ORDER_PATH_READY = False


@dataclass(frozen=True)
class PreflightProblem:
    check: str
    detail: str


def live_confirmed(*, confirm_flag: bool, environ: dict[str, str]) -> bool:
    """True iff the second go-live signal is present (CLI flag or the exact env phrase)."""
    return confirm_flag or environ.get(CONFIRM_ENV_VAR) == CONFIRM_PHRASE


def announce_live(alerter: Alerter) -> None:
    """Emit the mandatory loud startup alert when going LIVE — live state is never silent
    (design §10). CRITICAL severity so it can't be missed."""
    alerter.alert(AlertEvent(AlertKind.CRASH, "trader STARTING IN LIVE MODE — real orders enabled"))


def _effective_caps(
    config: AppConfig, bindings: Sequence[StrategyBinding]
) -> list[tuple[str, Decimal, float]]:
    """Per (enabled) strategy: the EFFECTIVE max_order_notional + max_position_size_pct after
    applying its risk_overrides over the account defaults. (Both keys are per-strategy
    overridable, so the account value alone is not the enforced cap.)"""
    base_notional = config.risk.max_order_notional_usd
    base_pct = config.risk.max_position_size_pct
    out: list[tuple[str, Decimal, float]] = []
    for b in bindings:
        if not b.enabled:
            continue
        ov = b.risk_overrides or {}
        notional = (
            Decimal(str(ov["max_order_notional_usd"]))
            if "max_order_notional_usd" in ov
            else base_notional
        )
        pct = (
            float(str(ov["max_position_size_pct"]))
            if "max_position_size_pct" in ov
            else base_pct
        )
        out.append((b.strategy_id, notional, pct))
    return out


def live_preflight(
    config: AppConfig,
    bindings: Sequence[StrategyBinding],
    *,
    kill_switch_engaged: bool,
    token_valid: bool,
    alert_channel_count: int,
    reconcile_clean: bool = True,
) -> list[PreflightProblem]:
    """Conservative go-live checks. Returns the list of problems (empty == cleared to start).

    Inputs that need the DB / token store / network (kill switch, token, reconcile, alert
    channels) are computed by the caller and passed in, keeping this pure + unit-testable."""
    problems: list[PreflightProblem] = []

    if not LIVE_ORDER_PATH_READY:
        problems.append(
            PreflightProblem(
                "idempotency",
                "live submit path is not yet idempotent / reconcile-before-resend (M5.7); "
                "refusing real orders",
            )
        )

    risk = config.risk
    if not risk.allowlist:
        problems.append(
            PreflightProblem(
                "allowlist", "live requires a non-empty risk.allowlist (explicit default-deny)"
            )
        )
    # Effective per-strategy caps (a risk_override must not raise a cap above the ceiling).
    for sid, notional, pct in _effective_caps(config, bindings):
        if notional > MAX_LIVE_ORDER_NOTIONAL_USD:
            problems.append(
                PreflightProblem(
                    "max_order_notional_usd",
                    f"strategy {sid!r} effective {notional} exceeds the guarded-rollout ceiling "
                    f"{MAX_LIVE_ORDER_NOTIONAL_USD}",
                )
            )
        if pct > MAX_LIVE_POSITION_SIZE_PCT:
            problems.append(
                PreflightProblem(
                    "max_position_size_pct",
                    f"strategy {sid!r} effective {pct}% exceeds the ceiling "
                    f"{MAX_LIVE_POSITION_SIZE_PCT}%",
                )
            )
    if risk.max_gross_exposure_usd > MAX_LIVE_GROSS_EXPOSURE_USD:
        problems.append(
            PreflightProblem(
                "max_gross_exposure_usd",
                f"{risk.max_gross_exposure_usd} exceeds the guarded-rollout ceiling "
                f"{MAX_LIVE_GROSS_EXPOSURE_USD}",
            )
        )
    if alert_channel_count < 1:
        problems.append(
            PreflightProblem(
                "alerting", "live requires at least one configured alert channel (never silent)"
            )
        )
    if kill_switch_engaged:
        problems.append(
            PreflightProblem("kill_switch", "kill switch is engaged; release it before going live")
        )
    if not token_valid:
        problems.append(
            PreflightProblem(
                "token", "no valid Schwab token; run `trader reauth` before going live"
            )
        )
    if not reconcile_clean:
        problems.append(
            PreflightProblem(
                "reconcile", "startup reconciliation found unexplained divergence; resolve first"
            )
        )
    return problems


__all__ = [
    "CONFIRM_ENV_VAR",
    "CONFIRM_PHRASE",
    "LIVE_ORDER_PATH_READY",
    "MAX_LIVE_GROSS_EXPOSURE_USD",
    "MAX_LIVE_ORDER_NOTIONAL_USD",
    "MAX_LIVE_POSITION_SIZE_PCT",
    "PreflightProblem",
    "announce_live",
    "live_confirmed",
    "live_preflight",
]
