"""Go-live guard rails (design §10).

Switching to **live** (real money) requires TWO explicit signals — ``mode: live`` in config
AND an out-of-band confirmation (``TRADER_CONFIRM_LIVE=I_UNDERSTAND`` or ``--confirm-live``) —
so live can never be entered silently or by a single typo. Before the daemon starts it must
pass ``live_preflight``: a conservative-rollout gate (default-deny allowlist, small caps, kill
switch off, a valid token, clean reconciliation) that refuses to start otherwise.

These functions are pure/inspectable so the safety gate is CI-enforced, not manual.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trader.config.models import AppConfig
from trader.observability.alerting import Alerter, AlertEvent, AlertKind

# The out-of-band confirmation signals (the SECOND signal beyond mode: live).
CONFIRM_ENV_VAR = "TRADER_CONFIRM_LIVE"
CONFIRM_PHRASE = "I_UNDERSTAND"

# Guarded first-rollout ceilings (design §10: "start with the smallest possible exposure").
# Live preflight refuses if the configured caps exceed these — go live small, then loosen.
MAX_LIVE_ORDER_NOTIONAL_USD = Decimal("1000")
MAX_LIVE_POSITION_SIZE_PCT = 5.0


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


def live_preflight(
    config: AppConfig,
    *,
    kill_switch_engaged: bool,
    token_valid: bool,
    reconcile_clean: bool = True,
) -> list[PreflightProblem]:
    """Conservative go-live checks. Returns the list of problems (empty == cleared to start).

    ``kill_switch_engaged`` / ``token_valid`` / ``reconcile_clean`` are computed by the caller
    (from the state DB / token store / a startup reconcile) and passed in, keeping this pure
    and unit-testable without network or DB."""
    problems: list[PreflightProblem] = []
    risk = config.risk

    if not risk.allowlist:
        problems.append(
            PreflightProblem(
                "allowlist", "live requires a non-empty risk.allowlist (explicit default-deny)"
            )
        )
    if risk.max_order_notional_usd > MAX_LIVE_ORDER_NOTIONAL_USD:
        problems.append(
            PreflightProblem(
                "max_order_notional_usd",
                f"{risk.max_order_notional_usd} exceeds the guarded-rollout ceiling "
                f"{MAX_LIVE_ORDER_NOTIONAL_USD}; go live small first",
            )
        )
    if risk.max_position_size_pct > MAX_LIVE_POSITION_SIZE_PCT:
        problems.append(
            PreflightProblem(
                "max_position_size_pct",
                f"{risk.max_position_size_pct}% exceeds the guarded-rollout ceiling "
                f"{MAX_LIVE_POSITION_SIZE_PCT}%",
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
    "MAX_LIVE_ORDER_NOTIONAL_USD",
    "MAX_LIVE_POSITION_SIZE_PCT",
    "PreflightProblem",
    "announce_live",
    "live_confirmed",
    "live_preflight",
]
