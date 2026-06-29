"""Tests for the global cycle lock + null lock (M3.9a)."""

import threading

import pytest

from trader.orchestrator.lock import CycleLock, GlobalCycleLock, NullLock


def test_protocol_conformance() -> None:
    assert isinstance(GlobalCycleLock(), CycleLock)
    assert isinstance(NullLock(), CycleLock)


def test_context_manager_acquires_and_releases() -> None:
    lock = GlobalCycleLock()
    with lock:
        pass
    # released -> can be acquired again immediately
    assert lock.acquire(timeout=0.1) is True
    lock.release()


def test_reentrant_same_thread() -> None:
    lock = GlobalCycleLock()
    assert lock.acquire() is True
    assert lock.acquire(timeout=0.1) is True  # reentrant: same thread re-acquires
    lock.release()
    lock.release()


def test_timeout_returns_false_when_held_by_another_thread() -> None:
    lock = GlobalCycleLock()
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        lock.acquire()
        held.set()
        release.wait(timeout=2)
        lock.release()

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(timeout=2)
        assert lock.acquire(timeout=0.05) is False  # contended -> times out
    finally:
        release.set()
        t.join(timeout=2)


def test_exception_in_with_block_still_releases() -> None:
    lock = GlobalCycleLock()
    with pytest.raises(RuntimeError), lock:
        raise RuntimeError("boom")
    assert lock.acquire(timeout=0.1) is True  # released despite the exception
    lock.release()


def test_release_without_acquire_raises() -> None:
    with pytest.raises(RuntimeError):
        GlobalCycleLock().release()


def test_null_lock_is_noop() -> None:
    lock = NullLock()
    assert lock.acquire() is True
    assert lock.acquire(timeout=0.0) is True  # never blocks
    lock.release()
    with lock:
        pass
