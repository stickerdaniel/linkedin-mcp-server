"""Whether a frontend may be served by the owner it found, version-wise.

``@latest`` is the documented install (README.md), so a machine can easily hold
two versions at once: a long-lived owner from last week and a frontend that was
downloaded a minute ago. Something has to give, and both obvious answers are
wrong on their own.

*Attach anyway* is what the shipped code did, because ``package_version`` was
published and never compared. A months-old owner then serves its own tools to
every new frontend indefinitely, which is exactly the "a pinned version quietly
rots" failure the README warns users about — except invisible, because the user
did update.

*Refuse* wedges the client. A frontend that declines an incompatible owner and
stops has no way forward: it cannot take the lock, so it cannot start a
replacement, and the owner it refused will still be there next time.

So the rule is neither. A **newer** frontend asks the owner to stand down and
elects a replacement; an **older** one attaches, because the owner it found is
at least as new as it is and downgrading a shared browser to satisfy one stale
client would be the worse trade. Same version, obviously, attaches.

``protocol_version`` stays a separate and stricter thing (``daemon.discovery._inspect``
requires equality on it before any tool call, and a mismatch is control only).
That is for the wire contract, where disagreement means two processes cannot
share tools. This is for behaviour, where they can talk perfectly well and one of
them is simply out of date.

Starting an owner is stricter again, and :func:`build_identity` is what it
compares. A frontend only ever starts an owner of its own build: the owner it
spawns runs whatever is installed *now*, and a package replaced under a running
frontend (``pip install -U``, an edited checkout) would otherwise hand the
browser to code the frontend has never seen. The package version alone cannot
tell, because an editable install or a rebuild keeps it, so the identity also
hashes the package's files.
"""

from __future__ import annotations

import enum
import functools
import hashlib
import logging
import struct
from importlib import metadata
from pathlib import Path

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

#: The names this package has been published under, in the order
#: ``linkedin_mcp_server.__version__`` looks them up.
_DISTRIBUTIONS = ("mcp-server-linkedin", "linkedin-scraper-mcp")


def _distribution() -> tuple[str, str]:
    for name in _DISTRIBUTIONS:
        try:
            return name, metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return "unpackaged", "0.0.0.dev"


def installed_build_identity() -> str:
    """Name the build on disk right now: distribution, version, file digest.

    The digest covers every file under the imported package directory except
    bytecode caches, each with its relative path and length, in path order. Not
    the installer's ``RECORD``: an editable install's record lists a ``.pth``
    file and nothing it points to, so an edited source tree keeps it unchanged.
    Read again on every call, which is the point; see :func:`build_identity` for
    the value this process runs.
    """
    import linkedin_mcp_server

    root = Path(linkedin_mcp_server.__file__).resolve().parent
    files = sorted(
        (path.relative_to(root).as_posix(), path)
        for path in root.rglob("*")
        if "__pycache__" not in path.relative_to(root).parts and path.is_file()
    )
    digest = hashlib.sha256()
    for name, path in files:
        encoded = name.encode("utf-8", "surrogatepass")
        content = path.read_bytes()
        digest.update(struct.pack("!I", len(encoded)) + encoded)
        digest.update(struct.pack("!Q", len(content)) + content)
    distribution, version = _distribution()
    return f"{distribution}:{version}:sha256:{digest.hexdigest()}"


@functools.cache
def build_identity() -> str:
    """The build this process runs, frozen the first time it is asked.

    A frontend asks at its first election, which is part of its own startup, so
    a later replacement on disk shows up as a difference against this value
    rather than moving it. An owner asks when it answers the frontend's HELLO.
    """
    return installed_build_identity()


class Skew(enum.Enum):
    """How a published owner's version relates to this frontend's."""

    #: Same version, or the owner is newer. Attach.
    SERVICEABLE = "serviceable"

    #: This frontend is newer. Ask the owner to stand down, then elect.
    OWNER_IS_STALE = "owner_is_stale"


def compare(*, owner: str, frontend: str) -> Skew:
    """Say whether *frontend* should be served by an owner running *owner*.

    An unparseable version on either side is treated as serviceable. Both come
    from installed package metadata, so a value neither ``packaging`` nor this
    understands is a local build or an editable install rather than a skew, and
    turning that into a forced restart on every single launch would make the
    daemon useless exactly where it is being worked on.
    """
    try:
        published, ours = Version(owner), Version(frontend)
    except InvalidVersion:
        logger.debug(
            "Cannot compare daemon versions (%s against %s); attaching", owner, frontend
        )
        return Skew.SERVICEABLE

    if ours > published:
        return Skew.OWNER_IS_STALE
    return Skew.SERVICEABLE
