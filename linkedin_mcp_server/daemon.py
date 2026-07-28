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

import logging
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Attachment:
    """An owner worth talking to, and the credential for doing so."""

    descriptor: DaemonDescriptor
    token: str


def _usable_attachment(
    auth_root: Path, profile: Path, config: AppConfig
) -> Attachment | None:
    """Whether the published descriptor describes a daemon we may attach to.

    Order matters. The cheap, local checks come first and the ones that touch
    the endpoint come last, so a descriptor that is merely stale is rejected
    without a connection attempt.
    """
    descriptor = daemon_descriptor.read(auth_root)
    if descriptor is None:
        return None

    # Not enforced by read(), deliberately: it is the caller who knows which
    # runtime it belongs to. A host attaching to a container's daemon would be
    # handed a browser in a different filesystem namespace.
    if descriptor.runtime_id != get_runtime_id():
        logger.debug("Daemon belongs to another runtime; not attaching")
        return None

    # The lock is per auth root, but a profile is what a browser opens. Two
    # profiles side by side elect one owner between them, so an owner exists
    # that is not *our* owner.
    if not descriptor.serves(profile):
        logger.debug("Daemon serves a different profile; not attaching")
        return None

    # No endpoint check here: `from_mapping` already refuses a non-local host
    # while parsing, so `read` above cannot return one. Repeating it would read
    # as the safeguard when it is really a second copy of one, and a second
    # copy is what rots when the first moves.
    token = daemon_descriptor.read_token(auth_root, descriptor)

    # Keyed with the token, so this can only run once the token is in hand.
    if descriptor.config_fingerprint != daemon_descriptor.config_fingerprint(
        config, key=token
    ):
        logger.debug("Daemon runs a different configuration; not attaching")
        return None

    return Attachment(descriptor=descriptor, token=token)


def find_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    wait_seconds: float = 0.0,
) -> Attachment | None:
    """Find an owner to attach to, optionally waiting for one to finish starting.

    A :class:`DescriptorError` is not an absence. A descriptor that exists but
    cannot be trusted, sitting beside a held lock, means a live daemon this
    client cannot talk to, and treating that as "nobody is there" would start a
    second browser on the profile the daemon is using.
    """
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        try:
            attachment = _usable_attachment(auth_root, profile, config)
        except DescriptorError as exc:
            logger.info("Not attaching to the running daemon: %s", exc)
            return None

        if attachment is not None:
            return attachment
        if time.monotonic() >= deadline:
            return None
        time.sleep(_ATTACH_POLL_SECONDS)


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
