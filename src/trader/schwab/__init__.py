"""First-party Charles Schwab Trader API client (design §8 / §8.7).

Built in-house (no third-party broker SDK); the open-source clients schwab-py /
Schwabdev are referenced only as a parity spec. This package holds the OAuth-aware
HTTP transport, typed endpoint models, and read endpoints; the live ``Broker``
adapter is layered on top in M5.
"""

from __future__ import annotations

from .config import SchwabClientConfig
from .errors import (
    SchwabAuthError,
    SchwabBadResponseError,
    SchwabError,
    SchwabRateLimitError,
    SchwabReadOnlyModeError,
    SchwabRefreshTokenDeadError,
    SchwabServerError,
    SchwabTokenExpiredError,
)

__all__ = [
    "SchwabAuthError",
    "SchwabBadResponseError",
    "SchwabClientConfig",
    "SchwabError",
    "SchwabRateLimitError",
    "SchwabReadOnlyModeError",
    "SchwabRefreshTokenDeadError",
    "SchwabServerError",
    "SchwabTokenExpiredError",
]
