"""Getting an owner started, from the side of the process that wants one.

The counterpart to :mod:`linkedin_mcp_server.daemon.discovery`, which only reads.
This is where a process acts: it starts a detached owner and waits for the endpoint to
exist. It never becomes the owner itself, because it cannot: ``cli_main`` runs
the stdio server blocking, so a process that served the owner's HTTP could not
also serve its own client.

The child owns every operation that may mutate daemon state. It takes the lock,
opens the log, starts the endpoint, and publishes while holding that lock. This
subprocess boundary is also the frontend's timeout boundary: if state storage is
stuck in the kernel, the parent can kill the child and prove that no abandoned
worker can later acquire the lock or publish over an in-process fallback. A
thread cannot provide that proof because it cannot be killed.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypeVar, cast

from linkedin_mcp_server import __version__
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import config as daemon_config
from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
from linkedin_mcp_server.daemon import discovery as daemon_discovery
from linkedin_mcp_server.daemon import owner as daemon_owner
from linkedin_mcp_server.daemon import startup
from linkedin_mcp_server.daemon import version as daemon_version
from linkedin_mcp_server.daemon.discovery import (
    Attachment,
    OwnerLookup,
    OwnerState,
    _DescriptorInspector,
    _DescriptorReadTimeout,
)
from linkedin_mcp_server.daemon.lock import DaemonLockError
from linkedin_mcp_server.process_protocol import new_nonce
from linkedin_mcp_server.process_tree import (
    ProcessTreeError,
    WindowsJob,
    release_nonce,
    release_windows_gate,
    terminate_process_group,
    windows_gate_command,
)

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"

#: How long a frontend waits for an owner to become attachable before giving up
#: and reporting what it last saw. It is what bounds the delay a user sees when
#: something goes wrong rather than when it goes right.
#:
#: Longer than the owner's endpoint and commit allowances combined, which a test
#: enforces. A frontend stops a child that has said nothing by the time this runs
#: out, so an owner permitted to take longer would be killed on a slow machine
#: while still inside its own rules. The remainder covers configuration handover
#: and lock attempts before the owner's clocks start.
DEFAULT_ELECTION_SECONDS = 90.0

#: A quiescent observation window after active election ends. Fifteen seconds
#: bounds the known overhang of an operation already begun, one complete
#: descriptor read and reachability probe, plus a small scheduling margin. It is
#: not an allowance for arbitrary scheduler delay.
DEFAULT_SETTLEMENT_SECONDS = 15.0

#: How long a published owner has to answer before it is treated as *silent*.
#: This is a loopback request to a process that is either serving or gone, so
#: the only thing a longer budget buys is a longer stall on a dead descriptor —
#: and a dead one does not stall at all, it refuses. Measured against a real
#: descriptor and token: a closed port comes back in about ten milliseconds,
#: while a process holding a listening socket that nobody reads spends the whole
#: budget. That is the distinction :class:`Reach` exists to carry.
_REACHABLE_SECONDS = 5.0

#: How long an older owner has to acknowledge a turnover request. Short:
#: the reply says only that the request arrived, and whether the browser is
#: actually free is settled by the lock afterwards, not by this.
_STAND_DOWN_SECONDS = 5.0

#: How long to pause before attempting the lock again, when the descriptor on
#: disk is readable but known unusable. Short, because the thing being waited for
#: is an owner finishing its shutdown, and long enough not to spin.
_RETRY_SECONDS = 0.2
_FAILURE_VERDICT_SECONDS = 0.2
_PREPARED_READ_SECONDS = 1.0
_PREPARED_CLEANUP_SECONDS = 1.0

#: Retry quickly through the initial race, then back off so a later lock release
#: remains discoverable without creating one short-lived child per polling pass.
_OWNER_START_BURST = 3
_OWNER_START_RETRY_SECONDS = 0.5
_MAX_OWNER_START_RETRY_SECONDS = 8.0


def _owner_start_delay_after(starts: int, current: float) -> float:
    if starts < _OWNER_START_BURST:
        return current
    return min(
        max(current * 2, _OWNER_START_RETRY_SECONDS),
        _MAX_OWNER_START_RETRY_SECONDS,
    )


class Reach(enum.Enum):
    """What a probe of a published endpoint established.

    Three answers rather than two, because "it did not answer" covers two
    situations that call for opposite reactions, and collapsing them is what
    made a healthy owner unreachable for a whole election.

    A process that is *gone* leaves a closed port, and the kernel refuses the
    connection at once. A process that is *stalled* still holds its listening
    socket, so the handshake completes into the backlog and the probe waits out
    its whole budget. Measured against the real probe with a real descriptor and
    token: about ten milliseconds for the first, a full five seconds for the
    second. Three orders of magnitude apart, and the old ``bool`` threw the
    difference away.
    """

    #: The endpoint answered an authenticated request. Attach to it.
    ANSWERED = "answered"

    #: Nothing is serving at that address, or something is and it is not this
    #: owner. Either way the descriptor is leftovers.
    REFUSED = "refused"

    #: Something holds the port and did not answer in time. No proof it is
    #: alive, and no proof it is dead.
    SILENT = "silent"


#: Proves a published endpoint is live within the supplied remaining budget.
#: Injectable so the election can be tested without a real server; production
#: passes nothing and gets a real round trip.
Reachable = Callable[[Attachment, float], Reach]


@dataclass(frozen=True)
class ElectionOutcome:
    """What came of trying to get an owner running."""

    attachment_lookup: OwnerLookup
    #: Whether this process started the owner. Diagnostics only: an attachment
    #: is worth exactly the same either way.
    started_owner: bool = False

    @property
    def worth_connecting(self) -> bool:
        return self.attachment_lookup.worth_connecting


def obtain_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    deadline_seconds: float = DEFAULT_ELECTION_SECONDS,
    settlement_seconds: float = DEFAULT_SETTLEMENT_SECONDS,
    connect: Reachable | None = None,
    buried: AbstractSet[str] = frozenset(),
) -> ElectionOutcome:
    """Return an owner to talk to, starting one if nobody else has.

    Active election and quiescent settlement have separate budgets. Their global
    deadlines are fixed from this call's entry. An early terminal local failure
    gets at most one settlement budget from that failure, capped by the global
    deadline. Settlement never starts or turns over an owner; it only observes
    the canonical descriptor and authenticates a compatible endpoint.

    *buried* names generations the caller has already found unusable, so they
    are never contacted or returned here. A proxy recovering from an owner that
    refused its calls passes them, because a retiring owner still answers the
    listing this election probes with.

    Returns at once, with nothing worth connecting to and a ``fallback`` set,
    when an owner of this build with another configuration answers its one
    probe or stays silent through it: that owner is left alone, and the caller
    drives its own browser rather than waiting on the lock it may hold.
    """
    # Frozen here because the first election is part of a frontend's own
    # startup: a package replaced later must show up as a difference against
    # this value when an owner is spawned, not become the value.
    daemon_version.build_identity()
    entered = time.monotonic()
    active_deadline = entered + max(deadline_seconds, 0.0)
    settlement_budget = max(settlement_seconds, 0.0)
    settlement_deadline = active_deadline + settlement_budget
    started = False
    starts = 0
    next_start = 0.0
    start_retry_seconds = _OWNER_START_RETRY_SECONDS
    reach = connect or _reachable
    inspector = _DescriptorInspector(auth_root, profile, config)
    # A copy, never the caller's own set: `_live_lookup` adds to it.
    buried = set(buried)
    asked_for_turnover = False
    last_lookup = OwnerLookup(
        state=OwnerState.ABSENT,
        reason="the election ended before daemon state was observed",
    )

    def look(wait_seconds: float = 0.0) -> OwnerLookup:
        nonlocal asked_for_turnover, last_lookup
        found, asked, observed = _live_lookup(
            auth_root,
            profile,
            config,
            reach,
            buried,
            inspector=inspector,
            may_ask_for_turnover=not asked_for_turnover,
            wait_seconds=wait_seconds,
            deadline=active_deadline,
        )
        asked_for_turnover = asked_for_turnover or asked
        if observed:
            last_lookup = found
        return found

    def settle(*, deadline: float | None = None) -> ElectionOutcome:
        effective_deadline = (
            settlement_deadline
            if deadline is None
            else min(settlement_deadline, deadline)
        )
        return _settle_owner(
            auth_root,
            profile,
            config,
            reach,
            buried,
            inspector=inspector,
            deadline=effective_deadline,
            last_lookup=last_lookup,
            started=started,
        )

    def settle_terminal_local_result() -> ElectionOutcome:
        return settle(deadline=time.monotonic() + settlement_budget)

    while True:
        if time.monotonic() >= active_deadline:
            return settle()
        lookup = look()
        if lookup.worth_connecting or lookup.fallback is not None:
            return ElectionOutcome(lookup, started_owner=started)
        if time.monotonic() >= active_deadline:
            return settle()

        now = time.monotonic()
        may_start = now >= next_start
        remaining = active_deadline - now
        if may_start and remaining > 0:
            starts += 1
            start_retry_seconds = _owner_start_delay_after(starts, start_retry_seconds)
            next_start = now + start_retry_seconds
            try:
                inspector.require_fresh_inspection()
                attempt = _start_owner(
                    auth_root,
                    profile,
                    config,
                    timeout=remaining,
                    inspector=inspector,
                )
            except DaemonLockError:
                logger.warning("The daemon lock is unusable", exc_info=True)
                return ElectionOutcome(last_lookup, started_owner=started)
            except OSError:
                logger.warning("The daemon could not be started", exc_info=True)
                return settle_terminal_local_result()
        else:
            attempt = _Attempt.CONTENDED

        if attempt is _Attempt.ABORTED:
            logger.warning("A daemon child reported a permanent startup failure")
            return settle_terminal_local_result()
        if attempt is _Attempt.FAILED:
            logger.warning(
                "A daemon child failed; waiting for any concurrent election winner"
            )
        if attempt is _Attempt.STARTED:
            started = True

        remaining = active_deadline - time.monotonic()
        if remaining <= 0:
            return settle()
        lookup = look(min(_RETRY_SECONDS, remaining))
        if lookup.worth_connecting or lookup.fallback is not None:
            return ElectionOutcome(lookup, started_owner=started)
        if time.monotonic() >= active_deadline:
            return settle()


def _settle_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    reach: Reachable,
    buried: set[str],
    *,
    inspector: _DescriptorInspector,
    deadline: float,
    last_lookup: OwnerLookup,
    started: bool,
) -> ElectionOutcome:
    """Observe one quiescent election tail without causing owner-side effects."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ElectionOutcome(last_lookup, started_owner=started)
        lookup, _asked, observed = _live_lookup(
            auth_root,
            profile,
            config,
            reach,
            buried,
            inspector=inspector,
            may_ask_for_turnover=False,
            wait_seconds=min(_RETRY_SECONDS, remaining),
            deadline=deadline,
        )
        if observed:
            last_lookup = lookup
        if lookup.worth_connecting or lookup.fallback is not None:
            return ElectionOutcome(lookup, started_owner=started)


