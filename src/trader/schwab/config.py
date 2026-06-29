"""Configuration for the first-party Schwab client (design §8).

Credentials are held as ``SecretStr`` so they never str/repr into logs. This is
distinct from the app-wide ``AppConfig`` (§11); it is assembled from the secrets
component (M1) and a few system settings.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .errors import SchwabAuthError


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


def schwab_config_from_env(
    *,
    default_token_store: str | Path,
    rate_limit_per_min: int = 100,
    environ: Mapping[str, str] | None = None,
    require_credentials: bool = False,
) -> SchwabClientConfig:
    """Assemble a SchwabClientConfig from process env (the secrets boundary, §13).

    Credentials come only from the environment (never the YAML config, never the
    repo): ``SCHWAB_APP_KEY`` / ``SCHWAB_APP_SECRET``; the redirect URI and an
    optional token-store path override may also be set. ``require_credentials``
    is True for commands that hit the network (e.g. ``reauth``) and False for
    read-only inspection (e.g. ``status`` reading token age), which needs only the
    token-store path. ``default_token_store`` / ``rate_limit_per_min`` are passed
    by the caller (from AppConfig) so this stays decoupled from the app config.
    """
    env = os.environ if environ is None else environ
    app_key = env.get("SCHWAB_APP_KEY", "")
    app_secret = env.get("SCHWAB_APP_SECRET", "")
    if require_credentials and not (app_key and app_secret):
        raise SchwabAuthError(
            "Schwab credentials missing; set SCHWAB_APP_KEY and SCHWAB_APP_SECRET"
        )
    token_store = env.get("SCHWAB_TOKEN_STORE_PATH") or str(default_token_store)
    return SchwabClientConfig(
        app_key=app_key,
        app_secret=SecretStr(app_secret),
        redirect_uri=env.get("SCHWAB_REDIRECT_URI", "https://127.0.0.1:8182"),
        token_store_path=Path(token_store),
        rate_limit_per_min=rate_limit_per_min,
    )
