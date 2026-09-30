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
