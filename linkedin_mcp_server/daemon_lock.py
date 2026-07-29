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

Handing a held lock to another process works only on POSIX. Measured on
Windows: a child that inherited the handle through the documented mechanism did
not hold the lock once the parent had closed its handles and exited, in 20 of 20
runs. The two platforms genuinely differ here, so startup has to differ with
them rather than share one path that is only true on one of them.
"""

from __future__ import annotations

import logging
import os
import weakref
from pathlib import Path
from types import TracebackType

from linkedin_mcp_server.daemon_descriptor import daemon_dir, daemon_state_root
from linkedin_mcp_server.common_utils import is_still_at
from linkedin_mcp_server.private_state import harden_directory
from linkedin_mcp_server.profile_lease import (
    acquire_locked_fd,
    open_lock_file,
    try_lock,
)

logger = logging.getLogger(__name__)

_DAEMON_LOCK_FILE = "daemon.lock"

#: Whether a held lock can be handed to another process by letting it inherit
#: the descriptor. True on POSIX, where the lock belongs to the open file
#: description that every copy shares.
#:
#: False on Windows, and this is measured rather than assumed. On a Windows
#: runner, a child that received the handle through the documented
#: ``STARTUPINFO`` handle list and kept it open did not hold the lock once the
#: parent had closed its handles and exited: a third process acquired the byte
#: range in 20 of 20 runs. Within one process a duplicate does keep the lock
#: alive, which is why this is easy to get wrong from a single-process test.
#: So on Windows the elected process has to take the lock itself rather than be
#: handed one.
#:
#: The measurement closed and exited together, so it does not say which of the
#: two released the region. That distinction would matter for a design that
#: kept the electing process alive as a lock broker; it does not matter for
#: this one, where the elector always exits.
_INHERITED_LOCKS_TRANSFER = os.name != "nt"


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

        harden_directory(daemon_state_root())
        harden_directory(daemon_dir(self._auth_root))
        fd = acquire_locked_fd(self._path, exclusive=True)
        if fd is None:
            return False

        self._fd = fd
        self._owner_pid = os.getpid()
        _held_locks.add(self)
        logger.debug("Daemon lock acquired for %s", self._auth_root)
        return True

    def release(self) -> None:
        """Give up the lock by closing our descriptor, never by unlocking it.

        Closing drops only this descriptor; the lock survives in any copy a
        owner inherited. Unlocking would release it for all of them at
        once, so an owner that had already adopted the handoff would lose
        the lock without anything saying so, and the next client would elect a
        second owner against a live browser.
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

    def inheritable_copy(self) -> int:
        """Duplicate the descriptor so a launched owner can inherit it.

        POSIX only, and refused elsewhere rather than quietly returning
        something that does not mean what it says.

        The original is opened close-on-exec, so it would not survive the exec
        that starts the owner. Handing over a duplicate rather than
        releasing and letting the owner take the lock itself is what closes
        the window in between, during which another client would see a free lock
        and elect a second owner.

        The caller closes the copy once the owner confirms it has it.
        """
        if not _INHERITED_LOCKS_TRANSFER:
            raise DaemonLockError(
                "Handing a held lock to another process is a POSIX mechanism. "
                "On this platform the owner has to take the lock itself."
            )
        if self._fd is None:
            raise DaemonLockError("Cannot hand over a lock this process does not hold")
        duplicate = os.dup(self._fd)
        try:
            os.set_inheritable(duplicate, True)
        except OSError:
            # The duplicate already shares the kernel lock, so leaking it here
            # would hold the lock for this process's whole life with nothing
            # left to release it: release() closes the original and the lock
            # survives in the copy. Measured with the call made to fail: the
            # daemon still read as running after release, and no other process
            # could take over.
            os.close(duplicate)
            raise
        return duplicate

    def adopt(self, fd: int) -> None:
        """Take ownership of an inherited locked descriptor.

        POSIX only, like its counterpart, and for the measured reason above.
        Called by an owner that was launched holding a copy.

        Adoption is the one path that would otherwise take a caller's word for
        what it was handed, so the descriptor is checked to be this lock's own
        file and then asked for the lock. Measured with neither check: a
        descriptor belonging to another auth root was adopted without complaint,
        so this process reported owning a browser nobody had elected it for
        while another process took the real lock unopposed. An unlocked
        descriptor for the right file did the same.

        Asking for the lock is not a second acquisition. A process that already
        holds it is granted it again against its own open file description, so
        a genuine handover passes untouched; a descriptor that turns out to hold
        nothing is turned into a real acquisition rather than a false claim.
        Either way exactly one process ends up holding it, which is the whole
        point of the exercise.
        """
        if not _INHERITED_LOCKS_TRANSFER:
            raise DaemonLockError(
                "An inherited lock cannot be adopted on this platform. "
                "The owner has to take the lock itself."
            )
        self._discard_if_forked()
        if self._fd is not None:
            raise DaemonLockError("This process already holds the daemon lock")

        try:
            inherited = os.fstat(fd)
        except OSError as exc:
            raise DaemonLockError(
                f"The inherited descriptor cannot be used: {exc}"
            ) from exc
        try:
            expected = self._path.stat()
        except OSError as exc:
            raise DaemonLockError(
                f"There is no lock file at {self._path} for the inherited "
                f"descriptor to belong to"
            ) from exc
        if (inherited.st_dev, inherited.st_ino) != (expected.st_dev, expected.st_ino):
            raise DaemonLockError(
                "The inherited descriptor is not this auth root's lock file. "
                "Adopting it would report ownership of a browser this process "
                "was never elected to hold."
            )
        # A descriptor for the right file that holds no lock is the same
        # mistake wearing the right name: another process would take the lock
        # while this one believed it already had it. Asking for the lock here
        # is not acquiring it twice, because on POSIX a process that already
        # holds it is granted it again against its own open file description.
        if not try_lock(fd, exclusive=True):
            raise DaemonLockError(
                "The inherited descriptor does not hold the daemon lock, so "
                "adopting it would leave the browser unowned."
            )
        # Compared again now that the lock is held, because the check above
        # happened before it. Anything that replaces the file in between leaves
        # this holding a lock on an inode the path no longer names, while the
        # live path sits free for somebody else to take, which is two owners.
        #
        # By identity rather than link count. A count catches an unlink and
        # misses a rename, where the inode keeps its one link and the name now
        # refers to something else. Measured with a rename plus recreate: the
        # count test passed and a contender locked the live path alongside the
        # adopter. ``acquire_locked_fd`` makes the same comparison and retries;
        # adoption cannot re-lock a descriptor another process opened, so it
        # refuses instead.
        if not is_still_at(fd, self._path):
            raise DaemonLockError(
                "The lock file was replaced while the inherited descriptor was "
                "being adopted, so it now locks an inode nothing refers to. "
                "Start the owner again."
            )
        # Made non-inheritable again straight away. It was marked inheritable
        # for exactly one launch, and leaving it that way would let any later
        # child that inherits descriptors, a Chromium among them, keep the lock
        # alive after this process exits. Measured: an unrelated child did
        # exactly that, and every client afterwards saw a daemon that was no
        # longer there.
        try:
            os.set_inheritable(fd, False)
        except OSError as exc:
            # The lock is held through this descriptor by now, and nothing has
            # recorded that yet, so failing here without closing it would leave
            # it held with no object able to release it: measured, a contender
            # could not take the lock afterwards and only a manual close freed
            # it. Closing gives the lock up, which is the honest outcome of an
            # adoption that did not complete.
            #
            # A failing close is logged rather than retried, and does not leave
            # the descriptor behind. Linux and the BSDs release it early in the
            # call and only then do the work that can fail, so the fd is gone
            # whatever the return says; retrying would close whichever
            # descriptor has since taken that number. POSIX.1-2024 standardised
            # the opposite for EINTR, which Linux does not implement.
            try:
                os.close(fd)
            except OSError:
                logger.debug("Closing the adopted descriptor failed", exc_info=True)
            raise DaemonLockError(
                f"The inherited descriptor could not be taken over: {exc}"
            ) from exc
        self._fd = fd
        self._owner_pid = os.getpid()
        _held_locks.add(self)
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
    path = daemon_lock_path(auth_root)
    if not path.exists():
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
