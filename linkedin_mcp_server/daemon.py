"""Decide whether this process attaches to a daemon, owns one, or goes it alone.

A stdio server is spawned per MCP client, and #598 made several of them safe on
one profile by passing the browser between them. Safe, but not cheap: ownership
moves on nearly every call, and every move reopens Chromium and revalidates the
session against ``/feed/``. One long-lived owner removes that traffic entirely.

This module is only the discovery half: it answers "is there an owner I should
be talking to, and may I trust it with a token". Electing an owner, forwarding
to it and supervising it build on this and land with the code that forwards.

Finding an owner takes a wait rather than a single read, because the interesting
case is neither "an owner exists" nor "none does", but the window in between. An
owner takes the lock first and publishes its descriptor only once it is actually
listening, so a client arriving in that gap sees no descriptor *and* loses the
lock race. Reading that as "no daemon" is the one wrong answer: the process
would drive its own browser against the same profile for its whole life, which
is exactly the per-call handoff this feature exists to remove, and it is what
two clients starting together would normally hit.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import DaemonDescriptor, DescriptorError
from linkedin_mcp_server.session_state import get_runtime_id

logger = logging.getLogger(__name__)

#: How long to keep re-reading while an owner is starting up. It covers the gap
#: between a lock being taken and the descriptor being published, which is a
#: bind plus a server start, so this is generous rather than tuned. Waiting too
#: long costs a slow first call; giving up too early costs a redundant browser
#: for the life of the process.
_ATTACH_WAIT_SECONDS = 10.0

#: Re-read this often while waiting. Short enough that the common case (an owner
#: that is nearly ready) does not feel like a stall.
_ATTACH_POLL_SECONDS = 0.1


class OwnerState(enum.Enum):
    """What the published descriptor says, in the terms a caller must act on.

    Strictly a reading of one file. None of these states knows whether the
    process that wrote it still exists, which is why none of them decides who
    owns the browser; they decide whether there is someone to *talk* to, and
    what to say when there is not.
    """

    #: Nothing published. The ordinary first start.
    ABSENT = "absent"

    #: Published and usable. Attach.
    ATTACHABLE = "attachable"

    #: A descriptor we may not use: it names a different profile, runtime, or
    #: configuration. Says nothing about whether that daemon is still running.
    #: A crashed owner leaves a perfectly valid descriptor behind, so this is
    #: "not for us", never "in use".
    INCOMPATIBLE = "incompatible"

    #: A descriptor exists that cannot be trusted. Distinct from absence
    #: because it must not be *deleted* or explained away: beside a held lock
    #: it is a live daemon this client cannot talk to. Whether the position is
    #: actually free is still the lock's answer, not this one.
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class Attachment:
    """An owner worth talking to, and the credential for doing so."""

    descriptor: DaemonDescriptor
    #: Kept out of the repr, as the proxy credentials are
    #: (``config/schema.py:116-117``). This is a bearer token for a server that
    #: drives a logged-in LinkedIn session, and the surrounding code logs whole
    #: objects at DEBUG while users paste those logs into issue reports.
    token: str = field(repr=False)


@dataclass(frozen=True)
class OwnerLookup:
    """The verdict, and the attachment when there is one."""

    state: OwnerState
    attachment: Attachment | None = None
    #: Why, in words worth logging. Never carries a token or a config value.
    reason: str = ""

    @property
    def worth_attempting_election(self) -> bool:
        """Whether to try the lock: true whenever there is nothing to attach to.

        Every state other than :attr:`OwnerState.ATTACHABLE` gets an attempt,
        because none of them can tell whether the position is actually free.
        The descriptor and the lock are separate artifacts that go out of step
        in both directions, and each case below was reproduced on this tree:

        * lock held, nothing published yet — the ordinary startup window. Reads
          as ``ABSENT``, so a "free" reading here would start a second browser.
        * lock free, a corrupt descriptor left by a crashed owner. Reads as
          ``UNTRUSTED`` while ``try_acquire`` succeeds.
        * lock free, a *valid* descriptor left by a crashed owner that served a
          sibling profile. Reads as ``INCOMPATIBLE`` while ``try_acquire``
          succeeds. Nothing about a descriptor says its writer is alive.

        Refusing on any of those readings strands the profile until someone
        deletes a file by hand. So this says only that there is no owner to
        talk to; :meth:`DaemonLock.try_acquire` settles who owns, which is what
        its own docstring says of the matching probe
        (``daemon_lock.py:382-407``). The state is the explanation for a failed
        attempt, never permission granted before one.
        """
        return self.state is not OwnerState.ATTACHABLE


def _inspect(auth_root: Path, profile: Path, config: AppConfig) -> OwnerLookup:
    """Read the descriptor once and say what it means.

    Order matters. The cheap, local checks come first, so a descriptor that
    already disqualifies itself is rejected before the token is read.
    """
    descriptor = daemon_descriptor.read(auth_root)
    if descriptor is None:
        return OwnerLookup(state=OwnerState.ABSENT, reason="no daemon is published")

    # Not enforced by read(), deliberately: it is the caller who knows which
    # runtime it belongs to. A host attaching to a container's daemon would be
    # handed a browser in a different filesystem namespace.
    if descriptor.runtime_id != get_runtime_id():
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            reason="the published daemon belongs to another runtime",
        )

    # The lock is per auth root, but a profile is what a browser opens. Two
    # profiles side by side elect one owner between them, so a published owner
    # can be one this client may not use.
    if not descriptor.serves(profile):
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            reason="the published daemon serves a different profile",
        )

    # No endpoint check here: `from_mapping` already refuses a non-local host
    # while parsing, so `read` above cannot return one. Repeating it would read
    # as the safeguard when it is really a second copy of one, and a second
    # copy is what rots when the first moves.
    token = daemon_descriptor.read_token(auth_root, descriptor)

    # Keyed with the token, so this can only run once the token is in hand.
    if descriptor.config_fingerprint != daemon_descriptor.config_fingerprint(
        config, key=token
    ):
        return OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            # Names no values: the shared fields include a proxy password and
            # the path to someone's profile.
            reason="the published daemon uses a different configuration",
        )

    return OwnerLookup(
        state=OwnerState.ATTACHABLE,
        attachment=Attachment(descriptor=descriptor, token=token),
        reason="attached to the running daemon",
    )


def look_up_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    wait_seconds: float = 0.0,
) -> OwnerLookup:
    """Say what owner is out there, waiting only while that could still change.

    Waiting is for ``ABSENT`` alone, because that is the one reading a starting
    owner produces: it publishes only once it is listening, so a client can
    arrive after the lock was taken and before the file exists. The other
    states are already answers. A descriptor that names another profile or a
    configuration this client did not ask for will still name it in ten
    seconds, and polling would only postpone the same verdict.
    """
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        try:
            lookup = _inspect(auth_root, profile, config)
        except DescriptorError as exc:
            # Distinct from absence so the file is preserved rather than
            # cleaned up: beside a held lock it belongs to a live daemon. It
            # says nothing about whether the position is free, which is why
            # this state still allows an election attempt.
            return OwnerLookup(state=OwnerState.UNTRUSTED, reason=str(exc))

        if lookup.state is not OwnerState.ABSENT:
            return lookup
        if time.monotonic() >= deadline:
            return lookup
        time.sleep(_ATTACH_POLL_SECONDS)


def find_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    wait_seconds: float = 0.0,
) -> Attachment | None:
    """The attachment alone, for callers that only want to talk to an owner.

    Callers deciding what this process should *be* want
    :func:`look_up_owner`: the reason there is no attachment governs what they
    may do next, and it is lost here.
    """
    lookup = look_up_owner(auth_root, profile, config, wait_seconds=wait_seconds)
    if lookup.state is not OwnerState.ATTACHABLE:
        logger.info("Not attaching to a daemon: %s", lookup.reason)
    return lookup.attachment


def daemon_would_be_used(config: AppConfig) -> bool:
    """Whether this process is even a candidate for sharing a browser.

    Separate from finding an owner, because it depends on nothing outside the
    configuration and rules out the two cases where the question is moot.
    """
    if not config.server.daemon_enabled:
        return False
    # An explicit HTTP bind is already one server for many clients, so there is
    # nothing for a daemon to deduplicate. Only stdio spawns a process per
    # client, which is the whole reason this exists.
    return config.server.transport == "stdio"