def _wants_a_decision(lookup: OwnerLookup) -> bool:
    """Whether an unusable reading still needs the election to act on it.

    A control-only pair is not an owner to attach to, but two kinds of it are
    not merely something to wait out either. A protocol mismatch is an owner to
    ask to stand down or to write off, and a same-build configuration mismatch
    is an owner to probe: alive, it is left alone and the caller falls back at
    once; gone, its descriptor is leftovers like any other. Every other
    reading is paced exactly as before, which keeps a caller from spinning
    through its backoff at read speed on a file it cannot use.
    """
    attachment = lookup.attachment
    if attachment is None or not attachment.control_only:
        return False
    if lookup.mismatch is daemon_discovery.Mismatch.PROTOCOL:
        return True
    return (
        lookup.mismatch is daemon_discovery.Mismatch.CONFIGURATION
        and attachment.descriptor.package_version == __version__
    )


def _live_lookup(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    reach: Reachable,
    buried: set[str],
    *,
    inspector: _DescriptorInspector,
    may_ask_for_turnover: bool = True,
    wait_seconds: float = 0.0,
    deadline: float,
) -> tuple[OwnerLookup, bool, bool]:
    """Find an owner and prove it answers, or report it as leftovers.

    The one place ``ATTACHABLE`` is turned from a claim about a file into a
    claim about a process. Everything in
    :mod:`linkedin_mcp_server.daemon.discovery` reads disk; this connects.

    A descriptor that fails the probe is downgraded rather than deleted, and the
    distinction matters: a live owner this client merely cannot reach right now
    would be stranded by a deletion, along with every other client attached to
    it. The file is left alone and the *lock* decides what happens next, which
    is the rule the whole module is built on.

    Failing the probe is itself two things, and only one of them is final. A
    refusal says nothing usable is at that address, so the instance is buried and
    not asked again. A silence says only that the answer did not arrive in time,
    so nothing is recorded and the next pass asks afresh.

    Reads stay on the caller's one inspector. A paced pass may re-read the
    canonical descriptor, but returns when that pass's wait ends so the active
    loop can try the lock again instead of spending its whole budget on reads.

    Returns the lookup, whether turnover was requested, and whether the lookup
    came from a completed descriptor read. The last flag lets settlement retain
    its last concrete observation when a bounded read times out.
    """
    del auth_root, profile, config  # carried by the reusable inspector
    wait_deadline = min(deadline, time.monotonic() + max(wait_seconds, 0.0))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (
                OwnerLookup(
                    state=OwnerState.UNTRUSTED,
                    reason="the election deadline expired before descriptor read",
                ),
                False,
                False,
            )
        try:
            lookup = inspector.inspect_until(
                timeout=min(daemon_discovery._DESCRIPTOR_READ_SECONDS, remaining)
            )
        except _DescriptorReadTimeout as exc:
            return (
                OwnerLookup(state=OwnerState.UNTRUSTED, reason=str(exc)),
                False,
                False,
            )
        except daemon_descriptor.DescriptorError as exc:
            # A completed, untrusted read is still a concrete observation. Pace it
            # like every other unusable canonical state so a persistent parse or
            # permission failure cannot create one reader thread per loop pass.
            lookup = OwnerLookup(state=OwnerState.UNTRUSTED, reason=str(exc))

        ignored = (
            lookup.attachment is not None
            and lookup.attachment.descriptor.instance_id in buried
        )
        # Before the generic filter, not after it: a control-only reading is
        # never worth connecting to, and discarded here it would never reach
        # the turnover or the liveness probe below.
        if (lookup.worth_connecting or _wants_a_decision(lookup)) and not ignored:
            break
        remaining = min(wait_deadline, deadline) - time.monotonic()
        if remaining <= 0:
            if ignored:
                return (
                    OwnerLookup(
                        state=OwnerState.INCOMPATIBLE,
                        reason="the published daemon was already found unusable",
                    ),
                    False,
                    True,
                )
            return lookup, False, True
        time.sleep(min(daemon_discovery._ATTACH_POLL_SECONDS, remaining))

    attachment = lookup.attachment
    assert attachment is not None  # both ways out of the loop imply one
    instance = attachment.descriptor.instance_id
    if instance in buried:
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the published daemon was already found unusable",
            ),
            False,
            True,
        )

    if attachment.control_only:
        return _decide_control_only(
            lookup,
            attachment,
            reach,
            buried,
            may_ask_for_turnover=may_ask_for_turnover,
            deadline=deadline,
        )

    stale = (
        daemon_version.compare(
            owner=attachment.descriptor.package_version, frontend=__version__
        )
        is daemon_version.Skew.OWNER_IS_STALE
    )
    if stale:
        if may_ask_for_turnover:
            logger.info(
                "The running daemon is version %s and this build is %s; asking it "
                "to hand the browser over",
                attachment.descriptor.package_version,
                __version__,
            )
            _ask_to_stand_down(attachment)
        # Classification and turnover are separate. Settlement never sends the
        # request, but it still writes this generation off so its paced reads wait
        # for a replacement instead of rediscovering the same stale owner at speed.
        buried.add(instance)
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the running daemon is older than this build",
            ),
            may_ask_for_turnover,
            True,
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the published daemon was not probed before the deadline",
            ),
            False,
            True,
        )
    verdict = reach(attachment, min(_REACHABLE_SECONDS, remaining))
    if verdict is Reach.ANSWERED:
        return lookup, False, True

    if verdict is Reach.SILENT:
        # Not buried, and that is the whole of the fix. Something holds the port
        # and did not get to us inside the budget, which is what a paused or
        # overloaded owner looks like — including one this very election started
        # and watched report ready. Burying it made every later pass short
        # circuit, so the owner never got a second chance inside the election it
        # belonged to, and the frontend spent its whole budget contending for a
        # lock the healthy owner was still holding.
        #
        # Left to the caller's own deadline rather than bounded by a retry
        # count. Each probe already costs the full budget, so a fixed number of
        # retries covers a fixed and rather short stall — measured, one retry
        # rescues a six second freeze and still gives up on a fifteen second
        # one. The election deadline is what bounds this, as it always was.
        logger.info("The published daemon has not answered yet; will ask again")
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the published daemon has not answered yet",
            ),
            False,
            True,
        )

    buried.add(instance)
    logger.info("The published daemon is not answering; electing a new one")
    # Deliberately not ABSENT. The file is still there, and calling it absent
    # invites a caller to clean it up; this says "not for us", which is what both
    # a corpse's descriptor and a superseded owner's are.
    return (
        OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            reason="the published daemon did not answer, so it is leftovers",
        ),
        False,
        True,
    )


