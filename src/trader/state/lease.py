"""The trading lease: one process at a time may send orders or settle order rows (LR9).

``execution.idempotency.resolve`` treats a ``pending`` order row as orphaned (its sender
died), so it must never run while another process could still be sending that order — an
operator's ``trader reconcile`` during the daemon's slow POST could otherwise re-anchor a
live send and later mark it ``not_placed``. The lease is an exclusive, non-blocking
``flock`` on ``<state db>.lease``: the daemon holds it for its lifetime and the reconcile
command must acquire it. The kernel releases it the moment the holder exits or dies, so a
crashed daemon never leaves it stuck. POSIX only (the deployment target is Linux).
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType


class LeaseHeldError(RuntimeError):
    """Another process holds the trading lease for this state database."""


class TradingLease:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(f"{db_path}.lease")
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lease if it is free; True on success (idempotent while held)."""
        if self._fd is not None:
            return True
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def acquire(self) -> None:
        if not self.try_acquire():
            raise LeaseHeldError(
                f"another trader process holds {self._path.name}: only one process may send "
                "orders or settle order rows for this state database"
            )

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> TradingLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["LeaseHeldError", "TradingLease"]
