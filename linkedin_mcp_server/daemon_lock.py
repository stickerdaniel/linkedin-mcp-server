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

* It must not unlock before closing. ``flock`` belongs to the open file
  description, which every inherited copy shares, so unlocking releases it for
  the supervisor that inherited it too. Measured: unlock-then-close left the
  lock free while the holder was alive and believed it held it.
* It must not treat a free lock as proof that nothing is running. Chromium
  outlives the process that started it, measured at over twenty seconds after a
  kill, and the kernel frees the lock at the instant of death. The two facts are
  true at the same time.
"""

from __future__ import annotations

import logging
import os
import weakref
from pathlib import Path
from types import TracebackType

from linkedin_mcp_server.private_state import harden_directory
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
#: Weak, so holding a lock here never keeps a discarded one alive.
_live_locks: weakref.WeakSet[DaemonLock] = weakref.WeakSet()


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
    for lock in list(_live_locks):
        lock._discard_if_forked()


def daemon_lock_path(auth_root: Path) -> Path:
    """Where the lock lives for *auth_root*.

    At the auth root, not under the per-profile directory, and that placement is
    load-bearing. The profile lease keys on the auth root too, because
    ``auth_root_dir`` returns the profile's parent, so two profile directories
    that sit side by side share one lease. An election scoped more narrowly than
    that would let two owners start, each convinced it had won, and then compete
    forever for a lease only one of them can hold.
    """
    return auth_root.expanduser().resolve() / _DAEMON_LOCK_FILE


class DaemonLock:
    """Exclusive ownership of one auth root's daemon, for as long as it is held.

    Not a context manager by default, because the normal case is a supervisor
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

        harden_directory(self._auth_root)
        fd = acquire_locked_fd(self._path, exclusive=True)
        if fd is None:
            return False

        self._fd = fd
        self._owner_pid = os.getpid()
        logger.debug("Daemon lock acquired for %s", self._auth_root)
        return True

    def release(self) -> None:
        """Give up the lock by closing our descriptor, never by unlocking it.

        Closing drops only this descriptor; the lock survives in any copy a
        supervisor inherited. Unlocking would release it for all of them at
        once, so a supervisor that had already adopted the handoff would lose
        the lock without anything saying so, and the next client would elect a
        second owner against a live browser.
        """
        self._discard_if_forked()
        if self._fd is None:
            return

        fd, self._fd, self._owner_pid = self._fd, None, None
        try:
            os.close(fd)
        except OSError:
            logger.debug("Closing the daemon lock failed (ignored)", exc_info=True)
        logger.debug("Daemon lock released for %s", self._auth_root)

    def inheritable_copy(self) -> int:
        """Duplicate the descriptor so a launched supervisor can inherit it.

        The original is opened close-on-exec, so it would not survive the exec
        that starts the supervisor. Handing over a duplicate rather than
        releasing and letting the supervisor take the lock itself is what closes
        the window in between, during which another client would see a free lock
        and elect a second owner.

        The caller closes the copy once the supervisor confirms it has it.

        How the copy reaches the child differs by platform, and the difference
        is not small: ``subprocess`` passes descriptors through ``pass_fds`` on
        POSIX and asserts outright that it is unsupported on Windows, where
        inheriting a handle means naming it in the startup information instead.
        Whoever launches the supervisor owns that difference; this returns a
        descriptor that is merely eligible to be inherited.
        """
        if self._fd is None:
            raise DaemonLockError("Cannot hand over a lock this process does not hold")
        duplicate = os.dup(self._fd)
        os.set_inheritable(duplicate, True)
        return duplicate

    def adopt(self, fd: int) -> None:
        """Take ownership of an inherited locked descriptor.

        Called by a supervisor that was launched holding a copy. It does not
        acquire anything: the lock is already held, and re-acquiring it against
        our own copy would fail on POSIX and mean nothing on Windows.
        """
        self._discard_if_forked()
        if self._fd is not None:
            raise DaemonLockError("This process already holds the daemon lock")
        self._fd = fd
        self._owner_pid = os.getpid()
        logger.debug("Adopted an inherited daemon lock for %s", self._auth_root)

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
    """Whether some process currently owns the daemon for *auth_root*.

    Answers the question that makes discovery cheap. A dead daemon leaves its
    descriptor file behind, and probing that file over the network costs a
    timeout on every cold start. The kernel already knows the answer for free:
    if the lock can be taken, nobody owns the daemon, so whatever the descriptor
    says is stale by definition and no connection needs attempting.

    Says nothing about whether a browser is still running. The kernel frees the
    lock the moment the holder dies, and Chromium can outlive it.
    """
    path = daemon_lock_path(auth_root)
    if not path.exists():
        return False

    # Opened and probed rather than acquired and released: taking the lock for
    # real would briefly exclude a supervisor that is starting up.
    fd = open_lock_file(path)
    try:
        if try_lock(fd, exclusive=True):
            return False
        return True
    finally:
        # Closed without unlocking, which releases whatever this descriptor
        # holds, and never touches a lock held through another one.
        try:
            os.close(fd)
        except OSError:
            logger.debug("Closing the probe descriptor failed", exc_info=True)