def _decide_control_only(
    lookup: OwnerLookup,
    attachment: Attachment,
    reach: Reachable,
    buried: set[str],
    *,
    may_ask_for_turnover: bool,
    deadline: float,
) -> tuple[OwnerLookup, bool, bool]:
    """Act on an owner this build may control but never give a tool call.

    Nothing returned here is worth connecting to, whatever happens. The pair
    was proved for its endpoint, profile, runtime and token, and that is enough
    to send a stand-down request or a probe; it is not enough to forward a call.

    **Another protocol.** An older owner is asked to stand down through the
    control floor, the one route every protocol keeps. The same or a newer one
    is left running. Either way this generation is written off, so the election
    goes to the lock: after a turnover that is where the replacement starts,
    and otherwise it ends in the caller's own browser for as long as that owner
    lives.

    **Same build, another configuration.** Probed once, because a descriptor
    outlives its writer and an owner that went idle leaves one behind on
    purpose. Refused, it is buried like any leftovers so the lock can be
    taken. Answering or silent, it is left alone and the election ends in this
    client's own browser (``OwnerLookup.fallback``), with no turnover request,
    no owner start and no second probe.
    """
    instance = attachment.descriptor.instance_id
    if lookup.mismatch is daemon_discovery.Mismatch.PROTOCOL:
        older = (
            daemon_version.compare(
                owner=attachment.descriptor.package_version, frontend=__version__
            )
            is daemon_version.Skew.OWNER_IS_STALE
        )
        asked = older and may_ask_for_turnover
        if asked:
            logger.info(
                "The running daemon is version %s and speaks an older protocol; "
                "asking it to hand the browser over",
                attachment.descriptor.package_version,
            )
            _ask_to_stand_down(attachment)
        elif not older:
            logger.info(
                "The running daemon speaks another protocol and is not older than "
                "this build; leaving it alone"
            )
        buried.add(instance)
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason=lookup.reason,
                mismatch=lookup.mismatch,
            ),
            asked,
            True,
        )

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the published daemon was not probed before the deadline",
                mismatch=lookup.mismatch,
            ),
            False,
            True,
        )
    verdict = reach(attachment, min(_REACHABLE_SECONDS, remaining))
    if verdict is Reach.REFUSED:
        # Nothing usable at that address: leftovers, and the lock decides.
        buried.add(instance)
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason=lookup.reason,
                mismatch=lookup.mismatch,
            ),
            False,
            True,
        )
    # Answered or silent, the election ends here. Silence proves nothing about
    # the owner's life and so authorizes no turnover, but it does not make the
    # owner usable either: waiting out a whole election for a process that
    # could never serve this configuration buys nothing, and the profile lease
    # is what keeps the two browsers apart meanwhile. The two are reported
    # apart, so a log never calls a silent owner alive.
    fallback = (
        daemon_discovery.DirectFallback.LIVE_RIVAL
        if verdict is Reach.ANSWERED
        else daemon_discovery.DirectFallback.SILENT_RIVAL
    )
    logger.info("Leaving the shared browser owner alone: %s", fallback.value)
    return (
        OwnerLookup(
            state=OwnerState.INCOMPATIBLE,
            reason=fallback.value,
            mismatch=lookup.mismatch,
            fallback=fallback,
        ),
        False,
        True,
    )


