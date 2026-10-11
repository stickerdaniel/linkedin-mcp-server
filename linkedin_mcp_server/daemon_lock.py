"""The lock that decides which process owns the shared browser.

Held for a process lifetime rather than around an operation, which is what
separates it from the profile lease next door. That difference is not
cosmetic. The lease is reference counted so a tool call, the browser singleton
and a destructive helper can each hold it at once, and it is released the moment
the browser closes so ``--login`` can take it. A lock with those properties
cannot decide ownership: it would be free every time the browser happened to be
shut, and holding it for a whole process life would lock out every login.

So this is a separate file with the same kernel primitives and different rules:
one holder, no reference counting, released only on exit.

Two things it must never do, both of which cost the lock silently:

* It must not unlock before closing. A duplicate of the descriptor shares the
  lock rather than holding one of its own, so unlocking through any of them
  releases it for all. Measured on both POSIX and Windows: unlock-then-close
  left the lock free while a live process believed it held it. The kernel
  bookkeeping behind that differs by platform, but the rule does not.
* It must not treat a free lock as proof that nothing is running. The kernel
  frees the lock at the instant of death, and Chromium can still be shutting
  down. The margin is much smaller than an earlier note here claimed: that said
  "over twenty seconds", and re-measuring found 13-38 ms on this machine and
  11-103 ms independently, because Chromium is wired to its driver by
  ``--remote-debugging-pipe`` and exits when the pipe breaks. A direct collision
  test, five rounds with a successor pre-armed and spinning, produced no round
  with two live browsers on one profile.

  The rule stands regardless of the number, because it is about which question a
  lock answers: ownership and profile-idleness are separate, and what actually
  keeps two processes off one profile is the lease next door. The old figure is
  recorded here because it was load-bearing in the design that produced this
  file, and leaving it unqualified invites conclusions the measurement does not
  support.

The owner takes the lock itself, on every platform. A held lock is never handed
to another process: measured on Windows, a child that inherited the handle did
not hold the lock once the parent had closed its handles and exited, in 20 of 20
runs.
"""

from __future__ import annotations

import contextlib
import logging
import os
import weakref
from pathlib import Path
from types import TracebackType

from linkedin_mcp_server.daemon_descriptor import (
    daemon_dir,
    prepare_daemon_state,
)
from linkedin_mcp_server.common_utils import is_still_at
from linkedin_mcp_server.private_state import harden_created_file, harden_file
from linkedin_mcp_server.profile_lease import (
    acquire_locked_fd,
    open_lock_file,
    try_lock,
)

logger = logging.getLogger(__name__)

_DAEMON_LOCK_FILE = "daemon.lock"


class DaemonLockError(RuntimeError):
    """The daemon lock could not be used, so ownership cannot be decided."""


#: Every lock this process has built, so a fork can be cleaned up in the child.
#:
#: Held strongly while a lock is *held* and weakly otherwise. A held lock owns a
#: descriptor, and the descriptor is what the kernel lock hangs on, so letting
#: the wrapper be collected would leave the lock held with nothing left to
#: release it or to close it in a forked child. Measured: after dropping the
#: last reference to a lock that had been acquired, the registry was empty and
#: another process still could not take it. Unheld locks stay weak, so building
#: one and never using it costs nothing.
_live_locks: weakref.WeakSet[DaemonLock] = weakref.WeakSet()
_held_locks: set[DaemonLock] = set()


def _discard_inherited_locks() -> None:
    """Drop every lock the child inherited, immediately after a fork.

    Waiting until the child touches a lock is not enough, and no check inside
    an individual method could be: a child that never mentions the lock still
    inherited the descriptor, and the kernel lock lives as long as any copy of
    it is open. Measured: after the owner released and exited, an untouched
    fork child kept the lock held, so every other process saw a daemon that was
    no longer there and none of them could elect a replacement.

    Registered rather than polled, so it also covers a child that forks again.
    """
    for lock in [*_live_locks, *_held_locks]:
        lock._discard_if_forked()


