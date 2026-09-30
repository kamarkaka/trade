"""The trading lease (LR9): one process at a time may send orders or settle order rows for a
state database; released when the holder releases it (or dies)."""

import os
import stat
from pathlib import Path

import pytest

from trader.state.lease import LeaseHeldError, TradingLease


def test_only_one_holder_at_a_time(tmp_path: Path) -> None:
    daemon, operator = TradingLease(tmp_path / "s.sqlite"), TradingLease(tmp_path / "s.sqlite")
    assert daemon.try_acquire() and daemon.held
    assert not operator.try_acquire() and not operator.held  # e.g. `trader reconcile`
    daemon.release()
    assert operator.try_acquire()
    operator.release()


def test_try_acquire_is_idempotent_for_the_holder(tmp_path: Path) -> None:
    lease = TradingLease(tmp_path / "s.sqlite")
    assert lease.try_acquire() and lease.try_acquire()
    lease.release()
    lease.release()  # releasing twice is harmless
    assert not lease.held


def test_context_manager_raises_when_held(tmp_path: Path) -> None:
    held = TradingLease(tmp_path / "s.sqlite")
    with held, pytest.raises(LeaseHeldError, match="only one process"):
        TradingLease(tmp_path / "s.sqlite").acquire()
    with TradingLease(tmp_path / "s.sqlite") as lease:  # free again after exit
        assert lease.held


def test_lease_file_is_private_and_next_to_the_database(tmp_path: Path) -> None:
    lease = TradingLease(tmp_path / "s.sqlite")
    lease.acquire()
    assert lease.path == tmp_path / "s.sqlite.lease"
    assert stat.S_IMODE(os.stat(lease.path).st_mode) & 0o077 == 0
    lease.release()


def test_every_path_to_the_database_shares_one_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "s.sqlite"
    db.touch()
    link = tmp_path / "link.sqlite"
    link.symlink_to(db)
    monkeypatch.chdir(tmp_path)
    with TradingLease(db):
        assert not TradingLease(link).try_acquire()  # a symlink to the same database
        assert not TradingLease("s.sqlite").try_acquire()  # a relative path
    assert TradingLease(link).path == TradingLease(db).path


def test_a_lease_file_replaced_while_locking_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    real_flock = fcntl.flock
    lease = TradingLease(tmp_path / "s.sqlite")
    replaced = {"times": 0}

    def replace_then_lock(fd: int, op: int) -> None:
        if op & fcntl.LOCK_EX and replaced["times"] < replaced.get("limit", 1):
            replaced["times"] += 1
            lease.path.unlink()  # someone swaps the file between our open and our lock
            lease.path.touch()
        real_flock(fd, op)

    monkeypatch.setattr(fcntl, "flock", replace_then_lock)
    assert lease.try_acquire()  # the orphaned lock is dropped; the retry locks the real file
    assert replaced["times"] == 1 and os.stat(lease.path).st_ino == os.fstat(lease._fd).st_ino  # type: ignore[arg-type]
    lease.release()
    replaced.update(times=0, limit=2)  # replaced on every attempt: refuse, never hold an orphan
    assert not lease.try_acquire() and not lease.held
