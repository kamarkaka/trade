"""Contract tests for the OAuth primitives (respx-mocked; no live calls)."""

import base64
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.oauth import build_authorize_url, exchange_code, refresh_access_token
from trader.observability.logging import clear_secrets, configure_logging, get_logger
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import OAUTH_TOKEN_URL
from trader.schwab.errors import (
    SchwabAuthError,
    SchwabBadResponseError,
    SchwabRefreshTokenDeadError,
    SchwabServerError,
)

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def _cfg() -> SchwabClientConfig:
    return SchwabClientConfig(app_key="K", app_secret="S", token_store_path=Path("/tmp/t.sqlite"))


def test_build_authorize_url() -> None:
    url = build_authorize_url(_cfg())
    assert "client_id=K" in url
    assert "response_type=code" in url
    assert "redirect_uri=" in url


@respx.mock
def test_exchange_code_success() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AAA",
                "refresh_token": "RRR",
                "expires_in": 1800,
                "scope": "api",
            },
        )
    )
    with httpx.Client() as client:
        tok = exchange_code(client, _cfg(), "the-code", FakeClock(NOW))
    assert tok.access_token == "AAA"
    assert tok.refresh_token == "RRR"
    assert tok.access_token_expires_at == NOW + timedelta(seconds=1800)
    assert tok.refresh_token_issued_at == NOW


@respx.mock
def test_exchange_sends_basic_auth_and_grant() -> None:
    route = respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "A", "refresh_token": "R", "expires_in": 1800}
        )
    )
    with httpx.Client() as client:
        exchange_code(client, _cfg(), "c", FakeClock(NOW))
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(b"K:S").decode()
    assert b"grant_type=authorization_code" in req.content
    assert b"code=c" in req.content


@respx.mock
def test_refresh_preserves_issued_at_when_no_new_refresh() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "A2", "expires_in": 1800})
    )
    issued = NOW - timedelta(days=3)
    with httpx.Client() as client:
        tok = refresh_access_token(client, _cfg(), "R", FakeClock(NOW), issued_at=issued)
    assert tok.refresh_token == "R"  # preserved
    assert tok.refresh_token_issued_at == issued  # NOT reset (7-day cap is not renewable)


@respx.mock
def test_refresh_with_new_refresh_token_still_preserves_issued_at() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "A", "refresh_token": "R-NEW", "expires_in": 1800}
        )
    )
    issued = NOW - timedelta(days=2)
    with httpx.Client() as client:
        tok = refresh_access_token(client, _cfg(), "R-OLD", FakeClock(NOW), issued_at=issued)
    assert tok.refresh_token == "R-NEW"
    assert tok.refresh_token_issued_at == issued


@respx.mock
def test_refresh_invalid_grant_raises_dead() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with httpx.Client() as client, pytest.raises(SchwabRefreshTokenDeadError):
        refresh_access_token(client, _cfg(), "R", FakeClock(NOW), issued_at=NOW)


@respx.mock
def test_exchange_invalid_grant_is_auth_not_dead() -> None:
    # Same 400 on the EXCHANGE path (e.g. reused/expired code) must be a plain auth
    # error, NOT the dead-refresh case.
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with httpx.Client() as client, pytest.raises(SchwabAuthError) as ei:
        exchange_code(client, _cfg(), "reused-code", FakeClock(NOW))
    assert not isinstance(ei.value, SchwabRefreshTokenDeadError)


@respx.mock
def test_server_error_raises_server() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as client, pytest.raises(SchwabServerError):
        exchange_code(client, _cfg(), "c", FakeClock(NOW))


@respx.mock
def test_missing_access_token_raises_bad_response() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(return_value=httpx.Response(200, json={"refresh_token": "R"}))
    with httpx.Client() as client, pytest.raises(SchwabBadResponseError):
        exchange_code(client, _cfg(), "c", FakeClock(NOW))


@respx.mock
def test_exchange_missing_refresh_token_raises() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "A", "expires_in": 1800})
    )
    with httpx.Client() as client, pytest.raises(SchwabBadResponseError):
        exchange_code(client, _cfg(), "c", FakeClock(NOW))


@respx.mock
def test_missing_expires_in_uses_default() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "A", "refresh_token": "R"})
    )
    with httpx.Client() as client:
        tok = exchange_code(client, _cfg(), "c", FakeClock(NOW))
    assert tok.access_token_expires_at == NOW + timedelta(seconds=1800)  # ACCESS_TOKEN_TTL default


def test_refresh_requires_issued_at() -> None:
    # issued_at is required so the 7-day clock can never be silently reset (§8.2).
    with httpx.Client() as client, pytest.raises(TypeError):
        refresh_access_token(client, _cfg(), "R", FakeClock(NOW))  # type: ignore[call-arg]


@respx.mock
def test_5xx_wins_over_invalid_grant_body() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(503, json={"error": "invalid_grant"})
    )
    with httpx.Client() as client, pytest.raises(SchwabServerError):
        refresh_access_token(client, _cfg(), "R", FakeClock(NOW), issued_at=NOW)


@respx.mock
def test_auth_code_is_scrubbed_from_logs() -> None:
    clear_secrets()
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "A", "refresh_token": "R", "expires_in": 1800}
        )
    )
    try:
        with httpx.Client() as client:
            exchange_code(client, _cfg(), "SECRET-CODE-123", FakeClock(NOW))
        buf = io.StringIO()
        configure_logging(stream=buf)
        get_logger().info("oops", code="SECRET-CODE-123")
        assert "SECRET-CODE-123" not in buf.getvalue()
    finally:
        clear_secrets()


@respx.mock
def test_non_json_error_body_is_auth_error() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(return_value=httpx.Response(400, text="<html>nope</html>"))
    with httpx.Client() as client, pytest.raises(SchwabAuthError):
        exchange_code(client, _cfg(), "c", FakeClock(NOW))


@respx.mock
def test_non_int_expires_in_uses_default() -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "A", "refresh_token": "R", "expires_in": "soon"}
        )
    )
    with httpx.Client() as client:
        tok = exchange_code(client, _cfg(), "c", FakeClock(NOW))
    assert tok.access_token_expires_at == NOW + timedelta(seconds=1800)


@respx.mock
def test_obtained_tokens_are_scrubbed_from_logs() -> None:
    clear_secrets()
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "SEKACC", "refresh_token": "SEKREF", "expires_in": 1800}
        )
    )
    try:
        with httpx.Client() as client:
            exchange_code(client, _cfg(), "c", FakeClock(NOW))
        buf = io.StringIO()
        configure_logging(stream=buf)
        get_logger().info("accidental", leaked="SEKACC and SEKREF here")
        out = buf.getvalue()
        assert "SEKACC" not in out
        assert "SEKREF" not in out
    finally:
        clear_secrets()
