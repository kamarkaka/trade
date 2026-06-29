"""Tests for the liveness heartbeat: freshness, stale-alert (silent-death detection),
and the ``status --healthcheck`` exit codes (M4.6)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from trader.app.cli import app
from trader.clock import RealClock
from trader.observability.alerting import AlertEvent, AlertKind
from trader.observability.heartbeat import Heartbeat
from trader.state.db import connect
from trader.state.migrate import run_migrations

NOW = datetime(2026, 6, 29, 15, 0, tzinfo=UTC)
runner = CliRunner()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, at: datetime | None = None) -> bool:
        return True


class _RecordingAlerter:
    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def alert(self, event: AlertEvent) -> None:
        self.events.append(event)


def _hb(tmp_path: Path, clock: _Clock, *, max_age: float = 60, alerter: object = None) -> Heartbeat:
    conn = connect(tmp_path / "state.sqlite")
    run_migrations(conn)
    return Heartbeat(conn, clock=clock, max_age_seconds=max_age, alerter=alerter)  # type: ignore[arg-type]


# --- freshness -------------------------------------------------------------- #


def test_fresh_heartbeat_healthy(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    hb = _hb(tmp_path, clock)
    hb.touch()
    assert hb.is_alive() is True
    clock._now = NOW + timedelta(seconds=59)
    assert hb.is_alive() is True  # within max_age (60s)
    clock._now = NOW + timedelta(seconds=61)
    assert hb.is_alive() is False  # past max_age


def test_touch_upserts_and_read_returns_record(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    hb = _hb(tmp_path, clock)
    hb.touch(scheduler_state="running", detail="3 jobs")
    clock._now = NOW + timedelta(seconds=10)
    hb.touch(scheduler_state="running", detail="4 jobs")  # second touch updates the singleton
    record = hb.read()
    assert record is not None
    assert record.scheduler_state == "running" and record.detail == "4 jobs"
    assert record.last_alive_at == NOW + timedelta(seconds=10)


# --- stale / missing -> alert ----------------------------------------------- #


def test_stale_heartbeat_unhealthy_and_alerts(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    alerter = _RecordingAlerter()
    hb = _hb(tmp_path, clock, max_age=60, alerter=alerter)
    hb.touch()
    clock._now = NOW + timedelta(seconds=300)
    assert hb.check() is False
    assert len(alerter.events) == 1
    assert alerter.events[0].kind is AlertKind.CRASH
    assert "stale" in alerter.events[0].message


def test_missing_heartbeat_unhealthy_and_alerts(tmp_path: Path) -> None:
    alerter = _RecordingAlerter()
    hb = _hb(tmp_path, _Clock(NOW), alerter=alerter)  # never touched
    assert hb.is_alive() is False
    assert hb.check() is False
    assert "no heartbeat" in alerter.events[0].message


def test_check_fresh_does_not_alert(tmp_path: Path) -> None:
    alerter = _RecordingAlerter()
    hb = _hb(tmp_path, _Clock(NOW), alerter=alerter)
    hb.touch()
    assert hb.check() is True
    assert alerter.events == []


# --- healthcheck exit codes ------------------------------------------------- #


def _write_config(path: Path, db_path: Path) -> None:
    path.write_text(
        f"""
mode: paper
strategies:
  - id: momentum
    name: threshold
    universe: [AAPL]
    slots:
      - {{id: open, time: "09:45"}}
observability:
  db_path: "{db_path}"
""",
        encoding="utf-8",
    )


def test_healthcheck_exit_codes(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    cfg = tmp_path / "c.yaml"
    _write_config(cfg, db)

    # (a) no DB yet -> not alive -> non-zero
    assert runner.invoke(app, ["status", "--healthcheck", "--config", str(cfg)]).exit_code != 0

    # (b) a FRESH heartbeat (RealClock) -> exit 0
    conn = connect(db)
    run_migrations(conn)
    Heartbeat(conn, clock=RealClock(), max_age_seconds=60).touch()
    assert runner.invoke(app, ["status", "--healthcheck", "--config", str(cfg)]).exit_code == 0

    # (c) a STALE heartbeat (written a day ago) -> non-zero
    stale_clock = _Clock(datetime.now(UTC) - timedelta(days=1))
    Heartbeat(conn, clock=stale_clock, max_age_seconds=60).touch()
    assert runner.invoke(app, ["status", "--healthcheck", "--config", str(cfg)]).exit_code != 0