def _ask_to_stand_down(attachment: Attachment) -> None:
    """Ask an older owner to give up the browser for this build.

    Best effort, and deliberately so. The owner may already be gone or may be
    busy finishing a call. In every one of those cases the caller's next step is
    the same: try the lock, and wait if somebody still holds it. Raising here
    would turn a routine upgrade into a client that fails to start.

    Whether the owner *complied* is never assumed. The lock is what says the
    browser is free, and this function never claims otherwise.

    A turnover request addressed to the instance the descriptor names, through
    the one route and request version every owner keeps
    (``daemon.owner.STAND_DOWN_PATH``), whatever its tool protocol.
    """

    descriptor = attachment.descriptor
    url = f"http://{descriptor.host if ':' not in descriptor.host else f'[{descriptor.host}]'}:{descriptor.port}{daemon_owner.STAND_DOWN_PATH}"
    try:
        with daemon_owner.direct_http_client(timeout=_STAND_DOWN_SECONDS) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {attachment.token}"},
                json=daemon_owner.stand_down_request(
                    descriptor.instance_id, mode=daemon_owner.STAND_DOWN_TURNOVER
                ),
            )
    except Exception:
        logger.debug("The stale daemon could not be asked to stand down", exc_info=True)
        return
    logger.debug("Stand-down request answered with %s", response.status_code)


def _reachable(attachment: Attachment, timeout: float) -> Reach:
    """What the published endpoint says when asked with this token.

    An authenticated round trip rather than a TCP connect. A connect succeeds
    against any listener that happens to hold the port, including one the
    operating system handed to something else after the owner died, and against
    an owner whose token no longer matches the file this client just read.

    The timeout is caught apart from every other failure, and that separation is
    the whole point of this function's return type. An owner that is gone refuses
    the connection; an owner that is merely stalled holds the port and says
    nothing. Both used to arrive here as ``False``, so a frontend buried a live
    owner it had just started because one ping landed in a scheduler stall.

    The round trip is a tool listing, not a ping, and the difference decides the
    answer. The 2026-07-28 protocol era removed ping (SDK
    ``docs/migration.md:1800``) and a default client negotiates that era with a
    current owner, so a ping came back as an error from a healthy owner and was
    read here as a refusal. A listing answers in every era, runs no tool and
    touches no browser.
    """
    import asyncio

    async def ask() -> Reach:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        try:
            async with Client(
                StreamableHttpTransport(
                    attachment.descriptor.url,
                    auth=attachment.token,
                    # Never through a proxy: this is a loopback hop carrying the
                    # token to a logged-in session. See ``direct_http_client``.
                    httpx_client_factory=daemon_owner.direct_async_http_client,
                )
            ) as client:
                await client.list_tools()
        except Exception:
            # Answered, and wrongly: a refused connection, a rejected token, a
            # stranger on the port. All of them say this descriptor is not an
            # owner to attach to, and saying so again would not change it.
            logger.debug("The published daemon did not answer", exc_info=True)
            return Reach.REFUSED
        return Reach.ANSWERED

    if timeout <= 0:
        return Reach.SILENT
    try:
        return asyncio.run(asyncio.wait_for(ask(), timeout))
    except TimeoutError:
        # Something holds the port and did not get to us in time. Deliberately
        # not treated as leftovers: a loaded machine, a paused process or a busy
        # owner all look like this, and every one of them may answer a moment
        # later.
        logger.debug("The published daemon did not answer in time", exc_info=True)
        return Reach.SILENT
    except Exception:
        # A caller already inside a running loop, which is no observation at all.
        # Classified with the refusals rather than the silences because asking
        # again would fail for the same reason, so a retry would only spend the
        # budget. Production never reaches it: the election runs synchronously
        # from ``cli_main`` before any server exists.
        logger.debug("The daemon reachability check could not run", exc_info=True)
        return Reach.REFUSED


class _Started(enum.Enum):
    """What the startup protocol established at the frontend boundary."""

    #: The endpoint answered and the descriptor is published.
    YES = "yes"

    #: The child could not serve, said so, died trying, or was stopped for
    #: having said nothing at all.
    NO = "no"

    #: The child proved a permanent error or could not publish safely.
    ABORTED = "aborted"

    #: This generation ended safely after releasing its position. Try again while
    #: the frontend's election budget remains.
    RETRY = "retry"

    #: Neither, within the budget, before a prepared generation exists.
    STILL_TRYING = "still_trying"

    #: The COMMIT frame may have reached the lock holder, which settles its own
    #: lifetime and must not be killed by the parent.
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class _BootstrapDiagnosis:
    code: str | None
    log_path: str | None = None


class _BootstrapReport:
    """Collect bounded diagnostics from the child's bootstrap pipe."""

    _CAPTURE_BYTES = 4096
    _READ_BYTES = 512

    def __init__(
        self, stream: BinaryIO | None, handshake_nonce: str | None = None
    ) -> None:
        self._result: queue.Queue[_BootstrapDiagnosis | None] = queue.Queue(maxsize=1)
        if stream is None:
            self._result.put(None)
            return

        def collect() -> None:
            code: str | None = None
            log_path: str | None = None
            remaining = self._CAPTURE_BYTES
            pending = bytearray()
            overflowed = False
            try:
                with stream:
                    while chunk := stream.readline(self._READ_BYTES):
                        if remaining <= 0:
                            continue
                        sample = chunk[:remaining]
                        remaining -= len(sample)
                        if overflowed:
                            if sample.endswith(b"\n"):
                                overflowed = False
                            continue
                        pending.extend(sample)
                        if len(sample) < len(chunk) or (
                            remaining <= 0 and not sample.endswith(b"\n")
                        ):
                            pending.clear()
                            overflowed = True
                            continue
                        if not sample.endswith(b"\n"):
                            continue
                        code, candidate = self._parse_line(
                            bytes(pending),
                            code=code,
                            handshake_nonce=handshake_nonce,
                        )
                        if candidate is not None and log_path is None:
                            log_path = candidate
                        pending.clear()
            except OSError:
                pass
            self._result.put(_BootstrapDiagnosis(code, log_path))

        threading.Thread(
            target=collect,
            name="daemon-bootstrap",
            daemon=True,
        ).start()

    @staticmethod
    def _parse_line(
        raw: bytes, *, code: str | None, handshake_nonce: str | None
    ) -> tuple[str | None, str | None]:
        try:
            text = raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8")
        except UnicodeError:
            return code, None
        prefix = f"{daemon_owner.BOOTSTRAP_PREFIX} "
        if text.startswith(prefix):
            candidate = text.removeprefix(prefix)
            if candidate in {
                daemon_owner.BOOTSTRAP_CONFIGURATION,
                daemon_owner.BOOTSTRAP_STATE,
                daemon_owner.BOOTSTRAP_LOCK,
                daemon_owner.BOOTSTRAP_LOG,
                daemon_owner.BOOTSTRAP_ATTACHED,
            }:
                return candidate, None
            return code, None
        fields = text.split(" ", 3)
        if len(fields) != 4 or fields[0] != daemon_owner.BOOTSTRAP_LOG_HINT_PREFIX:
            return code, None
        _, version, nonce, candidate = fields
        encoded = candidate.encode("utf-8")
        if (
            version != daemon_owner.BOOTSTRAP_LOG_HINT_VERSION
            or handshake_nonce is None
            or nonce != handshake_nonce
            or len(encoded) > daemon_owner.BOOTSTRAP_LOG_HINT_MAX_PATH_BYTES
            or not candidate
            or any(not character.isprintable() for character in candidate)
        ):
            return code, None
        path = Path(candidate)
        if not path.is_absolute() or path.name != "daemon.log":
            return code, None
        return code, candidate

    def read(
        self, *, timeout: float = _FAILURE_VERDICT_SECONDS
    ) -> _BootstrapDiagnosis | None:
        """Return collected records without letting diagnostics pin fallback."""
        try:
            return self._result.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None


