"""Structured JSON logging with secret scrubbing and a correlation id (design §12).

``configure_logging()`` installs a structlog pipeline that renders one JSON object
per line. A scrubbing processor redacts sensitive keys (e.g. ``access_token``,
``authorization``, raw ``account_number``) and any registered secret literals, so
tokens never reach the logs even if accidentally passed. ``bind_cycle_id()``
attaches a correlation id (via contextvars) that ties a trade cycle's records
together (inputs → decision → order → fill). Reused by the Schwab client (M1) and
the web UI (M7).
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

import structlog
from structlog.contextvars import bind_contextvars, merge_contextvars, unbind_contextvars
from structlog.typing import EventDict, WrappedLogger

REDACTED = "***"

# Keys whose values are always redacted (compared case-insensitively).
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "token",
        "app_secret",
        "client_secret",
        "secret",
        "authorization",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "account_number",
        "accountnumber",
    }
)

# Exact secret strings to scrub wherever they appear (registered at runtime, e.g.
# the live OAuth token, so it can never leak even via an unexpected field).
_secret_literals: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a literal secret to be scrubbed from all future log output."""
    if value:
        _secret_literals.add(value)


def clear_secrets() -> None:
    """Forget all registered secret literals (mainly for tests)."""
    _secret_literals.clear()


def _scrub_string(value: str) -> str:
    for literal in _secret_literals:
        if literal and literal in value:
            value = value.replace(literal, REDACTED)
    return value


def _scrub_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
                else _scrub_value(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(item) for item in value)
    if isinstance(value, str):
        return _scrub_string(value)
    return value


def _scrub_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    scrubbed: EventDict = _scrub_value(dict(event_dict))
    return scrubbed


def _level_to_int(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)


def configure_logging(
    level: str | int = "INFO",
    *,
    json_output: bool = True,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog: contextvars + level + ISO-UTC timestamp + scrub + render.

    ``stream`` lets tests capture output; production logs to stdout (one JSON line
    per event, collected by the Docker json-file driver, §16).
    """
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _scrub_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(level)),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger (loosely typed at this boundary)."""
    return structlog.get_logger(name)


def bind_cycle_id(cycle_id: str | None = None) -> str:
    """Bind a correlation id into the logging context; generate one if not given."""
    cid = cycle_id or uuid.uuid4().hex
    bind_contextvars(cycle_id=cid)
    return cid


def clear_cycle_id() -> None:
    unbind_contextvars("cycle_id")


@contextmanager
def cycle_context(cycle_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of a trade cycle, then clear it."""
    cid = bind_cycle_id(cycle_id)
    try:
        yield cid
    finally:
        clear_cycle_id()


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
