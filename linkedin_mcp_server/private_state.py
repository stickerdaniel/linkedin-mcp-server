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

import os
import stat
from pathlib import Path

from linkedin_mcp_server.common_utils import secure_mkdir

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

    path.chmod(_PRIVATE_DIR_MODE)
    _verify_posix_mode(path, _PRIVATE_DIR_MODE)


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

    path.chmod(_PRIVATE_FILE_MODE)
    _verify_posix_mode(path, _PRIVATE_FILE_MODE)


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
