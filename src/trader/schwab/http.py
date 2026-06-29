"""Resilient, auth-aware HTTP transport for the Schwab client (design §8.2/§8.6).

The security/correctness heart of M1. ``request()``:
1. refuses if in READ-ONLY safe mode;
2. injects a valid bearer token (proactively refreshing an expired access token);
3. throttles via the token bucket;
4. on 401, refreshes once and retries once (a second 401 raises);
5. maps 429/5xx to typed errors that tenacity retries with backoff (honoring
   Retry-After); maps other 4xx to non-retryable auth/HTTP errors;
6. flips to READ-ONLY safe mode + alerts when the refresh token is dead.

Token values never reach logs (scrubbed centrally + the bearer is only in headers).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from trader.auth.oauth import refresh_access_token
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.core.protocols import Clock
from trader.observability.logging import get_logger

from .config import SchwabClientConfig
from .errors import (
    SchwabAuthError,
    SchwabBadResponseError,
    SchwabError,
    SchwabRateLimitError,
    SchwabReadOnlyModeError,
    SchwabRefreshTokenDeadError,
    SchwabServerError,
)
from .rate_limit import TokenBucket
from .retry import make_retrying

Alerter = Callable[[str], None]
RefreshFn = Callable[..., TokenSet]


def _noop_alerter(message: str) -> None:
    return None


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form not modeled; fall back to exponential backoff


class SchwabHttp:
    """Authenticated, rate-limited, retrying transport over an injected httpx client."""

    def __init__(
        self,
        config: SchwabClientConfig,
        http_client: httpx.Client,
        token_store: TokenStore,
        *,
        clock: Clock,
        rate_limiter: TokenBucket | None = None,
        alerter: Alerter | None = None,
        refresh_fn: RefreshFn = refresh_access_token,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._client = http_client
        self._tokens = token_store
        self._clock = clock
        self._rate_limiter = rate_limiter or TokenBucket(config.rate_limit_per_min)
        self._alerter: Alerter = alerter or _noop_alerter
        self._refresh_fn: RefreshFn = refresh_fn
        self._retrying = make_retrying(config, sleep=sleep)
        self._safe_mode = False
        self._log = get_logger("schwab.http")

    @property
    def is_read_only(self) -> bool:
        return self._safe_mode

    def enter_safe_mode(self, reason: str) -> None:
        if not self._safe_mode:
            self._safe_mode = True
            self._log.warning("entering READ-ONLY safe mode", reason=reason)
            self._alerter(f"Schwab client entering READ-ONLY safe mode: {reason}")

    def _refresh(self, tok: TokenSet) -> TokenSet:
        try:
            new = self._refresh_fn(
                self._client,
                self._config,
                tok.refresh_token,
                self._clock,
                issued_at=tok.refresh_token_issued_at,
            )
        except SchwabRefreshTokenDeadError:
            self.enter_safe_mode("refresh token dead (interactive re-auth required)")
            raise
        self._tokens.save(new)
        return new

    def _bearer(self) -> str:
        tok = self._tokens.load()
        if tok is None:
            raise SchwabAuthError("not authenticated; run reauth")
        if tok.access_expired(self._clock):
            tok = self._refresh(tok)
        return tok.access_token

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        if self._safe_mode:
            raise SchwabReadOnlyModeError("Schwab client is in READ-ONLY safe mode")
        return self._retrying(self._send, method, url, params, json, allow_refresh=True)

    def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        json: Any,
        *,
        allow_refresh: bool,
    ) -> httpx.Response:
        bearer = self._bearer()
        self._rate_limiter.acquire()
        self._log.debug("request", method=method, url=url)
        resp = self._client.request(
            method, url, params=params, json=json, headers={"Authorization": f"Bearer {bearer}"}
        )
        status = resp.status_code
        if status == 401 and allow_refresh:
            tok = self._tokens.load()
            if tok is not None:
                self._refresh(tok)
            return self._send(method, url, params, json, allow_refresh=False)
        if status == 401:
            raise SchwabAuthError("unauthorized (after refresh)", status_code=401)
        if status == 429:
            raise SchwabRateLimitError(
                "rate limited", status_code=429, retry_after=_parse_retry_after(resp)
            )
        if status >= 500:
            raise SchwabServerError(f"server error {status}", status_code=status)
        if status == 403:
            raise SchwabAuthError("forbidden", status_code=403)
        if status >= 400:
            raise SchwabError(f"http error {status}", status_code=status)
        return resp

    def get_json(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        resp = self.request("GET", url, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise SchwabBadResponseError("non-JSON response from Schwab") from exc
