"""OAuth token model + age logic (design §8.2).

We track ``refresh_token_issued_at`` ourselves so we can compute the refresh
token's age against the 7-day cap and alert ahead of expiry. All time math goes
through an injected ``Clock`` so it is deterministic in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from trader.core.protocols import Clock


@dataclass(frozen=True, repr=False)
class TokenSet:
    """A persisted set of OAuth tokens with their issue/expiry timestamps."""

    access_token: str
    refresh_token: str
    access_token_expires_at: datetime  # tz-aware UTC
    refresh_token_issued_at: datetime  # tz-aware UTC (our own tracked value)
    scope: str | None = None

    def __repr__(self) -> str:
        # Never expose token values (logs/tracebacks); §8.2/§13 scrub mandate.
        return (
            "TokenSet(access_token=***, refresh_token=***, "
            f"access_token_expires_at={self.access_token_expires_at.isoformat()}, "
            f"refresh_token_issued_at={self.refresh_token_issued_at.isoformat()}, "
            f"scope={self.scope!r})"
        )

    def __post_init__(self) -> None:
        for name in ("access_token_expires_at", "refresh_token_issued_at"):
            value: datetime = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware (UTC)")

    def access_expired(self, clock: Clock, skew_seconds: int = 60) -> bool:
        """True if the access token is expired (or within ``skew_seconds`` of it)."""
        return clock.now() >= self.access_token_expires_at - timedelta(seconds=skew_seconds)

    def refresh_age_days(self, clock: Clock) -> float:
        return (clock.now() - self.refresh_token_issued_at).total_seconds() / 86400.0

    def refresh_expired(self, clock: Clock, max_age_days: float) -> bool:
        return self.refresh_age_days(clock) >= max_age_days

    def refresh_alert_due(self, clock: Clock, max_age_days: float, lead_days: float) -> bool:
        """True once the refresh token is within ``lead_days`` of its ``max_age_days`` cap."""
        return self.refresh_age_days(clock) >= (max_age_days - lead_days)
