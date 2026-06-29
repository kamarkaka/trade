"""Redundant alerting (design §12/§13).

Failures, kill-switch trips, reconciliation mismatches and the weekly re-auth reminder
must never be silent, so alerts fan out to MULTIPLE channels — one failing channel can't
swallow a critical event. Every alert is a typed ``AlertEvent`` (a small taxonomy + a
severity derived from the kind) rendered to a single line.

Credentials come from the environment only (§13), never the repo/config/image; channel
tokens are registered as secrets so they are scrubbed from logs. Transports are injectable
so CI never sends a real message.
"""

from __future__ import annotations

import os
import smtplib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from enum import StrEnum

import httpx

from trader.observability.logging import get_logger, register_secret


class AlertKind(StrEnum):
    """The alert taxonomy (design §12). Every alert is classified so channels/operators
    can route and prioritise."""

    CRASH = "crash"
    BROKER_ERROR = "broker_error"
    AUTH_ERROR = "auth_error"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS = "daily_loss"
    RECONCILE_MISMATCH = "reconcile_mismatch"
    STALE_DATA = "stale_data"
    SKIPPED_SLOT = "skipped_slot"
    REAUTH_REMINDER = "reauth_reminder"
    HEARTBEAT = "heartbeat"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


_SEVERITY_BY_KIND: Mapping[AlertKind, AlertSeverity] = {
    AlertKind.CRASH: AlertSeverity.CRITICAL,
    AlertKind.BROKER_ERROR: AlertSeverity.CRITICAL,
    AlertKind.AUTH_ERROR: AlertSeverity.CRITICAL,
    AlertKind.KILL_SWITCH: AlertSeverity.CRITICAL,
    AlertKind.DAILY_LOSS: AlertSeverity.CRITICAL,
    AlertKind.RECONCILE_MISMATCH: AlertSeverity.CRITICAL,
    AlertKind.STALE_DATA: AlertSeverity.WARNING,
    AlertKind.SKIPPED_SLOT: AlertSeverity.WARNING,
    AlertKind.REAUTH_REMINDER: AlertSeverity.WARNING,
    AlertKind.HEARTBEAT: AlertSeverity.INFO,
}


@dataclass(frozen=True)
class AlertEvent:
    """One classified alert. Severity is derived from the kind so it can't drift."""

    kind: AlertKind
    message: str

    @property
    def severity(self) -> AlertSeverity:
        return _SEVERITY_BY_KIND.get(self.kind, AlertSeverity.WARNING)

    def format(self) -> str:
        return f"[{self.severity.value}] {self.kind.value}: {self.message}"


class Alerter:
    """Structural interface every channel/aggregator implements: ``alert(event)``."""

    def alert(self, event: AlertEvent) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class TelegramAlerter(Alerter):
    """Posts to the Telegram bot API via httpx (a dep since M1)."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=timeout)
        register_secret(bot_token)  # scrub the token from any log line
        self._log = get_logger("alerting.telegram")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TelegramAlerter | None:
        env = environ if environ is not None else os.environ
        token = env.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return None
        return cls(token, chat_id)

    def alert(self, event: AlertEvent) -> None:
        # The token travels in the URL (required by the API); the message body never
        # carries credentials.
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        response = self._client.post(url, json={"chat_id": self._chat_id, "text": event.format()})
        response.raise_for_status()


class EmailAlerter(Alerter):
    """Sends mail via stdlib smtplib (STARTTLS). The SMTP factory is injectable for tests."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_addr: str,
        to_addrs: Sequence[str],
        smtp_factory: Callable[[str, int], smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from = from_addr
        self._to = list(to_addrs)
        self._smtp_factory = smtp_factory
        register_secret(password)  # scrub the SMTP password from any log line
        self._log = get_logger("alerting.email")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EmailAlerter | None:
        env = environ if environ is not None else os.environ
        required = (
            "SMTP_HOST",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
            "ALERT_EMAIL_FROM",
            "ALERT_EMAIL_TO",
        )
        if any(not env.get(k) for k in required):
            return None
        return cls(
            host=env["SMTP_HOST"],
            port=int(env.get("SMTP_PORT", "587")),
            username=env["SMTP_USERNAME"],
            password=env["SMTP_PASSWORD"],
            from_addr=env["ALERT_EMAIL_FROM"],
            to_addrs=[a.strip() for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()],
        )

    def alert(self, event: AlertEvent) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[trader] {event.severity.value} {event.kind.value}"
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        msg.set_content(event.format())
        with self._smtp_factory(self._host, self._port) as smtp:
            smtp.starttls()
            smtp.login(self._username, self._password)
            smtp.send_message(msg)


class MultiAlerter(Alerter):
    """Fans out to every channel; one channel's failure never stops the others or raises
    to the caller (alerting must not crash the trader). A total failure is logged CRITICAL."""

    def __init__(self, channels: Sequence[Alerter]) -> None:
        self._channels = list(channels)
        self._log = get_logger("alerting")

    def alert(self, event: AlertEvent) -> None:
        delivered = 0
        for channel in self._channels:
            try:
                channel.alert(event)
                delivered += 1
            except Exception as exc:  # one bad channel must not silence the others
                self._log.error(
                    "alert channel failed",
                    channel=type(channel).__name__,
                    kind=event.kind.value,
                    error=str(exc),
                )
        if self._channels and delivered == 0:
            # Every channel failed: the alert is effectively lost — make that loud locally.
            self._log.critical("all alert channels failed", kind=event.kind.value)


def build_alerter(
    channels: Sequence[str] = ("telegram", "email"),
    *,
    environ: Mapping[str, str] | None = None,
) -> MultiAlerter:
    """Construct a MultiAlerter from the configured channel names, including only those
    whose credentials are present in the environment (design §11 alerting + §13 secrets)."""
    built: list[Alerter] = []
    factories: dict[str, Callable[[], Alerter | None]] = {
        "telegram": lambda: TelegramAlerter.from_env(environ),
        "email": lambda: EmailAlerter.from_env(environ),
    }
    for name in channels:
        factory = factories.get(name)
        if factory is None:
            continue
        channel = factory()
        if channel is not None:
            built.append(channel)
    return MultiAlerter(built)


__all__ = [
    "AlertEvent",
    "AlertKind",
    "AlertSeverity",
    "Alerter",
    "EmailAlerter",
    "MultiAlerter",
    "TelegramAlerter",
    "build_alerter",
]
