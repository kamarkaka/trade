"""Tests for the fired-slot ledger: exactly-once, crash survival, status transitions,
strategy independence, and orphaned-claim recovery policy (M3.5)."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from trader.state.db import connect
from trader.state.ledger import FiredSlotLedger
from trader.state.migrate import run_migrations

DAY = date(2024, 7, 8)
FIRE = datetime(2024, 7, 8, 14, 30, tzinfo=UTC)


def _ledger(path: Path, *, now: datetime | None = None) -> FiredSlotLedger:
    conn = connect(path)
    run_migrations(conn)
    if now is None:
        return FiredSlotLedger(conn)
    return FiredSlotLedger(conn, now=lambda: now)


def _claim(ledger: FiredSlotLedger, strategy_id: str = "momentum", slot_id: str = "open") -> bool:
    return ledger.claim(DAY, strategy_id, slot_id, FIRE, drift_seconds=120, seed=42)


def test_first_claim_succeeds(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "s.sqlite")
    assert _claim(ledger) is True
    assert ledger.was_fired(DAY, "momentum", "open") == "claimed"


def test_double_claim_blocked(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "s.sqlite")
    assert _claim(ledger) is True
    assert _claim(ledger) is False  # exactly-once: second claim rejected


def test_claim_survives_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    assert _claim(_ledger(path)) is True
    # simulate a crash + restart: a fresh connection still sees the claim
    assert _claim(_ledger(path)) is False


def test_mark_done_and_failed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "s.sqlite")
    _claim(ledger, slot_id="open")
    ledger.mark_done(DAY, "momentum", "open")
    assert ledger.was_fired(DAY, "momentum", "open") == "done"

    _claim(ledger, slot_id="noon")
    ledger.mark_failed(DAY, "momentum", "noon", error="boom")
    assert ledger.was_fired(DAY, "momentum", "noon") == "failed"
    row = ledger._conn.execute("SELECT error FROM fired_slot WHERE slot_id = 'noon'").fetchone()
    assert row[0] == "boom"


def test_independent_strategies(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "s.sqlite")
    assert _claim(ledger, strategy_id="momentum") is True
    assert _claim(ledger, strategy_id="meanrev") is True  # same slot_id, different strategy


def test_orphaned_claimed_slot_recovery(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    t0 = datetime(2024, 7, 8, 14, 0, tzinfo=UTC)
    assert _claim(_ledger(path, now=t0)) is True  # claimed, then "crash" (never finished)

    # policy: the orphaned claim still BLOCKS re-fire (never auto-reopened)
    assert _claim(_ledger(path)) is False

    # and it is surfaced as stale for operator alerting once past the grace window
    later = _ledger(path, now=t0 + timedelta(minutes=30))
    stale = later.stale_claims(grace_seconds=300)
    assert len(stale) == 1
    assert stale[0].strategy_id == "momentum"
    assert later.stale_claims(grace_seconds=10_000) == []  # within grace -> not stale
