"""OAuth orchestration: interactive first-auth + the 7-day re-auth alert (§8.2).

``interactive_authorize`` runs the browser login (opens the authorize URL, captures
the callback code over a loopback HTTPS server, exchanges it, persists the tokens).
``check_token_age`` is the periodic hook the daemon (M3) calls to fire the
proactive re-auth alert 1-2 days before the refresh token's 7-day cap, and to flag
expiry. Everything is dependency-injected so it is testable without a real browser.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from trader.core.protocols import Clock
from trader.schwab.config import SchwabClientConfig
from trader.schwab.errors import SchwabAuthError

from .callback_server import CallbackResult, CallbackServer, generate_self_signed_cert
from .oauth import build_authorize_url, exchange_code
from .token_store import TokenStore
from .tokens import TokenSet

Alerter = Callable[[str], None]
ExchangeFn = Callable[[httpx.Client, SchwabClientConfig, str, Clock], TokenSet]


class _CallbackServerLike(Protocol):
    def serve_until_code(self, timeout: float = ...) -> CallbackResult: ...


CallbackFactory = Callable[..., _CallbackServerLike]


def _noop_alerter(message: str) -> None:
    return None


@dataclass(frozen=True)
class TokenAgeDecision:
    """Outcome of a token-age check (returned for testability + caller dedup)."""

    alerted: bool
    expired: bool
    days_remaining: float | None


class Authenticator:
    """Performs interactive first-auth and the periodic re-auth age check."""

    def __init__(
        self,
        config: SchwabClientConfig,
        http_client: httpx.Client,
        token_store: TokenStore,
        *,
        clock: Clock,
        alerter: Alerter | None = None,
        callback_factory: CallbackFactory = CallbackServer,
        exchange_fn: ExchangeFn = exchange_code,
    ) -> None:
        self._config = config
        self._http = http_client
        self._store = token_store
        self._clock = clock
        self._alerter: Alerter = alerter or _noop_alerter
        self._callback_factory = callback_factory
        self._exchange_fn = exchange_fn

    def interactive_authorize(
        self, *, open_browser: Callable[[str], object] = webbrowser.open, timeout: float = 300.0
    ) -> TokenSet:
        """Run the browser OAuth dance and persist the resulting tokens."""
        state = secrets.token_urlsafe(16)
        parsed = urlparse(self._config.redirect_uri)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8182

        tmpdir = Path(tempfile.mkdtemp(prefix="schwab-oauth-"))
        try:
            certfile, keyfile = tmpdir / "cert.pem", tmpdir / "key.pem"
            generate_self_signed_cert(certfile, keyfile, host=host)
            server = self._callback_factory(
                certfile, keyfile, host=host, port=port, expected_state=state
            )
            open_browser(build_authorize_url(self._config, state=state))
            result = server.serve_until_code(timeout)
            tokens = self._exchange_fn(self._http, self._config, result.code, self._clock)
            self._store.save(tokens)
            return tokens
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def ensure_authenticated(self) -> None:
        if self._store.load() is None:
            raise SchwabAuthError("not authenticated; run `trader reauth`")

    def check_token_age(self) -> TokenAgeDecision:
        """Fire the re-auth alert as the refresh token approaches/exceeds its cap."""
        tok = self._store.load()
        if tok is None:
            self._alerter("Schwab client not authenticated; run `trader reauth`")
            return TokenAgeDecision(alerted=True, expired=False, days_remaining=None)

        max_age = self._config.refresh_token_max_age_days
        lead = self._config.refresh_token_alert_lead_days
        remaining = max_age - tok.refresh_age_days(self._clock)

        if tok.refresh_expired(self._clock, max_age):
            self._alerter("Schwab refresh token EXPIRED — re-auth required now (§16.4 runbook)")
            return TokenAgeDecision(alerted=True, expired=True, days_remaining=remaining)
        if tok.refresh_alert_due(self._clock, max_age, lead):
            self._alerter(
                f"Schwab refresh token expires in ~{remaining:.1f} day(s) — re-auth soon (§16.4)"
            )
            return TokenAgeDecision(alerted=True, expired=False, days_remaining=remaining)
        return TokenAgeDecision(alerted=False, expired=False, days_remaining=remaining)
