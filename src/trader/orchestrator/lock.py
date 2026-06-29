"""Global cycle lock (design §7.5, Appendix C #2).

One process-wide lock serializes the entire decision->sizing->submit critical section
so two overlapping fires never read-modify-write account state on stale balances. It
is reentrant (same thread may re-acquire) and usable as a context manager. ``NullLock``
is a no-op double for single-threaded contexts/tests.
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Protocol, runtime_checkable


@runtime_checkable
class CycleLock(Protocol):
    """The lock contract the orchestrator depends on."""

    def acquire(self, timeout: float | None = None) -> bool: ...

    def release(self) -> None: ...

    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class GlobalCycleLock:
    """A reentrant, process-wide cycle lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until acquired, or wait at most ``timeout`` seconds (False on timeout)."""
        if timeout is None:
            return self._lock.acquire()
        return self._lock.acquire(timeout=timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> GlobalCycleLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


class NullLock:
    """A no-op lock for single-threaded tests/backtests (no serialization needed)."""

    def acquire(self, timeout: float | None = None) -> bool:
        return True

    def release(self) -> None:
        return None

    def __enter__(self) -> NullLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None
