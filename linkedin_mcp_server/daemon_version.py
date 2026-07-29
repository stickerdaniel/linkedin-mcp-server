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

``protocol_version`` stays a separate and stricter thing (``daemon_descriptor``
enforces equality on it). That is for the wire contract, where disagreement means
two processes cannot talk. This is for behaviour, where they can talk perfectly
well and one of them is simply out of date.
"""

from __future__ import annotations

import enum
import logging

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)


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
