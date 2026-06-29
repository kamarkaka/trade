"""Tests for the resilient Schwab HTTP transport: proactive refresh, 401->refresh->
retry, 429/5xx backoff (incl. Retry-After), non-retryable 4xx, dead-refresh safe
mode, and not-authenticated. Deterministic (FakeClock + injected sleep; respx)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import OAUTH_TOKEN_URL, QUOTES_PATH
from trader.schwab.errors import (
    SchwabAuthError,
    SchwabBadResponseError,
    SchwabReadOnlyModeError,
    SchwabRefreshTokenDeadError,
    SchwabServerError,
)
from trader.schwab.http import SchwabHttp

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
DATA_URL = QUOTES_PATH


def _build(
    tmp_path: Path, client: httpx.Client, *, access_in_s: int = 1800, max_retries: int = 2
) -> tuple[SchwabHttp, TokenStore, list[float], list[str]]:
    cfg = SchwabClientConfig(
        app_key="K", app_secret="S", token_store_path=tmp_path / "t.sqlite", max_retries=max_retries
    )
    store = TokenStore(tmp_path / "t.sqlite")
    store.save(TokenSet("ACC", "REF", NOW + timedelta(seconds=access_in_s), NOW))
    sleeps: list[float] = []
    alerts: list[str] = []
    http = SchwabHttp(
        cfg, client, store, clock=FakeClock(NOW), sleep=sleeps.append, alerter=alerts.append
    )
    return http, store, sleeps, alerts


@respx.mock
def test_valid_token_no_refresh(tmp_path: Path) -> None:
    data = respx.get(DATA_URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
    # No token route registered → if a refresh is attempted, respx raises.
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client)
        resp = http.request("GET", DATA_URL)
    assert resp.status_code == 200
    assert data.calls.last.request.headers["Authorization"] == "Bearer ACC"


@respx.mock
def test_proactive_refresh_when_access_expired(tmp_path: Path) -> None:
    token = respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ACC2", "refresh_token": "REF", "expires_in": 1800}
        )
    )
    data = respx.get(DATA_URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client, access_in_s=-10)  # already expired
        resp = http.request("GET", DATA_URL)
    assert resp.status_code == 200
    assert token.call_count == 1
    assert data.calls.last.request.headers["Authorization"] == "Bearer ACC2"


@respx.mock
def test_401_triggers_refresh_then_retry(tmp_path: Path) -> None:
    token = respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ACC2", "refresh_token": "REF", "expires_in": 1800}
        )
    )
    data = respx.get(DATA_URL).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": 1})]
    )
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client)
        resp = http.request("GET", DATA_URL)
    assert resp.status_code == 200
    assert token.call_count == 1
    assert data.call_count == 2


@respx.mock
def test_double_401_raises(tmp_path: Path) -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ACC2", "refresh_token": "REF", "expires_in": 1800}
        )
    )
    respx.get(DATA_URL).mock(return_value=httpx.Response(401))
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client)
        with pytest.raises(SchwabAuthError):
            http.request("GET", DATA_URL)


@respx.mock
def test_429_backoff_then_success(tmp_path: Path) -> None:
    data = respx.get(DATA_URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(429), httpx.Response(200, json={"ok": 1})]
    )
    with httpx.Client() as client:
        http, _store, sleeps, _ = _build(tmp_path, client, max_retries=2)
        resp = http.request("GET", DATA_URL)
    assert resp.status_code == 200
    assert data.call_count == 3
    assert len(sleeps) == 2
    assert all(s > 0 for s in sleeps)


@respx.mock
def test_429_honors_retry_after(tmp_path: Path) -> None:
    respx.get(DATA_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    with httpx.Client() as client:
        http, _store, sleeps, _ = _build(tmp_path, client)
        http.request("GET", DATA_URL)
    assert sleeps == [7.0]


@respx.mock
def test_5xx_exhausts_retries(tmp_path: Path) -> None:
    data = respx.get(DATA_URL).mock(return_value=httpx.Response(503))
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client, max_retries=2)
        with pytest.raises(SchwabServerError):
            http.request("GET", DATA_URL)
    assert data.call_count == 3  # initial + 2 retries


@respx.mock
def test_403_not_retried(tmp_path: Path) -> None:
    data = respx.get(DATA_URL).mock(return_value=httpx.Response(403))
    with httpx.Client() as client:
        http, _store, sleeps, _ = _build(tmp_path, client)
        with pytest.raises(SchwabAuthError):
            http.request("GET", DATA_URL)
    assert data.call_count == 1
    assert sleeps == []


@respx.mock
def test_generic_4xx_raises_and_not_retried(tmp_path: Path) -> None:
    from trader.schwab.errors import SchwabError

    data = respx.get(DATA_URL).mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        http, _store, sleeps, _ = _build(tmp_path, client)
        with pytest.raises(SchwabError):
            http.request("GET", DATA_URL)
    assert data.call_count == 1
    assert sleeps == []


@respx.mock
def test_retry_after_non_numeric_falls_back_to_backoff(tmp_path: Path) -> None:
    respx.get(DATA_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    with httpx.Client() as client:
        http, _store, sleeps, _ = _build(tmp_path, client)
        http.request("GET", DATA_URL)
    assert len(sleeps) == 1
    assert sleeps[0] > 0  # exponential fallback, not the (unparsed) date


@respx.mock
def test_dead_refresh_enters_safe_mode(tmp_path: Path) -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    respx.get(DATA_URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
    with httpx.Client() as client:
        http, _store, _sleeps, alerts = _build(tmp_path, client, access_in_s=-10)  # forces refresh
        with pytest.raises(SchwabRefreshTokenDeadError):
            http.request("GET", DATA_URL)
        assert http.is_read_only is True
        assert len(alerts) == 1
        # subsequent requests are refused without touching the network
        with pytest.raises(SchwabReadOnlyModeError):
            http.request("GET", DATA_URL)


@respx.mock
def test_no_token_raises_not_authenticated(tmp_path: Path) -> None:
    data = respx.get(DATA_URL).mock(return_value=httpx.Response(200))
    cfg = SchwabClientConfig(app_key="K", app_secret="S", token_store_path=tmp_path / "t.sqlite")
    store = TokenStore(tmp_path / "t.sqlite")  # empty
    with httpx.Client() as client:
        http = SchwabHttp(cfg, client, store, clock=FakeClock(NOW))
        with pytest.raises(SchwabAuthError):
            http.request("GET", DATA_URL)
    assert data.call_count == 0  # never hit the network


@respx.mock
def test_rate_limiter_is_invoked_per_send(tmp_path: Path) -> None:
    class _SpyLimiter:
        def __init__(self) -> None:
            self.calls = 0

        def acquire(self) -> float:
            self.calls += 1
            return 0.0

    respx.get(DATA_URL).mock(return_value=httpx.Response(200, json={"ok": 1}))
    spy = _SpyLimiter()
    cfg = SchwabClientConfig(app_key="K", app_secret="S", token_store_path=tmp_path / "t.sqlite")
    store = TokenStore(tmp_path / "t.sqlite")
    store.save(TokenSet("ACC", "REF", NOW + timedelta(seconds=1800), NOW))
    with httpx.Client() as client:
        http = SchwabHttp(cfg, client, store, clock=FakeClock(NOW), rate_limiter=spy)  # type: ignore[arg-type]
        http.request("GET", DATA_URL)
    assert spy.calls == 1


@respx.mock
def test_get_json(tmp_path: Path) -> None:
    respx.get(DATA_URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client)
        assert http.get_json(DATA_URL) == {"a": 1}


@respx.mock
def test_get_json_non_json_raises(tmp_path: Path) -> None:
    respx.get(DATA_URL).mock(return_value=httpx.Response(200, text="not json"))
    with httpx.Client() as client:
        http, *_ = _build(tmp_path, client)
        with pytest.raises(SchwabBadResponseError):
            http.get_json(DATA_URL)
