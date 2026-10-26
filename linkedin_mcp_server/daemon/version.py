"""Whether a frontend may be served by the owner it found, version-wise.

With ``@latest`` installs an owner and a frontend can run different versions. A
newer frontend asks the owner to stand down and elects a replacement; an older
one attaches, since downgrading a shared browser for one stale client is the
worse trade. Attaching regardless would let an old owner serve every new
frontend forever, and refusing would wedge the client behind a lock it cannot
take.

Two stricter checks sit beside this. ``protocol_version`` must match before any
tool call (``daemon.discovery._inspect``). And a frontend only starts an owner
of its own build (:func:`build_identity`), which hashes the package's files
because an editable install or rebuild keeps the version.
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
    bytecode caches, with relative path and length, in path order. Not the
    installer's ``RECORD``, which for an editable install lists only a ``.pth``
    file. Read again on every call; :func:`build_identity` is the value this
    process runs.
    """
    import linkedin_mcp_server

    root = Path(linkedin_mcp_server.__file__).resolve().parent
    if not root.is_dir():
        # Imported from an archive. A walk would find nothing and give every
        # such build the digest of no files, so two different builds would
        # match. Refused, which keeps this process on Direct.
        raise NotADirectoryError(f"The package is not a directory: {root}")
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

    An unparseable version on either side is serviceable: it is a local or
    editable build, and forcing a restart on every launch would make the daemon
    useless where it is being worked on.
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
