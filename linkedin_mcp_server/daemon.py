"""Decide whether this process attaches to a daemon, owns one, or goes it alone.

A stdio server is spawned per MCP client, and #598 made several of them safe on
one profile by passing the browser between them. Safe, but not cheap: ownership
moves on nearly every call, and every move reopens Chromium and revalidates the
session against ``/feed/``. One long-lived owner removes that traffic entirely.

This module is only the discovery half: it answers "is there an owner I should
be talking to, and may I trust it with a token". Electing one and spawning it
live in :mod:`linkedin_mcp_server.daemon_election`, and forwarding to what it
returns in :mod:`linkedin_mcp_server.daemon_proxy`.

Finding an owner takes a wait rather than a single read, because the interesting
case is neither "an owner exists" nor "none does", but the window in between. An
owner takes the lock first and publishes its descriptor only once it is actually
listening, so a client arriving in that gap loses the lock race while the file
still says whatever the *last* owner left there — nothing, or a descriptor that
outlived its writer. Settling on either reading is how a process ends up driving
its own browser against the same profile for its whole life, which is the
per-call handoff this feature exists to remove, and it is what two clients
starting together would normally hit.

One rule underpins the whole module: a file says nothing about whether the
process that wrote it still exists. Only :meth:`DaemonLock.try_acquire` settles
who owns the browser, and only connecting settles whether anything is listening.
Every place this module was wrong before, it was wrong by forgetting that — most
recently about ``ATTACHABLE`` itself, which an owner leaves behind intact when it
crashes after publishing.

That is also why the only entry point returns an :class:`OwnerLookup` rather
than an optional attachment. A convenience wrapper that handed back just the
attachment existed here and was removed: it let a caller act on "there is a
daemon" without the state that says how much that is worth.
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import threading
import time
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path

from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import DaemonDescriptor, DescriptorError
from linkedin_mcp_server.session_state import get_runtime_id

logger = logging.getLogger(__name__)

#: Re-read this often while waiting. Short enough that the common case (an owner
#: that is nearly ready) does not feel like a stall. How *long* to wait belongs
#: to the caller that lost the lock race, not here.
_ATTACH_POLL_SECONDS = 0.1
_DESCRIPTOR_READ_SECONDS = 1.0


class _DescriptorReadTimeout(TimeoutError):
    pass


class OwnerState(enum.Enum):
    """What the published descriptor says, in the terms a caller must act on.

    Strictly a reading of one file. None of these states knows whether the
    process that wrote it still exists, which is why none of them decides who
    owns the browser; they decide whether there is someone to *talk* to, and
    what to say when there is not.
    """

    #: Nothing published. The ordinary first start.
    ABSENT = "absent"

    #: A descriptor this client may use, with a token to match. Says the file
    #: is *compatible*, never that anyone is listening: an owner that crashes
    #: after publishing leaves exactly this behind. Whether the endpoint
    #: answers is settled by connecting, and who owns the browser by the lock.
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
    #: Why, for a log line. Never a token, and never a value read from the
    #: configuration. It *can* name a path or host taken from the descriptor
    #: when that is the diagnosis — an unusable profile path is only
    #: actionable if you can see which path — so it is DEBUG-grade detail and
    #: the callers here log it accordingly.
    reason: str = ""

    @property
    def worth_connecting(self) -> bool:
        """Whether there is a compatible endpoint to try talking to.

        Deliberately not "an owner is running". Connecting is what establishes
        that, and a caller whose connection is refused is looking at a crashed
        owner's leftovers, not at a daemon. It must fall through to the lock
        exactly as if nothing had been published.
        """
        return self.state is OwnerState.ATTACHABLE


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
        # Says what the file is, not what any process is doing. "attached to
        # the running daemon" was the earlier wording and it was wrong twice
        # over: nothing has attached yet, and the writer may be long dead.
        reason="the published daemon is compatible with this client",
    )


class _DescriptorInspector:
    """Reuse one native descriptor inspection until it has actually finished."""

    def __init__(self, auth_root: Path, profile: Path, config: AppConfig) -> None:
        self._auth_root = auth_root
        self._profile = profile
        self._config = config
        self._pending: queue.Queue[OwnerLookup | BaseException] | None = None
        self._settled = threading.Event()

    @property
    def settled(self) -> bool:
        """Whether an inspection here has reached an outcome, of either kind.

        An outcome and never a verdict: a reader that raised has finished
        touching state storage just as much as one that returned, and this
        question is only ever asked about what the reader may still be doing.
        Stays true once an inspection completes, because what a later caller
        needs to know is that this inspector is no longer the first to touch
        that state, not which of its reads is currently in flight.
        """
        return self._settled.is_set()

    def inspect_until(self, *, timeout: float) -> OwnerLookup:
        """Wait within one budget without abandoning a blocked native reader."""
        pending = self._begin()
        try:
            value = pending.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise _DescriptorReadTimeout(
                "Daemon descriptor state could not be read in time"
            ) from None
        self._pending = None
        if isinstance(value, BaseException):
            raise value
        return value

    def settle_within(self, *, timeout: float) -> bool:
        """Wait for the inspection in flight to finish, without consuming it.

        The reading itself belongs to :meth:`inspect_until`; this is for a caller
        that must know the reader is no longer inside state storage before it
        does something of its own. It reuses the one pending inspection rather
        than starting a reader of its own, so waiting for the answer can never be
        what makes a second process touch that state.
        """
        self._begin()
        return self._settled.wait(max(timeout, 0.0))

    def _begin(self) -> queue.Queue[OwnerLookup | BaseException]:
        pending = self._pending
        if pending is not None:
            return pending
        result: queue.Queue[OwnerLookup | BaseException] = queue.Queue(maxsize=1)
        self._pending = result

        def inspect() -> None:
            try:
                try:
                    value: OwnerLookup | BaseException = _inspect(
                        self._auth_root, self._profile, self._config
                    )
                except BaseException as exc:  # noqa: BLE001 - re-raised by the caller
                    value = exc
                result.put(value)
            finally:
                # In the finally, so a reader that could not even hand its answer
                # back still reports that it has stopped. A waiter that hung on
                # such a reader would be waiting for something that has already
                # happened.
                self._settled.set()

        threading.Thread(
            target=inspect,
            name="daemon-descriptor-read",
            daemon=True,
        ).start()
        return result


def _names_an_ignored_instance(lookup: OwnerLookup, ignored: AbstractSet[str]) -> bool:
    """Whether this reading is of a generation the caller has already written off."""
    if not ignored:
        return False
    attachment = lookup.attachment
    return attachment is not None and attachment.descriptor.instance_id in ignored


def look_up_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    wait_seconds: float = 0.0,
    ignore_instances: AbstractSet[str] = frozenset(),
    _inspector: _DescriptorInspector | None = None,
) -> OwnerLookup:
    """Read the descriptor until it is compatible or the budget runs out.

    Every state but ``ATTACHABLE`` is waited out, not just ``ABSENT``. The
    tempting rule is that only ``ABSENT`` can turn into ``ATTACHABLE``, since a
    starting owner publishes late. It is wrong for the same reason every other
    mistake in this module was: a descriptor outlives the owner that wrote it,
    so a fresh owner mid-startup is read through the dead one's file. Measured
    on this tree — an owner that crashed serving a sibling profile reads as
    ``INCOMPATIBLE`` while its replacement is coming up.

    Stopping at ``ATTACHABLE`` is not the same claim. It says a compatible file
    is on disk, which is all a re-read can ever establish; the process that
    wrote it may be gone. A caller whose connection to that endpoint fails must
    go to the lock rather than conclude a daemon exists.

    Callers pass a wait only when they have reason to think an owner is
    starting, which in practice means having just lost the lock race. With the
    default of no wait this is a single read.

    *ignore_instances* names generations the caller has already judged unusable,
    and it exists because ``ATTACHABLE`` alone made this function return
    instantly on a file the caller was going to throw away. Only the caller can
    know that: burial lives in :mod:`linkedin_mcp_server.daemon_election` and is
    established by connecting, which nothing here does. Without it a caller that
    asked for a wait got none, and its own retry loop spun through the whole
    backoff at read speed, starting a descriptor inspection thread and touching
    state storage on every pass. Named instances are therefore waited out like
    any other unusable state: the poll keeps watching the same file, so a *new*
    generation published over it is still picked up within one poll interval,
    and the buried endpoint is never contacted again. When the budget ends with
    nothing better, the reading is returned exactly as before, so the caller's
    own downgrade is unchanged.

    Raises:
        ValueError: *wait_seconds* is not finite.
    """
    # A NaN or infinite budget makes `monotonic() >= deadline` false forever,
    # so the stdio process would never finish starting. Refused rather than
    # clamped: an infinite wait is a plausible thing for a caller to mean and a
    # ruinous thing to grant, and the configuration parsers refuse non-finite
    # timeouts on the same grounds. A negative wait is merely no wait.
    if not math.isfinite(wait_seconds):
        raise ValueError(f"wait_seconds must be a finite number, got {wait_seconds}")

    wait_budget = max(wait_seconds, 0.0)
    deadline = time.monotonic() + wait_budget
    inspector = _inspector or _DescriptorInspector(auth_root, profile, config)
    last_lookup: OwnerLookup | None = None
    while True:
        remaining = deadline - time.monotonic()
        read_timeout = (
            _DESCRIPTOR_READ_SECONDS
            if wait_budget == 0.0
            else min(_DESCRIPTOR_READ_SECONDS, max(remaining, 0.0))
        )
        try:
            lookup = inspector.inspect_until(timeout=read_timeout)
        except _DescriptorReadTimeout as exc:
            return last_lookup or OwnerLookup(
                state=OwnerState.UNTRUSTED, reason=str(exc)
            )
        except DescriptorError as exc:
            # Distinct from absence so the file is preserved rather than
            # cleaned up: beside a held lock it belongs to a live daemon. It
            # says nothing about whether the position is free, which is why
            # this state still allows an election attempt.
            lookup = OwnerLookup(state=OwnerState.UNTRUSTED, reason=str(exc))

        last_lookup = lookup
        if lookup.state is OwnerState.ATTACHABLE and not _names_an_ignored_instance(
            lookup, ignore_instances
        ):
            return lookup

        # Never sleep past the deadline. A flat poll interval turned a 1 ms
        # budget into 101 ms, which is a hundredfold overrun for a caller that
        # asked to stay near fail-fast. Measured before this clamp existed.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The state at INFO, the detail at DEBUG. A reason can quote a path
            # or host out of the descriptor, and "nothing usable was published"
            # is the part that belongs in a log a user pastes into a report.
            # An ignored generation is named as such rather than by its state,
            # which would otherwise print the "attachable" this wait exists to
            # disbelieve.
            if _names_an_ignored_instance(lookup, ignore_instances):
                logger.info("No daemon to attach to (the published one is unusable)")
            else:
                logger.info("No daemon to attach to (%s)", lookup.state.value)
            logger.debug("Daemon lookup detail: %s", lookup.reason)
            return lookup
        time.sleep(min(_ATTACH_POLL_SECONDS, remaining))


def daemon_would_be_used(config: AppConfig) -> bool:
    """Whether this process is even a candidate for sharing a browser.

    Separate from finding an owner, because it depends on nothing outside the
    configuration and rules out the two cases where the question is moot.
    """
    if not config.server.daemon_enabled:
        return False
    # An explicit HTTP bind is already one server for many clients, so there is
    # nothing for a daemon to deduplicate. Settle that before container policy:
    # DAEMON_ENABLED on HTTP is inert everywhere, and warning that Docker
    # ignored it because of the display would describe a refusal that never
    # happened.
    if config.server.transport != "stdio":
        return False
    if get_runtime_id().endswith("-container"):
        # Xvfb belongs to PID 1's process group. The daemon owner deliberately
        # starts a new session so it can outlive an ordinary stdio frontend;
        # inside the image that gives the browser an owner which outlives the
        # display it needs. Measured: the owner had a distinct process group and
        # was still alive when EOF ended the frontend, then the container's PID
        # namespace killed it with the display. A display supervisor would make
        # the experimental daemon much larger than the problem it solves here,
        # so the container keeps one browser per frontend instead.
        logger.warning(
            "DAEMON_ENABLED is ignored in a container; the shared-browser "
            "daemon cannot outlive the virtual display owned by this server"
        )
        return False
    return True
