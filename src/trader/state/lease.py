"""The trading lease: one process at a time may send orders or settle order rows (LR9).

``execution.idempotency.resolve`` treats a ``pending`` order row as orphaned (its sender
died), so it must never run while another process could still be sending that order — an
operator's ``trader reconcile`` during the daemon's slow POST could otherwise re-anchor a
live send and later mark it ``not_placed``. The lease is an exclusive, non-blocking
``flock`` on ``<state db>.lease``: the daemon holds it for its lifetime and the reconcile
command must acquire it. The kernel releases it the moment the holder exits or dies, so a
crashed daemon never leaves it stuck. POSIX only (the deployment target is Linux).

Limits:

- **Never delete the lease file.** It is not a stale lock — a file nobody holds is harmless —
  and deleting it while it is held lets a second process lock a new file of the same name.
  (Acquiring re-checks that the file it locked is still the one on disk, which covers a
  file replaced between open and lock, not one deleted from under a holder.)
- **It guards one state database path, not a Schwab account.** The path is resolved, so a
  symlink or relative path to the same file shares the lease, but a copy of the database or
  a second config against the same account does not. Run one live config (and one state
  database) per account.
- It needs a filesystem with working ``flock`` (local disks and Docker named volumes; not
  NFS mounted without locking).
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
        # Resolved, so every path to the same database file shares one lease.
        self._path = Path(f"{Path(db_path).resolve()}.lease")
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
        for _ in range(2):  # once more if the file was replaced while we locked it
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            if self._names_path(fd):
                self._fd = fd
                return True
            os.close(fd)  # locked an orphaned file (and releases it): nobody contends there
        return False

    def _names_path(self, fd: int) -> bool:
        """Whether ``fd`` is still the file at the lease path (not deleted or replaced)."""
        try:
            on_disk = os.stat(self._path)
        except FileNotFoundError:
            return False
        held = os.fstat(fd)
        return (held.st_dev, held.st_ino) == (on_disk.st_dev, on_disk.st_ino)

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
