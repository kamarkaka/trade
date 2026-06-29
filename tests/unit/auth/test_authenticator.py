"""Tests for the Authenticator: interactive authorize (fakes) + token-age alert."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fakes import FakeClock
from trader.auth.authenticator import Authenticator
from trader.auth.callback_server import CallbackResult
from trader.auth.token_store import TokenStore
from trader.auth.tokens import TokenSet
from trader.schwab.config import SchwabClientConfig
from trader.schwab.constants import OAUTH_TOKEN_URL
from trader.schwab.errors import SchwabAuthError

NOW = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)


def _cfg(tmp_path: Path) -> SchwabClientConfig:
    return SchwabClientConfig(app_key="K", app_secret="S", token_store_path=tmp_path / "t.sqlite")


def _save(store: TokenStore, *, refresh_age_days: float) -> None:
    store.save(
        TokenSet("A", "R", NOW + timedelta(minutes=30), NOW - timedelta(days=refresh_age_days))
    )


def test_check_token_age_no_token_alerts(tmp_path: Path) -> None:
    alerts: list[str] = []
    store = TokenStore(tmp_path / "t.sqlite")
    with httpx.Client() as client:
        auth = Authenticator(
            _cfg(tmp_path), client, store, clock=FakeClock(NOW), alerter=alerts.append
        )
        decision = auth.check_token_age()
    assert decision.alerted is True
    assert decision.days_remaining is None
    assert "not authenticated" in alerts[0]


def test_check_token_age_alert_due_at_5_days(tmp_path: Path) -> None:
    alerts: list[str] = []
    store = TokenStore(tmp_path / "t.sqlite")
    _save(store, refresh_age_days=5.0)
    with httpx.Client() as client:
        auth = Authenticator(
            _cfg(tmp_path), client, store, clock=FakeClock(NOW), alerter=alerts.append
        )
        decision = auth.check_token_age()
    assert decision.alerted is True
    assert decision.expired is False
    assert len(alerts) == 1


def test_check_token_age_no_alert_before_lead(tmp_path: Path) -> None:
    alerts: list[str] = []
    store = TokenStore(tmp_path / "t.sqlite")
    _save(store, refresh_age_days=4.0)  # cap 7, lead 2 → alert at 5
    with httpx.Client() as client:
        auth = Authenticator(
            _cfg(tmp_path), client, store, clock=FakeClock(NOW), alerter=alerts.append
        )
        decision = auth.check_token_age()
    assert decision.alerted is False
    assert alerts == []


def test_check_token_age_expired(tmp_path: Path) -> None:
    alerts: list[str] = []
    store = TokenStore(tmp_path / "t.sqlite")
    _save(store, refresh_age_days=7.5)
    with httpx.Client() as client:
        auth = Authenticator(
            _cfg(tmp_path), client, store, clock=FakeClock(NOW), alerter=alerts.append
        )
        decision = auth.check_token_age()
    assert decision.expired is True
    assert "EXPIRED" in alerts[0]


def test_ensure_authenticated(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.sqlite")
    with httpx.Client() as client:
        auth = Authenticator(_cfg(tmp_path), client, store, clock=FakeClock(NOW))
        with pytest.raises(SchwabAuthError):
            auth.ensure_authenticated()
        _save(store, refresh_age_days=0.0)
        auth.ensure_authenticated()  # no raise


def test_interactive_authorize_cleans_up_tmpdir_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile as _tempfile

    created: dict[str, str] = {}
    real_mkdtemp = _tempfile.mkdtemp

    def spy_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]
        created["dir"] = path
        return path

    monkeypatch.setattr("trader.auth.authenticator.tempfile.mkdtemp", spy_mkdtemp)

    def factory(certfile: object, keyfile: object, **kw: object) -> object:
        class _Boom:
            def serve_until_code(self, timeout: float = 300.0) -> CallbackResult:
                raise SchwabAuthError("callback failed")

        return _Boom()

    store = TokenStore(tmp_path / "t.sqlite")
    with httpx.Client() as client:
        auth = Authenticator(
            _cfg(tmp_path), client, store, clock=FakeClock(NOW), callback_factory=factory
        )
        with pytest.raises(SchwabAuthError):
            auth.interactive_authorize(open_browser=lambda _u: None)
    # ephemeral cert dir is cleaned up even on failure, and no token persisted
    assert not Path(created["dir"]).exists()
    assert store.load() is None


@respx.mock
def test_interactive_authorize_saves_token_and_checks_state(tmp_path: Path) -> None:
    respx.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "ACC", "refresh_token": "REF", "expires_in": 1800}
        )
    )
    store = TokenStore(tmp_path / "t.sqlite")
    captured: dict[str, object] = {}
    opened: dict[str, str] = {}

    def factory(certfile: object, keyfile: object, **kw: object) -> object:
        captured.update(kw)

        class _FakeServer:
            def serve_until_code(self, timeout: float = 300.0) -> CallbackResult:
                return CallbackResult(code="THECODE", state=kw.get("expected_state"))  # type: ignore[arg-type]

        return _FakeServer()

    def open_browser(url: str) -> None:
        opened["url"] = url

    with httpx.Client() as client:
        auth = Authenticator(
            _cfg(tmp_path), client, store, clock=FakeClock(NOW), callback_factory=factory
        )
        tokens = auth.interactive_authorize(open_browser=open_browser)

    assert tokens.access_token == "ACC"
    assert store.load() is not None
    # CSRF state was generated, put in the URL, and given to the callback server
    state = captured["expected_state"]
    assert isinstance(state, str) and state
    assert f"state={state}" in opened["url"]
