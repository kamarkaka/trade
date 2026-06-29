"""Tests for the alerting channels: event formatting, dispatch (mocked transports),
fan-out resilience, env-construction, and no-secret-in-body (M4.5)."""

import json
from collections.abc import Iterator
from email.message import EmailMessage
from typing import ClassVar

import httpx
import pytest
import respx

from trader.observability.alerting import (
    AlertEvent,
    AlertKind,
    AlertSendError,
    AlertSeverity,
    EmailAlerter,
    MultiAlerter,
    TelegramAlerter,
    build_alerter,
)
from trader.observability.logging import clear_secrets

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


@pytest.fixture(autouse=True)
def _clear_secrets() -> Iterator[None]:
    # Clear in teardown so a failing assertion can't leak registered secrets into later tests.
    yield
    clear_secrets()


def _event(kind: AlertKind = AlertKind.CRASH, message: str = "boom") -> AlertEvent:
    return AlertEvent(kind, message)


# --- event taxonomy --------------------------------------------------------- #


def test_severity_derived_from_kind() -> None:
    assert _event(AlertKind.CRASH).severity is AlertSeverity.CRITICAL
    assert _event(AlertKind.HEARTBEAT).severity is AlertSeverity.INFO
    assert _event(AlertKind.SKIPPED_SLOT).severity is AlertSeverity.WARNING


def test_format_single_line() -> None:
    assert _event(AlertKind.DAILY_LOSS, "down 3%").format() == "[CRITICAL] daily_loss: down 3%"


# --- telegram --------------------------------------------------------------- #


@respx.mock
def test_telegram_formats_and_posts() -> None:
    route = respx.post(TELEGRAM_URL.format(token="T0KEN")).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    TelegramAlerter("T0KEN", "chat42").alert(_event(AlertKind.KILL_SWITCH, "tripped"))
    assert route.called
    body = route.calls.last.request
    payload = json.loads(body.content)
    assert payload == {"chat_id": "chat42", "text": "[CRITICAL] kill_switch: tripped"}


@respx.mock
def test_telegram_http_error_sanitized_no_token_leak() -> None:
    # C1: an httpx error stringifies the token-bearing URL; the alerter must raise a
    # sanitized AlertSendError instead so the token can't reach a log line.
    token = "LEAKYTOKEN999"
    respx.post(TELEGRAM_URL.format(token=token)).mock(return_value=httpx.Response(401))
    with pytest.raises(AlertSendError) as exc:
        TelegramAlerter(token, "chat42").alert(_event())
    assert token not in str(exc.value)
    assert "401" in str(exc.value)


@respx.mock
def test_telegram_connection_error_sanitized() -> None:
    token = "LEAKYTOKEN999"
    respx.post(TELEGRAM_URL.format(token=token)).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AlertSendError) as exc:
        TelegramAlerter(token, "chat42").alert(_event())
    assert token not in str(exc.value)


def test_telegram_from_env_absent_returns_none() -> None:
    assert TelegramAlerter.from_env({}) is None
    assert TelegramAlerter.from_env({"TELEGRAM_BOT_TOKEN": "x"}) is None  # chat id missing


# --- email ------------------------------------------------------------------ #


class _FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.logged_in = False
        self.tls = False

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starttls(self) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = True

    def send_message(self, msg: EmailMessage) -> None:
        _FakeSMTP.sent.append(msg)


def test_email_sends_via_smtp() -> None:
    _FakeSMTP.sent = []
    alerter = EmailAlerter(
        host="smtp.test",
        port=587,
        username="u",
        password="pw",
        from_addr="bot@test",
        to_addrs=["ops@test"],
        smtp_factory=_FakeSMTP,
    )
    alerter.alert(_event(AlertKind.RECONCILE_MISMATCH, "drift +4 AAPL"))
    assert len(_FakeSMTP.sent) == 1
    msg = _FakeSMTP.sent[0]
    assert msg["To"] == "ops@test"
    assert "drift +4 AAPL" in msg.get_content()


def test_email_ssl_skips_starttls() -> None:
    _FakeSMTP.sent = []
    captured: list[_FakeSMTP] = []

    def factory(host: str, port: int) -> _FakeSMTP:
        smtp = _FakeSMTP(host, port)
        captured.append(smtp)
        return smtp

    alerter = EmailAlerter(
        host="smtp.test",
        port=465,
        username="u",
        password="pw",
        from_addr="bot@test",
        to_addrs=["ops@test"],
        use_ssl=True,
        smtp_factory=factory,
    )
    alerter.alert(_event())
    assert captured[0].tls is False  # 465 = implicit TLS, STARTTLS must NOT be called
    assert len(_FakeSMTP.sent) == 1


