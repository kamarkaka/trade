"""OAuth authorization-code + refresh primitives for Schwab (design §8.2).

Low-level functions over an injected ``httpx.Client`` (so the transport is shared
and tests can mock with respx). Endpoint shapes are parity-checked against
schwab-py / Schwabdev (not imported). Obtained tokens are registered as secret
literals so they are scrubbed from all logs.

The refresh token has a hard 7-day cap and is NOT renewable (§8.2): a refresh
grant therefore PRESERVES ``refresh_token_issued_at`` (the original interactive
auth time), even if Schwab returns a new refresh-token string. Only the
interactive authorization-code exchange starts the clock.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from trader.core.protocols import Clock
from trader.observability.logging import register_secret
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import (
    ACCESS_TOKEN_TTL_SECONDS,
    OAUTH_AUTHORIZE_URL,
    OAUTH_TOKEN_URL,
)
from trader.schwab.errors import (
    SchwabAuthError,
    SchwabBadResponseError,
    SchwabRefreshTokenDeadError,
    SchwabServerError,
)

from .tokens import TokenSet

_DEAD_REFRESH_ERRORS = {"invalid_grant", "invalid_token"}


def build_authorize_url(config: SchwabClientConfig) -> str:
    """Build the interactive authorization URL the operator opens in a browser."""
    params = {
        "client_id": config.app_key,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth_header(config: SchwabClientConfig) -> str:
    raw = f"{config.app_key}:{config.app_secret.get_secret_value()}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _post_token(
    http_client: httpx.Client, config: SchwabClientConfig, data: dict[str, str]
) -> httpx.Response:
    return http_client.post(
        OAUTH_TOKEN_URL,
        data=data,
        headers={
            "Authorization": _basic_auth_header(config),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _parse_token_response(
    resp: httpx.Response,
    clock: Clock,
    *,
    on_refresh: bool,
    prior_refresh_token: str | None = None,
    prior_issued_at: datetime | None = None,
) -> TokenSet:
    if resp.status_code >= 500:
        raise SchwabServerError(f"token endpoint {resp.status_code}", status_code=resp.status_code)
    if resp.status_code >= 400:
        body = _safe_json(resp)
        err = body.get("error") if isinstance(body, dict) else None
        if on_refresh and err in _DEAD_REFRESH_ERRORS:
            raise SchwabRefreshTokenDeadError(
                "refresh token rejected; interactive re-auth required", status_code=resp.status_code
            )
        raise SchwabAuthError(
            f"token endpoint error: {err or resp.status_code}", status_code=resp.status_code
        )

    body = _safe_json(resp)
    if not isinstance(body, dict) or "access_token" not in body:
        raise SchwabBadResponseError("token response missing access_token")

    access_token = str(body["access_token"])
    try:
        expires_in = int(body.get("expires_in", ACCESS_TOKEN_TTL_SECONDS))
    except (TypeError, ValueError):
        expires_in = ACCESS_TOKEN_TTL_SECONDS

    new_refresh = body.get("refresh_token")
    refresh_token = str(new_refresh) if new_refresh else prior_refresh_token
    if not refresh_token:
        raise SchwabBadResponseError("token response missing refresh_token")

    now = clock.now()
    # Preserve the original auth time on refresh (7-day cap is not renewable, §8.2);
    # only the interactive code-exchange starts the clock.
    issued_at = prior_issued_at if (on_refresh and prior_issued_at is not None) else now

    register_secret(access_token)
    register_secret(refresh_token)
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=now + timedelta(seconds=expires_in),
        refresh_token_issued_at=issued_at,
        scope=body.get("scope"),
    )


def exchange_code(
    http_client: httpx.Client, config: SchwabClientConfig, code: str, clock: Clock
) -> TokenSet:
    """Exchange a single-use authorization code for a fresh TokenSet (§8.2)."""
    register_secret(code)  # the auth code is a credential; keep it out of any logs
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": config.redirect_uri}
    resp = _post_token(http_client, config, data)
    return _parse_token_response(resp, clock, on_refresh=False)


def refresh_access_token(
    http_client: httpx.Client,
    config: SchwabClientConfig,
    refresh_token: str,
    clock: Clock,
    *,
    issued_at: datetime,
) -> TokenSet:
    """Refresh the access token, preserving the refresh token's original issue time.

    ``issued_at`` is REQUIRED (the original interactive-auth time, from the stored
    TokenSet) so the 7-day cap is never silently reset (design §8.2).
    """
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    resp = _post_token(http_client, config, data)
    return _parse_token_response(
        resp, clock, on_refresh=True, prior_refresh_token=refresh_token, prior_issued_at=issued_at
    )
