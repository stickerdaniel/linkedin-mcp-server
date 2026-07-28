"""Cross-process ownership of the shared LinkedIn browser profile.

Every MCP client instance spawns its own server process, and each one would
otherwise launch its own Chromium against the same ``~/.linkedin-mcp/profile``.
Chromium does not refuse that: both browsers run, and the last one to close
overwrites the other's cookies. A live session is lost without any error.

The lease makes ownership explicit. One process holds it, keeps the browser open
across tool calls as before, and hands over when another process asks for it.

Two files under the auth root, neither ever unlinked:

``profile.lock``
    The lease itself. Held exclusively by the owner for as long as it has a
    browser open (or is mutating auth state).

``profile.handoff``
    A signal, not a lock on anything. A waiter holds a *shared* lock on it while
    it waits; the owner probes it *exclusively* and non-blocking. The probe fails
    exactly when at least one waiter is present, which is the cue to release.

Why not Chromium's own ``SingletonLock``: the default ``chrome-headless-shell``
never writes one (only full Chrome does), so it cannot see the case that matters.
:func:`linkedin_mcp_server.session_state.profile_in_use_by` still covers foreign
holders that never took this lease.

Why not the ``filelock`` package: the pinned 3.29.1 unlinks the lock path before
releasing it, which splits contenders across inodes and silently destroys mutual
exclusion. It is also not a runtime dependency.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from pathlib import Path
from types import TracebackType

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    is_still_at,
    secure_mkdir,
)

logger = logging.getLogger(__name__)

_LEASE_FILE = "profile.lock"
_HANDOFF_FILE = "profile.handoff"

# Interval between non-blocking acquisition attempts. Short enough that a handoff
# feels immediate, long enough to be free: one probe costs ~40 microseconds.
_POLL_INTERVAL_SECONDS = 0.1


class ProfileLeaseUnavailableError(RuntimeError):
    """Raised when the profile lease could not be acquired in time."""


# The single Windows error meaning "someone else holds it". Anything else,
# whether a bad handle, access denied or an invalid parameter, is a real
# failure, and reporting it as contention would leave the lease permanently
# and silently busy. Defined outside the Windows-only block so the classification can be
# tested on any platform; the branch itself only ever runs on Windows.
_ERROR_LOCK_VIOLATION = 33


def _windows_failure_is_contention(error_code: int) -> bool:
    """Whether a failed ``LockFileEx`` call means the lock is simply held."""
    return error_code == _ERROR_LOCK_VIOLATION


# --------------------------------------------------------------------------- #
# Platform backends
# --------------------------------------------------------------------------- #

try:  # POSIX
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    _HAS_FCNTL = False

if not _HAS_FCNTL:  # pragma: no cover - Windows
    try:
        import ctypes
        from ctypes import wintypes

        # Probed here rather than assumed: the block below binds kernel32 at
        # import time, and an ImportError or a missing symbol there would take
        # the whole server down with an obscure message instead of the explicit
        # "no usable file locking" refusal that try_lock raises.
        _HAS_WINDOWS_LOCKS = hasattr(ctypes, "WinDLL")
    except ImportError:
        _HAS_WINDOWS_LOCKS = False
else:  # pragma: no cover - POSIX
    _HAS_WINDOWS_LOCKS = False


#: The errnos a non-blocking lock request uses to say "somebody else holds it".
#: POSIX allows either, and they are the same value on Linux while macOS keeps
#: them distinct, so both are listed rather than assumed to coincide.
_CONTENTION_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})


def try_lock(fd: int, *, exclusive: bool) -> bool:
    """Take a non-blocking lock on *fd*. Return whether it was granted.

    ``msvcrt.locking`` is deliberately not used on Windows: it offers only
    exclusive byte-range locks (``LK_RLCK`` is documented as identical to
    ``LK_LOCK``), so it cannot express the shared waiter announcement, and its
    release on process death is only "as resources allow". ``LockFileEx`` has
    real shared/exclusive semantics and is released by the kernel.
    """
    if _HAS_FCNTL:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError as exc:
            # Only contention means "somebody else holds it". Every other errno
            # means the question could not be answered, and answering it with
            # "busy" is the worst of the two wrong answers: on a filesystem
            # without usable flock, every process would report a live owner
            # forever, no owner could ever be elected, and the message would
            # point at contention that does not exist. Measured with
            # EOPNOTSUPP: reported as busy.
            if exc.errno in _CONTENTION_ERRNOS:
                return False
            raise ProfileLeaseUnavailableError(
                f"Could not lock the profile: {exc}. This filesystem may not "
                f"support the locking that keeps two servers off one browser "
                f"profile. Move the profile to a local disk, or run only one "
                f"server process against it."
            ) from exc
        return True

    if _HAS_WINDOWS_LOCKS:  # pragma: no cover - Windows
        return _windows_try_lock(fd, exclusive=exclusive)

    raise ProfileLeaseUnavailableError(
        "This platform provides no usable file locking, so concurrent server "
        "processes cannot be prevented from corrupting the shared Chromium "
        "profile directory that holds your logged-in session. Run only one "
        "server process against this profile directory."
    )


def _unlock(fd: int) -> None:
    """Release a lock held on *fd*."""
    if _HAS_FCNTL:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            logger.debug("Unlock failed (ignored)", exc_info=True)
        return

    if _HAS_WINDOWS_LOCKS:  # pragma: no cover - Windows
        try:
            _windows_unlock(fd)
        except OSError:
            # Same tolerance as the POSIX branch above. Release runs from
            # teardown paths where the descriptor may already be closed, and
            # ``msvcrt.get_osfhandle`` raises OSError for a closed one.
            logger.debug("Unlock failed (ignored)", exc_info=True)


if not _HAS_FCNTL and _HAS_WINDOWS_LOCKS:  # pragma: no cover - Windows
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCK_BYTES = 1

    class _Overlapped(ctypes.Structure):
        # Internal/InternalHigh are ULONG_PTR, so pointer-sized on both 32- and
        # 64-bit. c_size_t matches; a plain c_ulong would not.
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    # ``WinDLL``, ``get_last_error`` and ``WinError`` exist only on Windows, so
    # they are resolved dynamically: a type checker running on another platform
    # cannot see them and would report the module as having no such member.
    _win_dll = getattr(ctypes, "WinDLL")
    _get_last_error = getattr(ctypes, "get_last_error")
    _win_error = getattr(ctypes, "WinError")

    # use_last_error keeps GetLastError from being clobbered between the call
    # and our read of it.
    _kernel32 = _win_dll("kernel32", use_last_error=True)

    # ``ctypes`` raises AttributeError for a symbol the DLL does not export, and
    # this runs at import time. Both have existed since Windows XP, so a failure
    # here means something is badly wrong, but it must surface as our own
    # refusal rather than as an unexplained import error on server startup.
    if not hasattr(_kernel32, "LockFileEx") or not hasattr(_kernel32, "UnlockFileEx"):
        raise ProfileLeaseUnavailableError(
            "kernel32 does not export LockFileEx/UnlockFileEx, so concurrent "
            "server processes cannot be prevented from corrupting the shared "
            "Chromium profile directory that holds your logged-in session. Run "
            "only one server process against this profile directory."
        )

    # Without explicit argtypes ctypes passes Python ints as C int, truncating
    # a pointer-sized HANDLE on 64-bit. Windows guarantees handle values fit in
    # 32 bits, so this happens to survive, but the prototype is still wrong,
    # and nothing about that guarantee covers the OVERLAPPED pointer.
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _lock_file_ex.restype = wintypes.BOOL

    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _unlock_file_ex.restype = wintypes.BOOL

    def _handle(fd: int) -> int:
        import msvcrt

        # Resolved dynamically: the symbol only exists on Windows, and a static
        # checker running on another platform cannot see it.
        return getattr(msvcrt, "get_osfhandle")(fd)

    def _windows_try_lock(fd: int, *, exclusive: bool) -> bool:
        flags = _LOCKFILE_FAIL_IMMEDIATELY
        if exclusive:
            flags |= _LOCKFILE_EXCLUSIVE_LOCK
        # Zero-initialised by ctypes, so the lock starts at offset 0 and hEvent
        # is NULL, which LockFileEx accepts for a synchronous call.
        overlapped = _Overlapped()
        if _lock_file_ex(
            _handle(fd), flags, 0, _LOCK_BYTES, 0, ctypes.byref(overlapped)
        ):
            return True
        error = _get_last_error()
        if _windows_failure_is_contention(error):
            return False
        cause = _win_error(error)
        raise ProfileLeaseUnavailableError(
            f"LockFileEx failed for descriptor {fd}: {cause}"
        ) from cause

    def _windows_unlock(fd: int) -> None:
        overlapped = _Overlapped()
        if not _unlock_file_ex(
            _handle(fd), 0, _LOCK_BYTES, 0, ctypes.byref(overlapped)
        ):
            raise _win_error(_get_last_error())


# --------------------------------------------------------------------------- #
# Locked file descriptors
# --------------------------------------------------------------------------- #


def open_lock_file(path: Path) -> int:
    """Open *path* for locking, creating it with owner-only permissions."""
    secure_mkdir(path.parent)
    harden_linkedin_tree(path.parent)
    # Keep the descriptor out of a launched Chromium, which would otherwise hold
    # the lease alive past our own exit. Windows has no O_CLOEXEC — os.open is
    # non-inheritable there by default — so it is looked up rather than assumed.
    cloexec = getattr(os, "O_CLOEXEC", 0)
    return os.open(path, os.O_RDWR | os.O_CREAT | cloexec, 0o600)


def acquire_locked_fd(path: Path, *, exclusive: bool) -> int | None:
    """Open *path* and lock it, or return ``None`` if it is already held.

    Re-opens when the file at the path changed between open and lock. Without
    that check two processes can hold locks on different inodes for the same
    path and each believe it holds the same logical lock.

    The identity is compared, not merely the link count. Counting links catches
    an unlink, which is the case Bazel's version of this guards against, and
    misses a rename: the inode keeps its one link, so the count still says one
    while the name now refers to something else. Measured: a rename plus
    recreate passed the count test, and a second process locked the live path
    alongside the first.
    """
    for _ in range(3):
        fd = open_lock_file(path)
        locked = False
        try:
            if not try_lock(fd, exclusive=exclusive):
                # Contention is only an answer about the file this opened. If
                # the path has moved on since, the holder is holding something
                # nobody else will consult, and reporting "busy" would refuse a
                # lock that is free: measured, this returned None while the live
                # path could be locked immediately. Retry against what is there
                # now, exactly as a successful lock on a stale inode does.
                if is_still_at(fd, path):
                    return None
                continue
            locked = True
            if is_still_at(fd, path):
                keep = fd
                fd = -1  # handed to the caller; not ours to close below
                return keep
        finally:
            # The lock backend raises for a genuine failure, such as a bad
            # handle or a platform with no locking, and the handoff probe
            # swallows that so a broken signal cannot make an owner give up its
            # browser. Without this the descriptor would escape on every poll.
            if fd != -1:
                if locked:
                    _unlock(fd)
                try:
                    os.close(fd)
                except OSError:
                    logger.debug("Closing lock descriptor failed", exc_info=True)
        # The file at the path changed while we held the lock: our inode no
        # longer protects anything anyone else will look at. Retry against
        # whatever is there now.
    raise ProfileLeaseUnavailableError(
        f"The lock file {path} keeps being replaced; refusing to continue."
    )


def _release_locked_fd(fd: int) -> None:
    # The close is in a finally so an unexpected unlock failure cannot leak the
    # descriptor. Closing it releases the lock regardless, which is the outcome
    # that matters; leaking it would hold the profile until the process exits.
    try:
        _unlock(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            logger.debug("Closing lock descriptor failed (ignored)", exc_info=True)


# --------------------------------------------------------------------------- #
# The lease
# --------------------------------------------------------------------------- #


class ProfileLease:
    """Reference-counted ownership of one auth root's browser profile.

    One instance per auth root per process, holding at most one kernel lock. The
    reference counting is what makes that safe: ``flock`` conflicts with itself
    across two open descriptions in the same process, so a middleware that locks
    and then a browser creation that locks again would deadlock the process
    against itself. Callers take :meth:`hold` handles instead; the kernel lock is
    taken on the first and released on the last.
    """

    def __init__(self, auth_root: Path) -> None:
        # Resolved so two spellings of the same directory (a symlinked TMPDIR,
        # a relative path) share one entry in the registry and therefore one
        # reference count. The kernel lock keys on the inode and would agree
        # regardless, but the in-process browser-open flag would not.
        self._auth_root = auth_root.expanduser().resolve()
        auth_root = self._auth_root
        self._lease_path = auth_root / _LEASE_FILE
        self._handoff_path = auth_root / _HANDOFF_FILE
        self._fd: int | None = None
        self._refs = 0
        self._owner_pid: int | None = None
        self._acquired_at: float | None = None
        self._browser_open = False

    # -- state ------------------------------------------------------------- #

    @property
    def auth_root(self) -> Path:
        return self._auth_root

    @property
    def held(self) -> bool:
        """Whether this process currently holds the lease.

        Runs the fork reset rather than merely reporting ``False`` for a foreign
        owner: a child that only *asked* whether it held the lease would keep the
        inherited descriptor open, and with it the parent's kernel lock.
        """
        self._reset_if_forked()
        return self._fd is not None and self._owner_pid == os.getpid()

    @property
    def held_seconds(self) -> float:
        """How long the lease has been held, or 0.0 when it is not.

        Also resets after a fork; without it a child would report the parent's
        hold time, and the min-hold window would decide against a duration this
        process never spent holding anything.
        """
        self._reset_if_forked()
        if self._acquired_at is None:
            return 0.0
        return time.monotonic() - self._acquired_at

    @property
    def browser_open(self) -> bool:
        """Whether a browser in *this* process currently uses the profile."""
        self._reset_if_forked()
        return self._browser_open

    def mark_browser_open(self) -> None:
        """Record that a browser in this process now uses the profile."""
        self._browser_open = True

    def mark_browser_closed(self) -> None:
        """Record that this process's browser has released the profile.

        Only call this once shutdown is *confirmed*. A browser whose cleanup
        timed out may still be running, and the whole point of the flag is to
        stop auth state being moved out from under it.
        """
        self._browser_open = False

    def _reset_if_forked(self) -> None:
        """Drop inherited state after a fork: the child does not own the lock."""
        if self._owner_pid is not None and self._owner_pid != os.getpid():
            logger.debug("Discarding profile lease state inherited across fork")
            inherited_fd = self._fd
            self._fd = None
            self._refs = 0
            self._owner_pid = None
            self._acquired_at = None
            self._browser_open = False
            if inherited_fd is not None:
                # Close it, do not merely forget it. fork duplicates the
                # descriptor, and the kernel lock lives as long as *any* copy is
                # open. A child that only cleared its bookkeeping would keep the
                # parent's lease alive after the parent died, locking every
                # other process out until the child happened to exit.
                # Never unlock first: that would drop the lock the parent still
                # believes it holds, both descriptions being the same one.
                try:
                    os.close(inherited_fd)
                except OSError:
                    logger.debug(
                        "Closing the inherited lock descriptor failed (ignored)",
                        exc_info=True,
                    )

    # -- acquisition ------------------------------------------------------- #

    def try_acquire(self) -> bool:
        """Take a reference, acquiring the kernel lock if this is the first."""
        self._reset_if_forked()
        if self._fd is not None:
            self._refs += 1
            return True

        fd = acquire_locked_fd(self._lease_path, exclusive=True)
        if fd is None:
            return False

        self._fd = fd
        self._refs = 1
        self._owner_pid = os.getpid()
        self._acquired_at = time.monotonic()
        logger.debug("Profile lease acquired for %s", self._auth_root)
        return True

    def release(self) -> None:
        """Drop one reference, releasing the kernel lock on the last."""
        self._reset_if_forked()
        if self._fd is None:
            return
        self._refs -= 1
        if self._refs > 0:
            return

        _release_locked_fd(self._fd)
        self._fd = None
        self._refs = 0
        self._owner_pid = None
        self._acquired_at = None
        logger.debug("Profile lease released for %s", self._auth_root)

    async def acquire(self, timeout: float) -> bool:
        """Acquire within *timeout* seconds, announcing while we wait.

        Polls rather than blocking. A blocking lock in a worker thread cannot be
        cancelled: the thread keeps waiting, acquires the lease after the caller
        already gave up, and nothing is left to release it — a permanent wedge
        from an ordinary timeout.
        """
        if self.try_acquire():
            return True

        deadline = time.monotonic() + max(timeout, 0.0)
        with self.announce():
            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                if self.try_acquire():
                    # The announcement is dropped by the context manager on the
                    # way out, before we run anything as owner. Leaving it up
                    # would make our own next probe see a waiter — ourselves —
                    # and hand the browser straight back.
                    return True
        return False

    def hold(self) -> _LeaseHandle:
        """Context manager taking a reference, raising when it cannot."""
        if not self.try_acquire():
            raise ProfileLeaseUnavailableError(
                "Another LinkedIn MCP client is currently using the browser."
            )
        return _LeaseHandle(self)

    # -- handoff ----------------------------------------------------------- #

    def announce(self) -> _Announcement:
        """Signal, for as long as the context is open, that we want the lease."""
        return _Announcement(self._handoff_path)

    def handoff_requested(self) -> bool:
        """Whether another process is waiting for the lease.

        Probes exclusively and non-blocking: it fails exactly while at least one
        waiter holds its shared lock.
        """
        try:
            fd = acquire_locked_fd(self._handoff_path, exclusive=True)
        except ProfileLeaseUnavailableError:
            # A path that keeps being replaced tells us nothing; do not hand over
            # on the strength of a broken signal.
            logger.debug("Handoff probe inconclusive", exc_info=True)
            return False
        if fd is None:
            return True
        _release_locked_fd(fd)
        return False


class _LeaseHandle:
    """One reference to a held lease, released on exit."""

    def __init__(self, lease: ProfileLease) -> None:
        self._lease = lease
        self._released = False

    def __enter__(self) -> ProfileLease:
        return self._lease

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lease.release()


class _Announcement:
    """A shared lock on the handoff file, held while a process waits."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def holds_lock(self) -> bool:
        """Whether the shared lock was actually taken.

        Announcing degrades to a no-op rather than failing a tool call, so this
        distinguishes "waiting, and the owner can see it" from "waiting quietly".
        """
        return self._fd is not None

    def __enter__(self) -> _Announcement:
        try:
            self._fd = acquire_locked_fd(self._path, exclusive=False)
        except ProfileLeaseUnavailableError:
            # Failing to announce only costs us a prompt handoff, so degrade to
            # waiting quietly rather than failing the caller's tool call.
            logger.debug("Could not announce interest in the profile lease")
            self._fd = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            _release_locked_fd(self._fd)
            self._fd = None