def test_email_from_env_partial_returns_none() -> None:
    assert EmailAlerter.from_env({"SMTP_HOST": "h"}) is None  # other required vars missing


def test_email_from_env_sets_ssl_for_465() -> None:
    env = {
        "SMTP_HOST": "h",
        "SMTP_PORT": "465",
        "SMTP_USERNAME": "u",
        "SMTP_PASSWORD": "pw",
        "ALERT_EMAIL_FROM": "f@t",
        "ALERT_EMAIL_TO": "o@t",
    }
    alerter = EmailAlerter.from_env(env)
    assert alerter is not None and alerter._use_ssl is True


def test_email_from_env_bad_port_raises_alert_send_error() -> None:
    env = {
        "SMTP_HOST": "h",
        "SMTP_PORT": "587x",
        "SMTP_USERNAME": "u",
        "SMTP_PASSWORD": "pw",
        "ALERT_EMAIL_FROM": "f@t",
        "ALERT_EMAIL_TO": "o@t",
    }
    with pytest.raises(AlertSendError):
        EmailAlerter.from_env(env)


# --- fan-out resilience ----------------------------------------------------- #


class _RecordingAlerter:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def alert(self, event: AlertEvent) -> None:
        self.events.append(event)


class _FailingAlerter:
    def alert(self, event: AlertEvent) -> None:
        raise RuntimeError("channel down")


def test_multialerter_one_channel_fails_others_still_send() -> None:
    ok = _RecordingAlerter()
    multi = MultiAlerter([_FailingAlerter(), ok])
    multi.alert(_event())  # must not raise despite the failing channel
    assert len(ok.events) == 1


def test_multialerter_all_fail_does_not_raise() -> None:
    MultiAlerter([_FailingAlerter(), _FailingAlerter()]).alert(_event())  # logged, not raised


def test_multialerter_empty_does_not_raise_or_alert() -> None:
    MultiAlerter([]).alert(_event())  # no channels: no CRITICAL, no exception


@respx.mock
def test_multialerter_survives_telegram_connection_error() -> None:
    token = "T0KEN"
    respx.post(TELEGRAM_URL.format(token=token)).mock(side_effect=httpx.ConnectError("down"))
    ok = _RecordingAlerter()
    MultiAlerter([TelegramAlerter(token, "c"), ok]).alert(_event())  # connect error is caught
    assert len(ok.events) == 1


# --- no secrets in body ----------------------------------------------------- #


@respx.mock
def test_no_secrets_in_alert_body() -> None:
    secret_token = "SUPERSECRET123"
    route = respx.post(TELEGRAM_URL.format(token=secret_token)).mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    TelegramAlerter(secret_token, "chat42").alert(_event(AlertKind.CRASH, "stack trace here"))
    payload = json.loads(route.calls.last.request.content)
    assert secret_token not in payload["text"]  # the token lives only in the URL, not the body


# --- env construction ------------------------------------------------------- #


def test_build_alerter_includes_only_configured_with_creds() -> None:
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}  # email creds absent
    multi = build_alerter(("telegram", "email"), environ=env)
    assert len(multi._channels) == 1  # only telegram constructed
    assert isinstance(multi._channels[0], TelegramAlerter)


def test_build_alerter_empty_when_no_creds() -> None:
    assert build_alerter(("telegram", "email"), environ={})._channels == []


def test_build_alerter_bad_email_port_keeps_other_channels() -> None:
    # C2/H1: a malformed SMTP_PORT must NOT crash wire-up or drop telegram.
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "SMTP_HOST": "h",
        "SMTP_PORT": "587x",  # malformed
        "SMTP_USERNAME": "u",
        "SMTP_PASSWORD": "pw",
        "ALERT_EMAIL_FROM": "f@t",
        "ALERT_EMAIL_TO": "o@t",
    }
    multi = build_alerter(("telegram", "email"), environ=env)
    assert len(multi._channels) == 1  # email skipped, telegram survives
    assert isinstance(multi._channels[0], TelegramAlerter)


def test_build_alerter_unknown_channel_ignored() -> None:
    multi = build_alerter(("carrier_pigeon",), environ={})
    assert multi._channels == []
