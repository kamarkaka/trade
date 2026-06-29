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


class AlertSendError(Exception):
    """A channel failed to deliver. Carries only a sanitized message — NEVER the underlying
    transport error string, which (for Telegram) embeds the bot token in the request URL."""


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
        # carries credentials. httpx errors stringify the URL (token included), so we
        # NEVER let the raw exception escape — re-raise a sanitized error so the token
        # can't reach a log line even if logging isn't configured to scrub it.
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            response = self._client.post(
                url, json={"chat_id": self._chat_id, "text": event.format()}
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AlertSendError(f"telegram send failed: HTTP {exc.response.status_code}") from None
        except Exception as exc:
            # Catch-all (not just httpx.HTTPError): some httpx errors are NOT HTTPError
            # subclasses, and any of them may stringify the token-bearing URL. Surface only
            # the exception TYPE so the token can never escape through this raise.
            raise AlertSendError(f"telegram send failed: {type(exc).__name__}") from None

    def close(self) -> None:
        self._client.close()


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
        use_ssl: bool = False,
        smtp_factory: Callable[[str, int], smtplib.SMTP] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from = from_addr
        self._to = list(to_addrs)
        # 465 = implicit TLS (SMTP_SSL, no STARTTLS); 587 = STARTTLS on a plain socket.
        self._use_ssl = use_ssl
        default_factory: Callable[[str, int], smtplib.SMTP] = (
            smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        )
        self._smtp_factory = smtp_factory or default_factory
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
        raw_port = env.get("SMTP_PORT", "587")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise AlertSendError(f"invalid SMTP_PORT {raw_port!r}") from exc
        use_ssl = port == 465 or env.get("SMTP_USE_SSL", "").lower() in ("1", "true", "yes")
        return cls(
            host=env["SMTP_HOST"],
            port=port,
            username=env["SMTP_USERNAME"],
            password=env["SMTP_PASSWORD"],
            from_addr=env["ALERT_EMAIL_FROM"],
            to_addrs=[a.strip() for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()],
            use_ssl=use_ssl,
        )

    def alert(self, event: AlertEvent) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[trader] {event.severity.value} {event.kind.value}"
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        msg.set_content(event.format())
        with self._smtp_factory(self._host, self._port) as smtp:
            if not self._use_ssl:
                smtp.starttls()  # 587: upgrade the plain socket; 465 is already encrypted
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
                # Log only the exception TYPE + sanitized message — never str() of an
                # arbitrary transport error, which could embed a credential (see
                # AlertSendError). Secret literals are also scrubbed as a second layer.
                self._log.error(
                    "alert channel failed",
                    channel=type(channel).__name__,
                    kind=event.kind.value,
                    error_type=type(exc).__name__,
                    error=str(exc) if isinstance(exc, AlertSendError) else "",
                )
        if self._channels and delivered == 0:
            # Every channel failed: the alert is effectively lost — make that loud locally.
            self._log.critical(
                "all alert channels failed",
                kind=event.kind.value,
                delivered=delivered,
                total=len(self._channels),
            )

    def close(self) -> None:
        for channel in self._channels:
            closer = getattr(channel, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # one channel's cleanup must not abort the rest
                    self._log.warning(
                        "alert channel close failed",
                        channel=type(channel).__name__,
                        error_type=type(exc).__name__,
                    )


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
    log = get_logger("alerting")
    for name in channels:
        factory = factories.get(name)
        if factory is None:
            log.warning("unknown alert channel ignored", channel=name)
            continue
        try:
            channel = factory()
        except Exception as exc:
            # A misconfigured channel (e.g. a bad SMTP_PORT) must NOT crash wire-up or
            # drop the other channels -- skip it loudly and keep building the rest.
            log.error("alert channel construction failed", channel=name, error=str(exc))
            continue
        if channel is not None:
            built.append(channel)
    return MultiAlerter(built)


__all__ = [
    "AlertEvent",
    "AlertKind",
    "AlertSendError",
    "AlertSeverity",
    "Alerter",
    "EmailAlerter",
    "MultiAlerter",
    "TelegramAlerter",
    "build_alerter",
]