def _report_child_failure(report: _BootstrapReport) -> None:
    """Log the most actionable safe diagnosis available from the child."""
    diagnosis = report.read()
    code = None if diagnosis is None else diagnosis.code
    if code == daemon_owner.BOOTSTRAP_CONFIGURATION:
        logger.warning("The daemon rejected its startup configuration")
    elif code == daemon_owner.BOOTSTRAP_STATE:
        logger.warning("The daemon could not resolve its profile state")
    elif code == daemon_owner.BOOTSTRAP_LOCK:
        logger.warning("The daemon could not take ownership")
    elif code == daemon_owner.BOOTSTRAP_LOG:
        logger.warning("The daemon log could not be opened")
    elif code == daemon_owner.BOOTSTRAP_ATTACHED:
        if diagnosis is not None and diagnosis.log_path is not None:
            logger.warning(
                "The daemon could not start; inspect the daemon log at %r",
                diagnosis.log_path,
            )
        else:
            logger.warning("The daemon could not start; inspect the daemon log")
    else:
        logger.warning("The daemon stopped before its diagnostic log became available")


class _Attempt(enum.Enum):
    """What came of trying to start an owner, in the three ways it can end.

    A boolean cannot carry this, and collapsing it was measured to cost a
    minute: a child that reported it could not serve read as "somebody is
    starting", so the caller waited out its whole deadline for a descriptor
    nothing was going to write.
    """

    #: An owner is up and has published. Ready to attach.
    STARTED = "started"

    #: Somebody is starting: another process holds the lock, or the child this
    #: process started has not answered yet and is still holding one. Wait.
    CONTENDED = "contended"

    #: The child this process started did not become an owner. Before this
    #: reaches the caller, ``_spawn`` has issued a hard stop and waited for the
    #: child. If the kernel cannot finish that stop within the bounded wait,
    #: ``_stop_child`` logs that the profile may remain locked.
    FAILED = "failed"

    #: The child proved a permanent startup or publication failure. Retrying the
    #: same state would only spend the remaining election budget.
    ABORTED = "aborted"


def _start_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    timeout: float,
    inspector: _DescriptorInspector | None = None,
) -> _Attempt:
    """Start a child that owns all potentially blocking state mutations."""
    del profile  # the owner derives it from the configuration it is handed
    return _start_contending_for_the_lock(
        auth_root,
        config,
        timeout=timeout,
        inspector=inspector,
    )


def _start_contending_for_the_lock(
    auth_root: Path,
    config: AppConfig,
    *,
    timeout: float,
    inspector: _DescriptorInspector | None = None,
) -> _Attempt:
    """Start one child that competes for the lock and reports the outcome."""
    started = _spawn(
        auth_root,
        config,
        timeout=timeout,
        inspector=inspector,
    )
    if started is _Started.YES:
        return _Attempt.STARTED
    if started is _Started.NO:
        return _Attempt.FAILED
    if started is _Started.ABORTED:
        return _Attempt.ABORTED
    return _Attempt.CONTENDED


def _refusal_before_spawn(config: AppConfig) -> _Started | None:
    """Refuse a start that cannot succeed, before there is a child to stop.

    Two permanent conditions, both answered with ``ABORTED`` so the election
    hands back to Direct rather than spending its budget on children that would
    fail the same way.

    The installed package differing from the one this process loaded is a
    diagnostic, not the authority: a replacement can still land between this
    check and the spawn, and the child's ACK is what refuses that case. The
    check exists to say why in a log a user can act on, without starting a
    process first.
    """
    try:
        running = daemon_version.build_identity()
        installed = daemon_version.installed_build_identity()
    except OSError:
        logger.warning(
            "The installed package could not be read; not starting a daemon",
            exc_info=True,
        )
        return _Started.ABORTED
    if installed != running:
        logger.warning(
            "The installed package changed after this server started (%s, now "
            "%s); restart the MCP client to use the shared browser",
            running,
            installed,
        )
        return _Started.ABORTED
    if not startup.fits(startup.CONFIG, config=daemon_config.encode(config)):
        logger.warning(
            "The configuration is too large to hand to a shared browser owner "
            "(the startup protocol carries at most %d bytes)",
            startup.MAX_FRAME_BYTES,
        )
        return _Started.ABORTED
    return None


