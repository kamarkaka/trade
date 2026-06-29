"""Tests for the alerting channels: event formatting, dispatch (mocked transports),
fan-out resilience, env-construction, and no-secret-in-body (M4.5)."""

import json
from email.message import EmailMessage
from typing import ClassVar

import httpx
import respx

from trader.observability.alerting import (
    AlertEvent,
    AlertKind,
    AlertSeverity,
    EmailAlerter,
    MultiAlerter,
    TelegramAlerter,
    build_alerter,
)
from trader.observability.logging import clear_secrets

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


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
    clear_secrets()


@respx.mock
def test_telegram_raises_on_http_error() -> None:
    respx.post(TELEGRAM_URL.format(token="T0KEN")).mock(return_value=httpx.Response(500))
    try:
        TelegramAlerter("T0KEN", "chat42").alert(_event())
        raise AssertionError("expected an HTTP error to propagate to the fan-out")
    except httpx.HTTPStatusError:
        pass
    clear_secrets()


def test_telegram_from_env_absent_returns_none() -> None:
    assert TelegramAlerter.from_env({}) is None
    assert TelegramAlerter.from_env({"TELEGRAM_BOT_TOKEN": "x"}) is None  # chat id missing
    clear_secrets()


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
    clear_secrets()


def test_email_from_env_partial_returns_none() -> None:
    assert EmailAlerter.from_env({"SMTP_HOST": "h"}) is None  # other required vars missing


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
    clear_secrets()


# --- env construction ------------------------------------------------------- #


def test_build_alerter_includes_only_configured_with_creds() -> None:
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}  # email creds absent
    multi = build_alerter(("telegram", "email"), environ=env)
    assert len(multi._channels) == 1  # only telegram constructed
    assert isinstance(multi._channels[0], TelegramAlerter)
    clear_secrets()


def test_build_alerter_empty_when_no_creds() -> None:
    assert build_alerter(("telegram", "email"), environ={})._channels == []