# --------------------------------------------------------------------------- #
# Process-wide registry
# --------------------------------------------------------------------------- #

_leases: dict[Path, ProfileLease] = {}


def _discard_inherited_leases() -> None:
    """Drop every lease the child inherited, immediately after a fork.

    Waiting for the child to touch a lease is not enough, and no amount of
    checking inside individual methods would be: a child that never mentions
    the lease still inherited the descriptor, and the kernel lock lives as long
    as any copy of it is open. Such a child silently holds its parent's lease,
    locking every other process out until it happens to exit, with nothing in
    its own state to explain why.

    Registered rather than polled, so it also covers a child that forks again.
    """
    for lease in _leases.values():
        lease._reset_if_forked()


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX
    os.register_at_fork(after_in_child=_discard_inherited_leases)


def get_profile_lease(source_profile_dir: Path | None = None) -> ProfileLease:
    """Return this process's lease for the auth root of *source_profile_dir*."""
    from linkedin_mcp_server.session_state import auth_root_dir

    auth_root = auth_root_dir(source_profile_dir).expanduser().resolve()
    lease = _leases.get(auth_root)
    if lease is None:
        lease = ProfileLease(auth_root)
        _leases[auth_root] = lease
    return lease


def reset_leases_for_testing() -> None:
    """Release every lease held by this process and forget them."""
    for lease in list(_leases.values()):
        while lease.held:
            lease.release()
    _leases.clear()
