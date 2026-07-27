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
import logging
import os
import stat
import threading
from pathlib import Path

from linkedin_mcp_server.common_utils import secure_mkdir

logger = logging.getLogger(__name__)

_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600

_WINDOWS = os.name == "nt"


class PrivateStateError(RuntimeError):
    """Owner-only storage could not be established, so nothing was written.

    Always fatal to the operation that asked for it. A caller that catches this
    and continues has written a secret somewhere it did not verify.
    """


def harden_directory(path: Path) -> None:
    """Create *path* if needed and leave it readable only by this account.

    Existing directories are hardened too, which is the case that matters:
    ``secure_mkdir`` applies its mode only to directories it creates, so a
    daemon directory inside an auth root that some earlier version or the user
    created with a default umask would otherwise keep those wider permissions
    while the token inside it looked safe.
    """
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

    Harden the file before writing the secret into it, not after: the window in
    between is a window in which the secret is on disk under whatever
    permissions it was created with.
    """
    if not path.exists():
        raise PrivateStateError(f"Cannot harden a file that does not exist: {path}")

    if _WINDOWS:
        from linkedin_mcp_server.windows_acl import restrict_to_current_user

        restrict_to_current_user(path, directory=False)
        return

    _harden_posix(path, _PRIVATE_FILE_MODE)


def _harden_posix(path: Path, mode: int) -> None:
    """Apply *mode*, drop any extended ACL, and check the result."""
    path.chmod(mode)
    _drop_extended_acl(path)
    _verify_posix_mode(path, mode)
    _verify_no_extended_acl(path)


#: macOS ``acl_get_file``/``acl_set_file`` selector for the list that sits
#: alongside the mode bits.
_ACL_TYPE_EXTENDED = 0x00000100

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
    """Whether *path* carries an access list beyond its permission bits."""
    library = _libc()
    if library is None:  # pragma: no cover - no ACL support to report on
        return False
    handle = library.acl_get_file(str(path).encode(), _ACL_TYPE_EXTENDED)
    if not handle:
        return False
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
    library = _libc()
    if library is None:  # pragma: no cover - nothing to clear
        return

    empty = library.acl_init(0)
    if not empty:
        raise PrivateStateError(
            f"Could not build an empty access list to apply to {path}"
        )
    try:
        if library.acl_set_file(str(path).encode(), _ACL_TYPE_EXTENDED, empty) != 0:
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
        raise PrivateStateError(
            f"{path} carries an access control list that the permission bits "
            f"do not describe, so it may still be readable by other accounts. "
            f"Clear it with: chmod -N {path}"
        )


def _verify_posix_mode(path: Path, expected: int) -> None:
    """Read the mode back, because chmod can silently do nothing.

    A filesystem that does not carry POSIX permission bits, such as a mounted
    share or a FAT volume, accepts the call and keeps the old mode. Trusting the
    call to have worked is exactly how a secret ends up world-readable.
    """
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != expected:
        raise PrivateStateError(
            f"{path} could not be made owner-only: asked for {expected:04o}, "
            f"the filesystem reports {actual:04o}. It may not support Unix "
            f"permissions. Move the LinkedIn MCP data directory to a local "
            f"disk."
        )
