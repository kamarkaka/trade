"""Typed error taxonomy for the Schwab client (design §8.7).

A closed hierarchy so callers can distinguish retryable transport failures from
auth failures and the fatal dead-refresh-token case that drives READ-ONLY safe
mode.
"""

from __future__ import annotations


class SchwabError(Exception):
    """Base for all Schwab client errors. Messages must be pre-scrubbed of secrets."""

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SchwabAuthError(SchwabError):
    """Authentication/authorization failure (4xx auth, bad/expired credentials)."""


class SchwabTokenExpiredError(SchwabAuthError):
    """Access token expired (recoverable by refresh)."""


class SchwabRefreshTokenDeadError(SchwabAuthError):
    """Refresh token is dead (7-day cap / invalid_grant) — requires interactive
    re-auth. Drives READ-ONLY safe mode."""


class SchwabRateLimitError(SchwabError):
    """HTTP 429 — retryable with backoff."""


class SchwabServerError(SchwabError):
    """HTTP 5xx — retryable with backoff."""


class SchwabBadResponseError(SchwabError):
    """Malformed / unparseable response from Schwab."""


class SchwabReadOnlyModeError(SchwabError):
    """The client is in READ-ONLY safe mode and refused a request."""