def _spawn(
    auth_root: Path,
    config: AppConfig,
    *,
    timeout: float,
    on_spawned: Callable[[], None] | None = None,
    inspector: _DescriptorInspector | None = None,
) -> _Started:
    """Start a detached owner and atomically commit its prepared descriptor.

    The exchange itself is described in :mod:`linkedin_mcp_server.daemon.startup`.
    Standard error carries one bounded bootstrap diagnosis until the child opens
    its own log. Every operation against state storage remains inside the process
    that a timed-out frontend can kill.
    """
    refusal = _refusal_before_spawn(config)
    if refusal is not None:
        return refusal
    windows_job = WindowsJob.named("owner") if os.name == "nt" else None
    try:
        nonce = release_nonce() if windows_job is not None else None
        target = _spawn_command(
            job_name=windows_job.name if windows_job is not None else None,
        )
        command = (
            windows_gate_command(target, nonce)
            if windows_job is not None and nonce is not None
            else target
        )
        child = subprocess.Popen(
            command,
            env=_owner_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            creationflags=_detachment_flags(),
        )
    except BaseException:
        if windows_job is not None:
            windows_job.close()
        raise

    try:
        # Created only after Popen returns and carried only by the child's own
        # standard input. Startup output written before the HELLO cannot know
        # the token the parent will accept.
        handshake_nonce = new_nonce()
        # Inside the same handler as the spawn: the diagnosis starts a thread,
        # and a host that has run out of them would otherwise leave this child
        # running with nothing holding it, waiting out its own timeout for a
        # HELLO no one is left to send.
        bootstrap = _BootstrapReport(
            getattr(child, "stderr", None), handshake_nonce=handshake_nonce
        )
    except BaseException:
        _stop_child(child, windows_job=windows_job, assigned=False)
        if windows_job is not None:
            windows_job.close()
        raise
    channel: startup.FrontendChannel | None = None
    prepared_instance_id: str | None = None
    #: Whether the child may already own itself, because a COMMIT frame may
    #: have reached it. Past this, no exception licenses a kill.
    owner_may_be_adopted = False
    assigned = False
    job_settled = False

    def stop_child() -> None:
        nonlocal job_settled
        try:
            _stop_child(child, windows_job=windows_job, assigned=assigned)
        finally:
            if windows_job is not None:
                windows_job.close()
                job_settled = True

    def release_job() -> None:
        nonlocal job_settled
        if windows_job is not None:
            windows_job.close()
            job_settled = True

    try:
        try:
            if windows_job is not None and nonce is not None:
                windows_job.assign_popen(child)
                assigned = True
                # No owner code exists until the parent owns the Job.
                release_windows_gate(child.stdin, nonce)
            # Only now, because until the gate is released its input is the one
            # thing that can stop it, and that close belongs to ``_stop_child``.
            channel = startup.FrontendChannel(child, handshake_nonce)
            if on_spawned is not None:
                on_spawned()
            started = time.monotonic()
            greeting = _greet(channel, timeout=timeout)
        except BaseException:
            stop_child()
            raise
        if greeting is not None:
            # Nothing usable crossed: the configuration is written only after a
            # matching ACK, so this child holds no settings and no lock it could
            # have taken with them.
            stop_child()
            if greeting is _Started.NO:
                _report_child_failure(bootstrap)
            return greeting

        try:
            _hand_over_config(
                channel, config, timeout=timeout - (time.monotonic() - started)
            )
        except TimeoutError:
            logger.warning(
                "The daemon never read its configuration; stopping it so the "
                "lock is not held by a process that cannot serve"
            )
            verdict = _await_prepared(channel, timeout=_FAILURE_VERDICT_SECONDS)
            if isinstance(verdict, str):
                prepared_instance_id = verdict
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.RETRY if verdict is _Started.RETRY else _Started.NO
        except Exception:
            logger.warning(
                "The daemon could not receive its configuration", exc_info=True
            )
            verdict = _await_prepared(channel, timeout=_FAILURE_VERDICT_SECONDS)
            if isinstance(verdict, str):
                prepared_instance_id = verdict
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.RETRY if verdict is _Started.RETRY else _Started.NO
        except BaseException:
            stop_child()
            raise

        try:
            verdict = _await_prepared(
                channel, timeout=timeout - (time.monotonic() - started)
            )
        except BaseException:
            # Still pre-commit. EOF is only a lease once the child has armed its
            # prepared-state wait; an interrupted or suspended child may never
            # observe it, so the parent must end the lock holder itself.
            stop_child()
            raise
        if verdict is _Started.STILL_TRYING:
            logger.warning(
                "The daemon did not prepare an endpoint; stopping it so the lock "
                "is not held by a process that never served"
            )
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.NO
        if verdict is _Started.NO:
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.NO
        if verdict is _Started.ABORTED:
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.ABORTED
        if verdict is _Started.RETRY:
            stop_child()
            return _Started.RETRY

        if not isinstance(verdict, str):  # pragma: no cover - handled above
            raise AssertionError(f"Unexpected startup verdict: {verdict}")
        prepared_instance_id = verdict
        prepared_deadline = time.monotonic() + min(
            _PREPARED_READ_SECONDS,
            max(timeout - (time.monotonic() - started), 0.0),
        )

        try:
            profile = _resolve_profile_until(
                Path(config.browser.user_data_dir),
                timeout=prepared_deadline - time.monotonic(),
            )
            _validate_prepared_until(
                auth_root,
                prepared_instance_id,
                profile=profile,
                config=config,
                timeout=prepared_deadline - time.monotonic(),
            )
        except TimeoutError:
            # No COMMIT was sent, so this child cannot become canonical. Waiting
            # for a verdict would leave it holding the lock until its own
            # authorization timeout expires.
            stop_child()
            return _Started.NO
        except Exception:
            logger.warning("The daemon prepared unusable startup state", exc_info=True)
            # No COMMIT was sent, so the child is killed however its verdict
            # reads; a handler that raised here would leave it holding the lock.
            try:
                verdict = _await_committed(channel, timeout=_FAILURE_VERDICT_SECONDS)
            finally:
                stop_child()
            return _Started.RETRY if verdict is _Started.RETRY else _Started.ABORTED
        except BaseException:
            stop_child()
            raise

        # COMMIT_ATTEMPTED. The child owns the daemon lock and therefore owns
        # publication. Once the COMMIT frame may have reached it, no exception
        # authorizes a kill: the child either commits and serves or observes EOF
        # and discards its state.
        owner_may_be_adopted = True
        try:
            _send_commit(
                channel,
                prepared_instance_id,
                timeout=timeout - (time.monotonic() - started),
            )
        except Exception:
            logger.warning(
                "The daemon commit request could not be delivered", exc_info=True
            )
            return _settle_commit_result(
                auth_root,
                prepared_instance_id,
                child,
                timeout=timeout - (time.monotonic() - started),
                inspector=inspector,
            )

        committed = _await_committed(
            channel, timeout=timeout - (time.monotonic() - started)
        )
        if committed is _Started.YES:
            return _Started.YES
        if committed is _Started.ABORTED:
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.ABORTED
        if committed is _Started.RETRY:
            stop_child()
            return _Started.RETRY
        if committed is _Started.UNCERTAIN:
            return _Started.UNCERTAIN
        return _settle_commit_result(
            auth_root,
            prepared_instance_id,
            child,
            timeout=timeout - (time.monotonic() - started),
            inspector=inspector,
        )
    finally:
        if windows_job is not None and not job_settled:
            if owner_may_be_adopted:
                # The COMMIT frame precedes owner adoption. Closing this copy
                # cannot revoke an adopted owner, and kills one that never adopted.
                release_job()
            else:
                stop_child()
        if channel is not None:
            # End of file aborts a prepared child that never received COMMIT.
            # After that frame it is only a lease: the lock holder settles
            # publication.
            channel.close()
        else:
            # The channel never took the pipes, so no thread is inside them.
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    with contextlib.suppress(OSError, ValueError):
                        stream.close()
        _reap(child)
        if prepared_instance_id is not None and not owner_may_be_adopted:
            try:
                _discard_prepared_until(
                    auth_root,
                    prepared_instance_id,
                    timeout=_PREPARED_CLEANUP_SECONDS,
                )
            except Exception:
                logger.warning(
                    "The daemon's abandoned prepared state could not be removed",
                    exc_info=True,
                )


#: How long to wait for a killed child to be collected. Short by design: this
#: follows a hard kill (``SIGKILL`` on POSIX and ``TerminateProcess`` on
#: Windows), which the process cannot decline, so anything but a prompt exit
#: means the process is stuck in the kernel and no amount of waiting here will
#: change that.
_STOP_CHILD_SECONDS = 2.0


