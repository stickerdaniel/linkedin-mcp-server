"""The lock that decides which process owns the shared browser.

Held for the process lifetime, unlike the reference-counted profile lease, which
is free whenever the browser is closed and so cannot decide ownership. Same
kernel primitives, different rules: one holder, no reference counting, released
only on exit. The owner takes it itself on every platform; a held lock is never
handed to another process.

Two rules, both of which lose the lock silently when broken:

* Never unlock before closing. Every duplicate of the descriptor shares the
  lock, so unlocking through any of them releases it for all, on POSIX and
  Windows alike.
* A free lock is no proof that nothing runs: the kernel frees it when the
  holder dies, while Chromium may still be shutting down. The profile lease,
  not this lock, keeps two processes off one profile.
"""

from __future__ import annotations

import contextlib
import logging
import os
import weakref
from pathlib import Path

from linkedin_mcp_server.daemon.descriptor import (
    daemon_dir,
    prepare_daemon_state,
)
from linkedin_mcp_server.common_utils import is_still_at
from linkedin_mcp_server.private_state import harden_created_file, harden_file
from linkedin_mcp_server.profile_lease import acquire_locked_fd

logger = logging.getLogger(__name__)

_DAEMON_LOCK_FILE = "daemon.lock"


class DaemonLockError(RuntimeError):
    """The daemon lock could not be used, so ownership cannot be decided."""


#: Every lock this process has built, so a fork can be cleaned up in the child.
#: Held strongly while a lock is held: a collected wrapper would leave the
#: kernel lock held with nothing left to release it. Unheld locks stay weak.
_live_locks: weakref.WeakSet[DaemonLock] = weakref.WeakSet()
_held_locks: set[DaemonLock] = set()


def _discard_inherited_locks() -> None:
    """Drop every lock the child inherited, immediately after a fork.

    A child that never touches the lock still holds a copy of its descriptor,
    which keeps the kernel lock alive after the owner exits, so no per-method
    check suffices. Registered at fork, so it also covers a child that forks
    again.
    """
    for lock in [*_live_locks, *_held_locks]:
        lock._discard_if_forked()


def daemon_lock_path(auth_root: Path) -> Path:
    """Where the lock lives for *auth_root*.

    In the private daemon directory, keyed by auth root rather than profile, so
    sibling profiles share one election and never start two owners competing
    for one profile lease.
    """
    return daemon_dir(auth_root) / _DAEMON_LOCK_FILE


def _harden_existing_lock(path: Path) -> bool:
    """Harden a pre-existing lock entry, returning whether one was present."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    harden_file(path)
    return True


class DaemonLock:
    """Exclusive ownership of one auth root's daemon, for as long as it is held.

    Not a context manager: an owner takes it at startup and holds it until
    exit.
    """

    def __init__(self, auth_root: Path) -> None:
        self._auth_root = auth_root.expanduser().resolve()
        self._path = daemon_lock_path(self._auth_root)
        self._fd: int | None = None
        self._owner_pid: int | None = None
        _live_locks.add(self)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        """Whether *this* process holds it.

        A forked child shares the kernel lock through its descriptor copy but
        none of this bookkeeping, so it discards the copy first.
        """
        self._discard_if_forked()
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock, or report that another process holds it."""
        self._discard_if_forked()
        if self._fd is not None:
            # Deliberately not reference counted: a second acquire is a caller
            # believing it is the first, and the lock's whole meaning is that
            # exactly one holder exists.
            raise DaemonLockError("This process already holds the daemon lock")

        directory = prepare_daemon_state(self._auth_root)
        self._path = directory / _DAEMON_LOCK_FILE
        existed = _harden_existing_lock(self._path)
        fd = acquire_locked_fd(self._path, exclusive=True)
        if fd is None:
            return False

        try:
            # A fresh file inherits the private directory ACL on Windows and is
            # created 0600 on POSIX. Verify that promise explicitly before using
            # it as the account's ownership record. Existing entries were checked
            # before open so links and special files never reach the lock backend.
            if not existed:
                harden_created_file(self._path)
            if not is_still_at(fd, self._path):
                raise DaemonLockError(
                    "The daemon lock file changed while its private access was "
                    "being established"
                )
        except BaseException:
            remove = not existed and is_still_at(fd, self._path)
            closed = False
            if remove and os.name == "nt":
                # Windows refuses to unlink a file this process still holds
                # open, so there the descriptor goes first and the file this
                # just created is left behind otherwise. What the removal rests
                # on is the identity check above, and that was made while the
                # descriptor still held the file. POSIX keeps the stronger
                # order, where nothing at all can come between the two.
                os.close(fd)
                closed = True
            if remove:
                with contextlib.suppress(OSError):
                    self._path.unlink()
            if not closed:
                os.close(fd)
            raise

        self._fd = fd
        self._owner_pid = os.getpid()
        _held_locks.add(self)
        logger.debug("Daemon lock acquired for %s", self._auth_root)
        return True

    def release(self) -> None:
        """Give up the lock by closing our descriptor, never by unlocking it.

        Unlocking would release it for every descriptor copy at once, and the
        next client would elect a second owner against a live browser.
        """
        self._discard_if_forked()
        if self._fd is None:
            return

        fd, self._fd, self._owner_pid = self._fd, None, None
        _held_locks.discard(self)
        try:
            os.close(fd)
        except OSError:
            logger.debug("Closing the daemon lock failed (ignored)", exc_info=True)
        logger.debug("Daemon lock released for %s", self._auth_root)

    def release_for_exit(self) -> None:
        """Release as :meth:`release` does, for a process about to ``os._exit``.

        Never logs or raises on any branch: a blocking or failing log handler
        would hold the exit after the election was given up. Closed, not
        unlocked, like :meth:`release`.
        """
        fd, self._fd, self._owner_pid = self._fd, None, None
        _held_locks.discard(self)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def _discard_if_forked(self) -> None:
        if self._owner_pid is not None and self._owner_pid != os.getpid():
            logger.debug("Discarding a daemon lock inherited across a fork")
            inherited, self._fd, self._owner_pid = self._fd, None, None
            _held_locks.discard(self)
            if inherited is not None:
                # Closed, not unlocked, for the reason release() gives: the
                # parent shares this open file description and still believes it
                # holds the lock.
                try:
                    os.close(inherited)
                except OSError:
                    logger.debug("Closing an inherited lock failed", exc_info=True)


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX
    os.register_at_fork(after_in_child=_discard_inherited_locks)
