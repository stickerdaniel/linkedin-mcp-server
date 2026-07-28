"""Storage that only this user account can read.

The daemon work needs somewhere to keep a bearer token, and a token file that
any local account can read is the same as no token at all. POSIX already has an
answer in ``common_utils``: ``0700`` on the directory, ``0600`` on the file.
Windows does not, because ``os.chmod`` there only toggles the read-only
attribute and leaves the ACL, which is what actually decides access, untouched.

So this module owns one promise, made the same way on both platforms and
checked rather than assumed: after :func:`harden_directory` or
:func:`harden_file` returns, no other account can read the path. If that cannot
be arranged, it raises. Nothing here degrades to a warning, because the caller's
next step is to write a secret.

On Windows a normal user profile is already closed to other non-administrator
accounts, so the ACL work below is defence in depth rather than the only thing
standing in the way. It earns its place when the profile has been redirected,
when a parent directory was created with wider permissions, or when the auth
root lives somewhere outside the profile entirely. Neither platform's mechanism
keeps out root or an administrator, and no file permission ever has.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import stat
import sys
import threading
import contextlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from linkedin_mcp_server.common_utils import is_still_at, secure_mkdir

logger = logging.getLogger(__name__)

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600

_WINDOWS = os.name == "nt"
_MACOS = sys.platform == "darwin"


class PrivateStateError(RuntimeError):
    """Owner-only storage could not be established.

    Always fatal to the operation that asked for it. A caller that catches this
    and carries on is working with a path whose permissions were never
    established, and its next step is usually to write a secret there.
    """


@contextmanager
def _as_private_state_error(path: Path, action: str) -> Iterator[None]:
    """Turn a filesystem failure into this module's own error.

    Every entry point here promises to raise :class:`PrivateStateError` when it
    cannot establish owner-only storage. The operating system does not know
    that, so a file where a directory belongs, an unreadable parent, or a path
    that vanishes mid-flight arrives as NotADirectoryError, PermissionError or
    FileNotFoundError and crosses the boundary as itself. Measured: a file at
    the state root surfaced as NotADirectoryError from inside a lock
    acquisition, several layers from anything that could explain it.
    """
    try:
        yield
    except PrivateStateError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        # RuntimeError and ValueError alongside OSError: a home directory that
        # cannot be determined raises the first, an embedded NUL the second,
        # and both arrive from a path a caller was entitled to hand over.
        raise PrivateStateError(f"Could not {action} {path}: {exc}") from exc


def harden_directory(path: Path) -> None:
    """Create *path* if needed and leave it readable only by this account.

    Existing directories are hardened too, which is the case that matters:
    ``secure_mkdir`` applies its mode only to directories it creates, so a
    daemon directory inside an auth root that some earlier version or the user
    created with a default umask would otherwise keep those wider permissions
    while the token inside it looked safe.

    A link is followed rather than refused, unlike :func:`harden_file`. The
    difference is where the two get their paths. Every caller here passes one
    that ``daemon_state_root`` or ``daemon_dir`` already resolved, so a link is
    part of the layout the user chose, and a home reached through one is an
    ordinary arrangement. A file name, by contrast, is built from an identifier
    and lands in a directory this hardens first.
    """
    with _as_private_state_error(path, "prepare the private directory"):
        # An unexpanded tilde means expanduser could not resolve it, which
        # happens for a user this system does not know. Left alone it becomes a
        # directory literally named "~something" wherever the process happens
        # to be running: measured, one appeared in the working directory. A
        # path that was meant to be somewhere else is not somewhere to keep a
        # secret.
        if str(path).startswith("~"):
            raise PrivateStateError(
                f"{path} still begins with a tilde, so the home directory it "
                f"names could not be resolved"
            )

        if path.exists() and not path.is_dir():
            raise PrivateStateError(f"Not a directory: {path}")

        secure_mkdir(path, mode=_PRIVATE_DIR_MODE)

        if _WINDOWS:
            from linkedin_mcp_server.windows_acl import restrict_to_current_user

            restrict_to_current_user(path, directory=True)
            return

        _harden_posix(path, _PRIVATE_DIR_MODE)


def harden_file(path: Path) -> None:
    """Leave the existing file *path* readable only by this account.

    The file has to exist already, so a caller writing a secret creates it with
    owner-only permissions itself and calls this to *establish* that rather
    than assume it: the mode, the owner and the access list are read back, and
    on Windows the access list is replaced outright. A refusal therefore means
    the permissions could not be confirmed, not that the file is exposed.
    """
    with _as_private_state_error(path, "inspect"):
        if not path.exists():
            raise PrivateStateError(f"Cannot harden a file that does not exist: {path}")

        # Before the platform split, because both halves get it wrong in their own
        # way. POSIX opens a directory with O_RDONLY quite happily and would take it
        # from 0700 to 0600, which stops it being searchable; Windows would give it
        # a file's non-inheritable access list, so files created inside afterwards
        # no longer inherit the owner-only entry that is the point of hardening the
        # directory. Neither is something a caller should discover later.
        #
        # lstat, so a link is judged as itself rather than as its target. POSIX
        # refuses one again at open time through O_NOFOLLOW; Windows has no
        # equivalent, so this is where both learn about it, and a link is worth its
        # own message because it is the case a caller is most likely to hit.
        entry = path.lstat()
    if stat.S_ISLNK(entry.st_mode):
        raise PrivateStateError(
            f"{path} is a symbolic link rather than a file. Hardening it would "
            f"change permissions somewhere else and say nothing about this path."
        )
    if not stat.S_ISREG(entry.st_mode):
        raise PrivateStateError(f"{path} is not a regular file")

    if _WINDOWS:
        from linkedin_mcp_server.windows_acl import restrict_to_current_user

        restrict_to_current_user(path, directory=False)
        return

    # Opened once, then hardened through the descriptor. Checking the path for
    # a link and then calling chmod on that same path resolves it twice, and
    # anything that swaps the file in between is followed by the second
    # resolution: measured, an unrelated file became 0600 while this reported
    # the token as private. O_NOFOLLOW refuses the link at open time, and every
    # step afterwards names the descriptor, so there is no second lookup to
    # race. The only caller writes into a directory hardened to 0700 first, so
    # nothing else can plant the link there, but a promise this specific should
    # not rest on where it happens to be called from.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise PrivateStateError(
                f"{path} is a symbolic link rather than a file. Hardening it "
                f"would change permissions somewhere else and say nothing "
                f"about this path."
            ) from exc
        raise PrivateStateError(f"{path} could not be opened to harden: {exc}") from exc

    try:
        # The work is inside the translation, not just the path checks before
        # it: fstat, fchmod and the access list calls each fail with an OSError
        # of their own. Measured with fchmod made to answer EIO: it crossed the
        # boundary as itself. The close below is the exception, and
        # deliberately: it runs in a finally, where raising would replace
        # whatever sent us there with a less useful error.
        with _as_private_state_error(path, "make private"):
            # Asked again of the descriptor, not the name. The check above was
            # made before the open, so it says what the path was then; this
            # says what was actually opened, which is what the next few calls
            # will change.
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise PrivateStateError(f"{path} is not a regular file")

            _harden_posix_fd(fd, path, _PRIVATE_FILE_MODE)

            # The descriptor is provably private now, but the promise is about
            # the path, and the caller reaches this file by name. If the name
            # has come to mean something else in the meantime, hardening the
            # old inode says nothing about the file that answers to it.
            # Measured: this returned success while the path was already 0644
            # again.
            if not is_still_at(fd, path):
                raise PrivateStateError(
                    f"{path} was replaced while it was being hardened, so it "
                    f"no longer refers to the file this made private."
                )
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _require_acl_support(path: Path) -> None:
    """Refuse to promise owner-only where access lists cannot be read at all.

    macOS puts an access list alongside the mode on every filesystem this runs
    on, and the functions that reach it are part of its libc. Not finding them
    there means something is wrong with the interpreter or the library, not that
    the platform has no access lists, and treating it as the latter is what made
    this fail open. Measured with the bindings absent: a directory carrying an
    inherited ``everyone`` entry came back from hardening unchanged, and the
    call reported success. Elsewhere a missing binding genuinely does mean there
    is no list to clear.
    """
    if _MACOS and _libc() is None:
        raise PrivateStateError(
            f"The access list functions are missing from this system's C "
            f"library, so it cannot be established that only this account can "
            f"read {path}. Refusing to treat it as private."
        )


def _harden_posix(path: Path, mode: int) -> None:
    """Apply *mode*, drop any extended ACL, and check the result.

    By path, which is right for a directory: the caller passes one that has
    already been resolved, and a directory reached through a link is an ordinary
    layout. A file goes through :func:`_harden_posix_fd` instead, because its
    name is built rather than resolved and a second lookup could be aimed
    elsewhere in between.
    """
    _require_acl_support(path)
    path.chmod(mode)
    _drop_extended_acl(path)
    _verify_posix_owner(path.stat(), path)
    _verify_posix_mode(path.stat(), mode, path)
    _verify_no_extended_acl(path)


def _harden_posix_fd(fd: int, path: Path, mode: int) -> None:
    """The same, applied and verified through an open descriptor.

    Every step names *fd* rather than the path, so nothing between them can be
    redirected by swapping what the name refers to. *path* is carried only to
    say where a failure happened.
    """
    _require_acl_support(path)
    os.fchmod(fd, mode)
    _drop_extended_acl_fd(fd, path)
    _verify_posix_owner(os.fstat(fd), path)
    _verify_posix_mode(os.fstat(fd), mode, path)
    _verify_no_extended_acl_fd(fd, path)


def _verify_posix_owner(info: os.stat_result, path: Path) -> None:
    """Refuse a path owned by another account.

    ``0600`` says "only the owner", which is worth nothing when the owner is
    somebody else: those bits are then granting *them* the access, and an owner
    can chmod their own path back open whenever they like. The mode alone
    cannot distinguish the two cases.

    It takes a privileged process to get here, since chmod on another account's
    path fails otherwise, and a container or service started against a
    pre-existing state tree is exactly that. The Windows side already reads its
    owner back for the same reason; this is the POSIX half of that promise.
    """
    if info.st_uid != os.geteuid():
        raise PrivateStateError(
            f"{path} is owned by uid {info.st_uid}, not by this account (uid "
            f"{os.geteuid()}), so its permission bits grant that account rather "
            f"than this one and it can widen them again at any time"
        )


#: macOS ``acl_get_file``/``acl_set_file`` selector for the list that sits
#: alongside the mode bits.
_ACL_TYPE_EXTENDED = 0x00000100

#: The errno values that mean "this path carries no extended access list"
#: rather than "the question could not be answered". ENOENT is what macOS
#: returns for a perfectly ordinary directory without one, measured. The other
#: two are how a filesystem says it does not implement access lists at all,
#: which is equally an answer: there is no list to worry about.
_NO_EXTENDED_ACL_ERRNOS = frozenset(
    {errno.ENOENT, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
)

#: Resolved once, on first use. Two variables rather than a sentinel value, so
#: "not looked up yet" and "looked up and unavailable" stay distinguishable
#: without a type the caller has to narrow.
_libc_resolved = False
_libc_cache: ctypes.CDLL | None = None
# Reentrant, because loading a library raises a ctypes.dlopen audit event and
# a hook on it runs synchronously, inside this lock. A hook that reached back
# into hardening would deadlock the resolving thread against itself on a plain
# lock. Measured: one that re-entered left the thread waiting indefinitely.
# Nothing in this project installs such a hook, but an embedding process or a
# security monitor can, and the cost of allowing for it is one word.
_libc_lock = threading.RLock()


def _libc() -> ctypes.CDLL | None:
    """Return libc with the ACL calls bound, or None where they do not exist.

    Through libc rather than by running ``chmod`` and reading ``ls``. Shelling
    out made this fail open: measured with no usable ``PATH``, hardening
    reported success while the token stayed readable by ``everyone``, because
    neither the clearing nor the check could run and both treated that as
    nothing to report. A missing library is a fact this can establish; a
    missing executable was not.
    """
    global _libc_cache, _libc_resolved
    # Under the lock for the whole resolution, and the flag is set last.
    # Published early, the flag would tell a concurrent caller the lookup was
    # finished while the cache was still empty, and an empty cache reads as
    # "this platform has no access lists" everywhere below. Measured: a second
    # thread entering that window hardened a directory that kept an inherited
    # everyone entry, and the call reported success.
    with _libc_lock:
        if _libc_resolved:
            return _libc_cache

        library: ctypes.CDLL | None = None
        try:
            name = ctypes.util.find_library("c")
            if name is not None:
                candidate = ctypes.CDLL(name, use_errno=True)
                if hasattr(candidate, "acl_get_file") and hasattr(
                    candidate, "acl_set_file"
                ):
                    candidate.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_uint]
                    candidate.acl_get_file.restype = ctypes.c_void_p
                    candidate.acl_set_file.argtypes = [
                        ctypes.c_char_p,
                        ctypes.c_uint,
                        ctypes.c_void_p,
                    ]
                    candidate.acl_set_file.restype = ctypes.c_int
                    candidate.acl_init.argtypes = [ctypes.c_int]
                    candidate.acl_init.restype = ctypes.c_void_p
                    candidate.acl_free.argtypes = [ctypes.c_void_p]
                    candidate.acl_free.restype = ctypes.c_int
                    # The descriptor forms, used where a second lookup of the
                    # name would be a second chance to redirect it.
                    candidate.acl_get_fd.argtypes = [ctypes.c_int]
                    candidate.acl_get_fd.restype = ctypes.c_void_p
                    candidate.acl_set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p]
                    candidate.acl_set_fd.restype = ctypes.c_int
                    library = candidate
        except OSError:
            # A failure to load is not an answer about the platform, so it is
            # not cached as one: the next caller looks again rather than living
            # with a verdict reached while something was momentarily wrong.
            logger.debug("Could not load the access list functions", exc_info=True)
            raise

        _libc_cache = library
        _libc_resolved = True
        return _libc_cache


def _has_extended_acl(path: Path) -> bool:
    """Whether *path* carries an access list beyond its permission bits.

    ``acl_get_file`` returns NULL for *any* failure and sets errno to say
    which, so NULL alone does not mean the path carries no list. Measured on
    macOS: a directory with no extended ACL returns NULL with ENOENT, which is
    the answer this wants. Anything else is the call failing to answer, and
    reading that as "no list" would report a path as owner-only precisely when
    its permissions could not be established.
    """
    return _has_extended_acl_via(
        lambda library: library.acl_get_file(str(path).encode(), _ACL_TYPE_EXTENDED),
        path,
    )


def _has_extended_acl_fd(fd: int, path: Path) -> bool:
    """The same question, asked of an open descriptor."""
    return _has_extended_acl_via(lambda library: library.acl_get_fd(fd), path)


def _has_extended_acl_via(
    read: Callable[[ctypes.CDLL], int | None], path: Path
) -> bool:
    library = _libc()
    if library is None:  # pragma: no cover - no ACL support to report on
        return False
    # Cleared first: errno keeps whatever an earlier call left there, so a
    # stale value would be read as this call's outcome.
    ctypes.set_errno(0)
    handle = read(library)
    if not handle:
        code = ctypes.get_errno()
        if code in _NO_EXTENDED_ACL_ERRNOS:
            return False
        raise PrivateStateError(
            f"Could not read the access list on {path}: {os.strerror(code)}. "
            f"Refusing to report it as owner-only without having checked."
        )
    library.acl_free(handle)
    return True


def _drop_extended_acl(path: Path) -> None:
    """Remove an extended ACL, which the mode bits do not describe.

    On macOS an ACL sits alongside the mode rather than inside it, and
    ``chmod`` leaves it untouched. Measured: an auth root carrying an
    inheritable ``everyone`` entry produced a token file reporting exactly
    ``0600`` while ``everyone`` could still read it. Verifying only the mode
    is therefore not verifying anything about who has access.

    Linux ACLs behave the same way in principle, but ``chmod`` there already
    clamps the mask so the extra entries grant nothing.
    """
    _drop_extended_acl_via(
        lambda library, empty: library.acl_set_file(
            str(path).encode(), _ACL_TYPE_EXTENDED, empty
        ),
        path,
    )


def _drop_extended_acl_fd(fd: int, path: Path) -> None:
    """The same, applied to an open descriptor."""
    _drop_extended_acl_via(
        lambda library, empty: library.acl_set_fd(fd, empty),
        path,
    )


def _drop_extended_acl_via(
    apply_empty: Callable[[ctypes.CDLL, int], int], path: Path
) -> None:
    library = _libc()
    if library is None:  # pragma: no cover - nothing to clear
        return

    empty = library.acl_init(0)
    if not empty:
        raise PrivateStateError(
            f"Could not build an empty access list to apply to {path}"
        )
    try:
        if apply_empty(library, empty) != 0:
            # Not swallowed. The verification below would catch a list that is
            # still there, but a failure here is worth reporting on its own
            # terms rather than as a mysterious refusal one step later.
            raise PrivateStateError(
                f"Could not clear the access list on {path}: "
                f"{os.strerror(ctypes.get_errno())}"
            )
    finally:
        library.acl_free(empty)


def _verify_no_extended_acl(path: Path) -> None:
    """Refuse when an extended ACL still grants access the mode does not show."""
    if _has_extended_acl(path):
        raise PrivateStateError(_EXTENDED_ACL_REMAINS.format(path=path))


def _verify_no_extended_acl_fd(fd: int, path: Path) -> None:
    """The same check, made through the descriptor that was hardened."""
    if _has_extended_acl_fd(fd, path):
        raise PrivateStateError(_EXTENDED_ACL_REMAINS.format(path=path))


_EXTENDED_ACL_REMAINS = (
    "{path} carries an access control list that the permission bits do not "
    "describe, so it may still be readable by other accounts. Clear it with: "
    "chmod -N {path}"
)


def _verify_posix_mode(info: os.stat_result, expected: int, path: Path) -> None:
    """Read the mode back, because chmod can silently do nothing.

    A filesystem that does not carry POSIX permission bits, such as a mounted
    share or a FAT volume, accepts the call and keeps the old mode. Trusting the
    call to have worked is exactly how a secret ends up world-readable.
    """
    actual = stat.S_IMODE(info.st_mode)
    if actual != expected:
        raise PrivateStateError(
            f"{path} could not be made owner-only: asked for {expected:04o}, "
            f"the filesystem reports {actual:04o}. It may not support Unix "
            f"permissions. Move the LinkedIn MCP data directory to a local "
            f"disk."
        )