def daemon_lock_path(auth_root: Path) -> Path:
    """Where the lock lives for *auth_root*.

    The dedicated daemon directory is private state owned by this application,
    unlike the configurable auth root, which must keep the user's permissions.
    The path remains derived from the auth root rather than a profile directory,
    so sibling profiles still share one election and cannot start two owners that
    compete for the same profile lease.
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

    Not a context manager by default, because the normal case is a daemon owner
    that takes it at startup and holds it until it has finished cleaning up. Use
    :meth:`hold` when a scope really is the right shape, which is mainly tests
    and the one-shot maintenance commands.
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

        A fork gives the child a copy of the descriptor, and with it a share of
        the kernel lock, while none of the bookkeeping here belongs to it. Left
        alone, such a child keeps its parent's lock alive after the parent dies
        and locks every other process out with nothing to explain why.
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

        Closing drops only this descriptor; the lock survives in any copy a
        child inherited across a fork. Unlocking would release it for all of
        them at once, so the lock would be gone without anything saying so,
        and the next client would elect a second owner against a live browser.
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

        Nothing here logs or raises, on any branch. The caller exits next, and a
        log handler that blocks or fails here would hold that exit after the
        election is already given up. Closed, not unlocked, for the reason
        :meth:`release` gives; a descriptor inherited across a fork is closed
        the same way, which is all :meth:`_discard_if_forked` adds besides its
        log line.
        """
        fd, self._fd, self._owner_pid = self._fd, None, None
        _held_locks.discard(self)
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    def hold(self) -> _DaemonLockScope:
        """Take the lock for a scope, raising when another process holds it."""
        if not self.try_acquire():
            raise DaemonLockError(
                f"Another LinkedIn MCP process owns the browser for {self._auth_root}"
            )
        return _DaemonLockScope(self)

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


class _DaemonLockScope:
    def __init__(self, lock: DaemonLock) -> None:
        self._lock = lock

    def __enter__(self) -> DaemonLock:
        return self._lock

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX
    os.register_at_fork(after_in_child=_discard_inherited_locks)


def daemon_is_running(auth_root: Path) -> bool:
    """Whether some process appeared to own the daemon for *auth_root*.

    An observation, never a decision. Deciding whether to become the owner is
    what :meth:`DaemonLock.try_acquire` is for, and asking this first would be
    both slower and wrong: the answer can stop being true before the caller has
    read it, and the attempt settles the question anyway.

    The imprecision is not a shortcoming of this implementation, it is what
    ``flock`` offers. There is no way to ask whether a lock is held without
    taking one, so every probe contends with everything else touching the file.
    Both directions were measured on this tree. Probing exclusively, 43 of 400
    concurrent probes read a *sibling probe* as an owner. Probing shared fixes
    that and costs the other half: 11 of 3000 concurrent elections failed
    against a probe while no owner existed at all. Shared is the better trade,
    because a probe that briefly delays an election is recoverable and a probe
    that invents an owner is not, but neither is exact.

    So: a False here means nobody held it a moment ago, which is worth acting
    on only as a hint. A True may be an owner or may be another caller of this
    function. Tests use it to observe ownership they established themselves,
    where no such race exists.

    Says nothing about whether a browser is still running either. The kernel
    frees the lock the moment the holder dies, and Chromium can outlive it.
    """
    directory = prepare_daemon_state(auth_root)
    path = directory / _DAEMON_LOCK_FILE
    if not _harden_existing_lock(path):
        return False

    fd = open_lock_file(path)
    try:
        if try_lock(fd, exclusive=False):
            return False
        return True
    finally:
        # Closed without unlocking, which releases whatever this descriptor
        # holds, and never touches a lock held through another one.
        try:
            os.close(fd)
        except OSError:
            logger.debug("Closing the probe descriptor failed", exc_info=True)
