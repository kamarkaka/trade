"""Observability: structured logging + secret scrubbing (M0.6). Metrics, the audit
trail, alerting, and the heartbeat are added in later milestones."""

from __future__ import annotations

from .logging import (
    REDACTED,
    SENSITIVE_KEYS,
    bind_cycle_id,
    clear_cycle_id,
    clear_secrets,
    configure_logging,
    cycle_context,
    get_logger,
    register_secret,
)

__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "bind_cycle_id",
    "clear_cycle_id",
    "clear_secrets",
    "configure_logging",
    "cycle_context",
    "get_logger",
    "register_secret",
]
