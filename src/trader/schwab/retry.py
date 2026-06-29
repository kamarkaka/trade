"""Retry/backoff policy for the Schwab HTTP transport (design §8.6).

Retries ONLY rate-limit (429) and server (5xx) errors — never auth/4xx. Uses
exponential backoff with jitter, but honors a server-provided ``Retry-After``
when present. The sleep function is injectable so tests never wall-sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import tenacity

from .config import SchwabClientConfig
from .errors import SchwabRateLimitError, SchwabServerError


def make_retrying(
    config: SchwabClientConfig, *, sleep: Callable[[float], None] | None = None
) -> tenacity.Retrying:
    base_wait = tenacity.wait_exponential_jitter(initial=0.5, max=30.0)

    def wait(retry_state: tenacity.RetryCallState) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, SchwabRateLimitError) and exc.retry_after is not None:
            return float(exc.retry_after)
        return base_wait(retry_state)

    return tenacity.Retrying(
        retry=tenacity.retry_if_exception_type((SchwabRateLimitError, SchwabServerError)),
        wait=wait,
        stop=tenacity.stop_after_attempt(config.max_retries + 1),  # initial try + retries
        reraise=True,
        sleep=sleep if sleep is not None else time.sleep,
    )