def _stop_child(
    child: subprocess.Popen[bytes],
    *,
    windows_job: WindowsJob | None = None,
    assigned: bool = False,
) -> None:
    """End a child whose startup cannot produce a usable owner, and collect it.

    Called after a configuration timeout, an exhausted startup budget, or a
    terminal non-ready verdict. A reported failure has already run ``_serve``'s
    bounded endpoint shutdown, and EOF means the child already exited. A timed-out
    child may be blocked in log or lock I/O, so it must be made terminal before the
    frontend can fall back to an in-process browser.

    The contained tree is killed outright rather than asked politely first. A
    ``SIGTERM`` grace period is time added after the caller's budget is already
    spent, so a child that ignores it would make the documented ceiling false.

    The wait normally proves process death before this returns: a signalled
    process may still have its descriptors open until the kernel finishes the
    exit. The bounded-wait warning is the explicit exception, and the caller must
    not claim the lock is certainly free after it.
    """
    deadline = time.monotonic() + _STOP_CHILD_SECONDS
    if windows_job is not None:
        if assigned:
            try:
                windows_job.terminate()
                try:
                    child.wait(timeout=max(deadline - time.monotonic(), 0.0))
                except subprocess.TimeoutExpired as exc:
                    raise ProcessTreeError(
                        "The Windows owner gate did not exit after Job termination"
                    ) from exc
                # ActiveProcesses includes an exited member until every
                # process-handle reference is released. CPython retains this
                # direct gate handle after wait(), so release it before asking
                # the Job to prove it is empty.
                windows_job.release_popen_handle(child)
            except BaseException:
                # Kill-on-close is the one containment that depends on none of
                # the calls above. Without this the handle stays open and unheld
                # by anyone: the caller cleared its own cleanup before calling,
                # and only this branch still knows the Job exists. The tree would
                # then live until this process exits, holding the daemon lock a
                # failed owner was stopped to release. The installer keeps the
                # same fallback in ``_cleanup_assigned_windows_job_once``.
                windows_job.close()
                raise
            # Left to settle itself: it closes on a proved drain and retains the
            # handle on an unproved one, and retention is the deliberate choice
            # to keep containment authority rather than to abandon it.
            # Float rounding must never increase the drain beyond the stop budget.
            windows_job.wait_until_empty(
                timeout=min(_STOP_CHILD_SECONDS, max(deadline - time.monotonic(), 0.0))
            )
        else:
            # Before assignment EOF is the only contained stop. If the isolated
            # gate does not accept it, stop and reap that direct child explicitly.
            # Once the startup channel owns it this is ``None``: the kill below
            # is bounded, a close racing the writer's blocked write is not.
            if child.stdin is not None:
                with contextlib.suppress(OSError, ValueError):
                    child.stdin.close()
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                child.kill()
            finally:
                windows_job.close()
            try:
                child.wait(timeout=max(deadline - time.monotonic(), 0.0))
            except subprocess.TimeoutExpired as exc:
                raise ProcessTreeError(
                    "The Windows owner gate did not exit after cleanup"
                ) from exc
        return

    stopped_tree = terminate_process_group(
        child.pid, timeout=_STOP_CHILD_SECONDS, child=child
    )
    if not stopped_tree:
        with contextlib.suppress(OSError):
            child.kill()
    try:
        child.wait(timeout=max(deadline - time.monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        # Uninterruptible sleep, or a kernel that has not finished with it.
        # Reported rather than waited out, since the caller has a deadline and
        # this is not something a longer wait resolves.
        logger.warning(
            "The daemon has not exited after being killed; the profile may stay "
            "locked until it does"
        )


def _greet(channel: startup.FrontendChannel, *, timeout: float) -> _Started | None:
    """Send HELLO and accept only an ACK from this build. ``None`` means go on.

    The child holds nothing yet: no configuration, no lock. So every way this
    fails ends in a stop, and the only question is whether retrying could help.
    Silence, end of file and a frame this protocol does not permit could be a
    slow or crashed child (``NO``). An ACK naming another protocol or another
    build is the installed package having changed under this process, which
    another child would repeat (``ABORTED``).
    """
    started = time.monotonic()
    try:
        channel.send(startup.HELLO, protocol=startup.PROTOCOL, timeout=timeout)
        answer = channel.receive(timeout=timeout - (time.monotonic() - started))
    except TimeoutError:
        logger.warning(
            "The daemon did not answer its startup greeting; stopping it so the "
            "lock is not held by a process that cannot serve"
        )
        return _Started.NO
    except (OSError, ValueError):
        logger.info("The daemon could not be greeted", exc_info=True)
        return _Started.NO
    try:
        ack = startup.expect(answer, startup.ACK, nonce=channel.nonce)
    except startup.ProtocolError:
        logger.info("The daemon did not answer in the startup protocol")
        return _Started.NO
    running = daemon_version.build_identity()
    if ack["protocol"] != startup.PROTOCOL or ack["build"] != running:
        logger.warning(
            "The daemon that started is build %s, not this server's %s; the "
            "installed package changed, so restart the MCP client to use the "
            "shared browser",
            ack["build"],
            running,
        )
        return _Started.ABORTED
    return None


def _hand_over_config(
    channel: startup.FrontendChannel, config: AppConfig, *, timeout: float
) -> None:
    """Write the CONFIG frame, which is the only place the settings travel."""
    channel.send(startup.CONFIG, config=daemon_config.encode(config), timeout=timeout)


_FilesystemResult = TypeVar("_FilesystemResult")


def _filesystem_until(
    operation: Callable[[], _FilesystemResult],
    *,
    timeout: float,
    thread_name: str,
    timeout_message: str,
) -> _FilesystemResult:
    """Run one parent-side filesystem operation within the caller's wait."""
    result: queue.Queue[_FilesystemResult | BaseException] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            value = operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
            result.put(exc)
        else:
            result.put(value)

    threading.Thread(target=run, name=thread_name, daemon=True).start()
    try:
        value = result.get(timeout=max(timeout, 0.0))
    except queue.Empty:
        raise TimeoutError(timeout_message) from None
    if isinstance(value, BaseException):
        raise value
    return value


def _resolve_profile_until(profile: Path, *, timeout: float) -> Path:
    """Resolve the configured profile without pinning a lock-holding child."""
    return _filesystem_until(
        lambda: profile.expanduser().resolve(),
        timeout=timeout,
        thread_name="daemon-profile-resolve",
        timeout_message="The daemon profile could not be resolved in time",
    )


def _validate_prepared_until(
    auth_root: Path,
    instance_id: str,
    *,
    profile: Path,
    config: AppConfig,
    timeout: float,
) -> None:
    """Validate prepared state without letting a blocked filesystem pin the caller."""
    _filesystem_until(
        lambda: daemon_descriptor.validate_prepared(
            auth_root, instance_id, profile=profile, config=config
        ),
        timeout=timeout,
        thread_name="daemon-prepared-read",
        timeout_message="Prepared daemon state could not be read in time",
    )


def _discard_prepared_until(
    auth_root: Path, instance_id: str, *, timeout: float
) -> None:
    """Bound cleanup that can only finish against this abandoned generation.

    The worker may complete after the frontend gives up waiting. That is safe only
    because ``discard_prepared`` names the unique instance's pending descriptor and
    token directly; it never removes canonical state or scans generations belonging
    to a later lock holder.
    """
    _filesystem_until(
        lambda: daemon_descriptor.discard_prepared(auth_root, instance_id),
        timeout=timeout,
        thread_name="daemon-prepared-cleanup",
        timeout_message="Prepared daemon state could not be removed in time",
    )


def _send_commit(
    channel: startup.FrontendChannel, instance_id: str, *, timeout: float
) -> None:
    """Tell the lock-holding child to publish the generation it prepared."""
    channel.send(startup.COMMIT, instance=instance_id, timeout=timeout)


def _read_canonical_until(
    auth_root: Path, *, timeout: float
) -> daemon_descriptor.DaemonDescriptor | None:
    """Read canonical state without letting filesystem I/O exceed the caller's wait."""
    return _filesystem_until(
        lambda: daemon_descriptor.read(auth_root),
        timeout=timeout,
        thread_name="daemon-canonical-read",
        timeout_message="Canonical daemon state could not be read in time",
    )


def _settle_commit_result(
    auth_root: Path,
    instance_id: str,
    child: subprocess.Popen[bytes],
    *,
    timeout: float,
    inspector: _DescriptorInspector | None = None,
) -> _Started:
    """Settle a missing commit verdict through the election's shared inspection."""
    del child  # process state cannot disambiguate an attempted remote rename
    try:
        if inspector is None:
            canonical = _read_canonical_until(auth_root, timeout=timeout)
            committed_instance = None if canonical is None else canonical.instance_id
        else:
            lookup = inspector.inspect_until(timeout=timeout)
            attachment = lookup.attachment
            committed_instance = (
                None if attachment is None else attachment.descriptor.instance_id
            )
    except Exception:
        return _Started.UNCERTAIN
    if committed_instance == instance_id:
        return _Started.YES
    # After the COMMIT frame may have arrived, a dead child is not proof of an
    # uncommitted generation. NFS may report a failed rename and keep returning a
    # stale canonical entry until after that child exits. The next successful
    # owner removes abandoned pending files and superseded tokens while holding
    # the lock.
    return _Started.UNCERTAIN


def _await_committed(channel: startup.FrontendChannel, *, timeout: float) -> _Started:
    """Wait for the lock holder's final commit verdict."""
    try:
        answer = channel.receive(timeout=timeout)
    except TimeoutError:
        return _Started.STILL_TRYING
    try:
        verdict = startup.expect(answer, startup.VERDICT, nonce=channel.nonce)
    except startup.ProtocolError:
        return _Started.NO
    status = verdict["status"]
    if status == startup.COMMITTED:
        return _Started.YES
    if status == startup.ABORTED:
        return _Started.ABORTED
    if status == startup.RETRY:
        return _Started.RETRY
    if status == startup.UNCERTAIN:
        return _Started.UNCERTAIN
    return _Started.NO


def _spawn_command(*, job_name: str | None = None) -> list[str]:
    """How the owner is invoked.

    ``-P`` keeps the working directory off ``sys.path`` while preserving the
    interpreter's user site, where a supported ``pip install --user`` may have put
    this package. An explicitly isolated frontend is different: weakening its
    ``-I``, ``-s`` or ``-E`` mode would restore startup code it refused before the
    configuration secrets cross stdin. The child environment drops ``PYTHONPATH``
    separately, so a workspace cannot put a shadow
    ``linkedin_mcp_server/daemon/owner.py`` back in front and receive the
    configuration secrets.
    """
    interpreter_flags: list[str] = []
    if sys.flags.isolated:
        # ``-I`` already implies ``-E`` and ``-s``. Preserve the mode itself rather
        # than expanding it, so the child's flags describe the same boundary.
        interpreter_flags.append("-I")
    else:
        if sys.flags.ignore_environment:
            interpreter_flags.append("-E")
        if sys.flags.no_user_site:
            interpreter_flags.append("-s")

    command = [
        sys.executable,
        *interpreter_flags,
        "-P",
        "-m",
        "linkedin_mcp_server.daemon.owner",
    ]
    if job_name is not None:
        command += ["--job-name", job_name]
    return command


#: Environment variables carrying a secret that the owner has no use for. It
#: receives every setting it needs on standard input, so keeping these would be
#: the exposure the stdin channel exists to avoid, held for the owner's whole
#: lifetime rather than the frontend's. ``/proc/<pid>/environ`` is readable by
#: this account, and ``ps e`` shows it on the BSDs.
#:
#: ``PROXY_SERVER`` belongs here despite its name. The documented form accepts
#: embedded credentials — ``http://user:password@host:port`` — which
#: ``BrowserConfig.validate`` splits out into the separate fields
#: (``config/schema.py:242-253``). It splits them out of the *configuration*; the
#: environment variable still holds the original string.
_SECRETS_TO_DROP = ("PROXY_PASSWORD", "PROXY_USERNAME", "PROXY_SERVER")


def _owner_environment() -> dict[str, str]:
    """The environment to start the owner in, minus what it must not keep.

    ``daemon.config`` hands the configuration over on standard input precisely
    so that a password does not sit in a readable environment. That is only half
    the job while the child inherits the frontend's environment unchanged: the
    documented way to configure a proxy is ``PROXY_PASSWORD``
    (``config/loaders.py:107-108``), so the owner would hold the raw value for
    as long as it runs, which is far longer than the process it came from.

    Dropped rather than emptied. An empty ``PROXY_USERNAME`` is meaningful to
    the configuration parser (``daemon/descriptor.py:480``), and the owner is
    told the real values through the channel that was built for them.
    """
    environment = dict(os.environ)
    for name in _SECRETS_TO_DROP:
        environment.pop(name, None)
    environment.pop("PYTHONPATH", None)
    return environment


def _detachment_flags() -> int:
    """What it takes on this platform to survive the client that started us.

    ``start_new_session`` is POSIX-only. CPython's Windows implementation names
    the parameter ``unused_start_new_session`` and ignores it outright
    (``subprocess.py:1461``), so passing it there detaches nothing: a console
    ``Ctrl+C``, or a client that cleans up its process tree, would take the
    owner down with it — the opposite of what the owner is for.

    The Windows equivalent is a creation flag. ``CREATE_NEW_PROCESS_GROUP``
    removes the child from the console group that receives ``Ctrl+C`` and
    ``Ctrl+Break``, ``DETACHED_PROCESS`` gives it no console, and
    ``CREATE_BREAKAWAY_FROM_JOB`` keeps a host's kill-on-close Job from retaining
    authority after the owner adopts its own Job.
    """
    if not _IS_WINDOWS:
        return 0
    return (  # pragma: no cover - exercised on the Windows runner
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_BREAKAWAY_FROM_JOB
    )


def _reap(child: subprocess.Popen[bytes]) -> None:
    """Collect a child that has already exited, without waiting for one that has not.

    A successful election leaves a *running* owner, and this process must never
    wait for it: outliving this process is the point. The other outcomes leave a
    child that has exited, and ``poll`` collects exactly those.

    Belt and braces rather than a fix for something observed. ``subprocess``
    keeps its own registry of abandoned children and reaps them when the next one
    is created, and a failed election measured here left no zombie with this call
    removed. It is kept because that behaviour is an implementation detail of the
    standard library, and this is the one place that knowingly walks away from a
    child.
    """
    with contextlib.suppress(OSError, ValueError):
        child.poll()


def _await_prepared(
    channel: startup.FrontendChannel, *, timeout: float
) -> str | _Started:
    """Wait for a prepared generation id, a failure, or silence."""
    try:
        answer = channel.receive(timeout=timeout)
    except TimeoutError:
        return _Started.STILL_TRYING
    if answer is None:
        logger.info("The daemon exited before it prepared an endpoint")
        return _Started.NO
    try:
        if answer.get("type") == startup.PREPARED:
            prepared = startup.expect(answer, startup.PREPARED, nonce=channel.nonce)
            return cast(str, prepared["instance"])
        verdict = startup.expect(answer, startup.VERDICT, nonce=channel.nonce)
    except startup.ProtocolError:
        logger.info("The daemon answered outside the startup protocol")
        return _Started.NO
    status = verdict["status"]
    if status == startup.FAILED:
        logger.info("The daemon reported that it could not start")
        return _Started.NO
    if status == startup.ABORTED:
        logger.info("The daemon reported a terminal startup failure")
        return _Started.ABORTED
    if status == startup.RETRY:
        logger.info("The daemon released its startup position for another attempt")
        return _Started.RETRY
    return _Started.NO
