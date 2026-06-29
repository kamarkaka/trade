"""Configuration for the first-party Schwab client (design §8).

Credentials are held as ``SecretStr`` so they never str/repr into logs. This is
distinct from the app-wide ``AppConfig`` (§11); it is assembled from the secrets
component (M1) and a few system settings.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SchwabClientConfig(BaseModel):
    """Settings + credentials for the Schwab client. Frozen and secret-safe."""

    model_config = ConfigDict(frozen=True)

    app_key: str
    app_secret: SecretStr
    redirect_uri: str = "https://127.0.0.1:8182"
    token_store_path: Path
    rate_limit_per_min: int = Field(default=100, ge=1, le=120)
    refresh_token_max_age_days: int = Field(default=7, gt=0)
    refresh_token_alert_lead_days: int = Field(default=2, ge=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0)

    @field_validator("redirect_uri")
    @classmethod
    def _must_be_https(cls, v: str) -> str:
        # Schwab requires HTTPS even for loopback redirects (§8.1).
        if not v.startswith("https://"):
            raise ValueError(f"redirect_uri must use https://, got {v!r}")
        return v
