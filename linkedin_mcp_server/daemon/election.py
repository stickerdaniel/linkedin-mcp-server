"""Getting an owner started, from the side of the process that wants one.

The acting counterpart of :mod:`linkedin_mcp_server.daemon.discovery`: it
starts a detached owner through :mod:`linkedin_mcp_server.daemon.spawn` and
waits for its endpoint. It never becomes the owner itself, since ``cli_main``
blocks on the stdio server.

Decisions live in ``daemon.core``: the loop in
:mod:`~linkedin_mcp_server.daemon.core.election` and each look at the published
owner in :mod:`~linkedin_mcp_server.daemon.core.lookup`. This module carries out
their effects and feeds back the clock and the results.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from linkedin_mcp_server import __version__
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
from linkedin_mcp_server.daemon import discovery as daemon_discovery
from linkedin_mcp_server.daemon import owner as daemon_owner
from linkedin_mcp_server.daemon import spawn as daemon_spawn
from linkedin_mcp_server.daemon import version as daemon_version
from linkedin_mcp_server.daemon.core import election as election_core
from linkedin_mcp_server.daemon.core import lookup as lookup_core
from linkedin_mcp_server.daemon.core.lookup import Finding, Reach
from linkedin_mcp_server.daemon.core.spawn import Attempt
from linkedin_mcp_server.daemon.discovery import (
    Attachment,
    OwnerLookup,
    OwnerState,
    Unanswered,
    _DescriptorInspector,
    _DescriptorReadTimeout,
)
from linkedin_mcp_server.daemon.lock import DaemonLockError

logger = logging.getLogger(__name__)

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
#: A dead owner refuses at once; only a stalled listener spends the budget,
#: which is the distinction :class:`Reach` carries.
_REACHABLE_SECONDS = 5.0

#: How long an older owner has to acknowledge a turnover request. Short:
#: the reply says only that the request arrived, and whether the browser is
#: actually free is settled by the lock afterwards, not by this.
_STAND_DOWN_SECONDS = 5.0

#: How long to pause before attempting the lock again, when the descriptor on
#: disk is readable but known unusable. Short, because the thing being waited for
#: is an owner finishing its shutdown, and long enough not to spin.
_RETRY_SECONDS = 0.2

#: Retry quickly through the initial race, then back off so a later lock release
#: remains discoverable without creating one short-lived child per polling pass.
_OWNER_START_BURST = 3
_OWNER_START_RETRY_SECONDS = 0.5
_MAX_OWNER_START_RETRY_SECONDS = 8.0


def _pacing() -> election_core.Pacing:
    """The election's clocks as this module sets them now."""
    return election_core.Pacing(
        retry=_RETRY_SECONDS,
        burst=_OWNER_START_BURST,
        first_delay=_OWNER_START_RETRY_SECONDS,
        max_delay=_MAX_OWNER_START_RETRY_SECONDS,
    )


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
    #: The generation the election's probes last found silent, if nothing it
    #: saw since says otherwise (:class:`_ProbeWitness`). For the text a caller
    #: reads; the election decided without it.
    unanswered: Unanswered | None = None

    @property
    def worth_connecting(self) -> bool:
        return self.attachment_lookup.worth_connecting


@dataclass
class _ProbeWitness:
    """What the election last heard from the published owner, beside the reducers.

    Kept in the shell because a pass's finding cannot carry it: a silent probe
    reports ``NOT_ANSWERED_YET``, and the pass after it, out of time, reports
    ``NOT_PROBED`` and is the lookup the election returns. Nothing here is fed
    back to a reducer.

    A probe that ran out its positive budget records the probed generation. An
    answer or a refusal clears it, as does a completed read naming another
    generation. A pass that ends on the descriptor alone leaves it.
    """

    unanswered: Unanswered | None = None

    def read(self, lookup: OwnerLookup) -> None:
        attachment = lookup.attachment
        if (
            self.unanswered is not None
            and attachment is not None
            and attachment.descriptor.instance_id != self.unanswered.instance_id
        ):
            self.unanswered = None

    def probed(self, attachment: Attachment, budget: float, reach: Reach) -> None:
        if reach is not Reach.SILENT:
            self.unanswered = None
        elif budget > 0:
            # Only a probe that started: `_reachable` answers a spent budget
            # with SILENT without asking anybody.
            self.unanswered = Unanswered.of(attachment)


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

    The loop is :mod:`linkedin_mcp_server.daemon.core.election`'s, including the
    separate budgets of active election and quiescent settlement. *buried*
    names generations the caller already found unusable, which are never
    contacted or returned (a retiring owner still answers the probe).

    Returns at once with a ``fallback`` set when an owner of this version with
    another configuration answers its one probe or stays silent through it:
    that owner is left alone and the caller drives its own browser. An older
    owner is asked to stand down whatever its configuration.

    The outcome's ``unanswered`` names the published generation its probes
    last found silent, whatever the election decided.
    """
    # Frozen here because the first election is part of a frontend's own
    # startup: a package replaced later must show up as a difference against
    # this value when an owner is spawned, not become the value.
    daemon_version.build_identity()
    reach = connect or _reachable
    inspector = _DescriptorInspector(auth_root, profile, config)
    # A copy, never the caller's own set: `_live_lookup` adds to it.
    buried = set(buried)
    witness = _ProbeWitness()
    election, effects = election_core.elect(
        now=time.monotonic(),
        deadline_seconds=deadline_seconds,
        settlement_seconds=settlement_seconds,
        pacing=_pacing(),
        nothing_seen=OwnerLookup(
            state=OwnerState.ABSENT,
            reason="the election ended before daemon state was observed",
        ),
    )
    while True:
        # Exactly one effect per decision: the election waits on one thing.
        [effect] = effects
        match effect:
            case election_core.Decide(lookup=lookup, started=started):
                return ElectionOutcome(
                    cast(OwnerLookup, lookup),
                    started_owner=started,
                    unanswered=witness.unanswered,
                )
            case election_core.Look(op=op, may_ask=may_ask, wait=wait, deadline=until):
                found, asked, observed = _live_lookup(
                    reach,
                    buried,
                    inspector=inspector,
                    may_ask_for_turnover=may_ask,
                    wait_seconds=wait,
                    deadline=until,
                    witness=witness,
                )
                event: election_core.Event = election_core.Looked(
                    op,
                    time.monotonic(),
                    found,
                    conclusive=found.worth_connecting or found.fallback is not None,
                    asked=asked,
                    observed=observed,
                )
            case election_core.StartOwner(op=op, timeout=timeout):
                event = _start(auth_root, config, op, timeout, inspector)
        election, effects = election_core.on_event(election, event)


def _start(
    auth_root: Path,
    config: AppConfig,
    op: int,
    timeout: float,
    inspector: _DescriptorInspector,
) -> election_core.Event:
    """Start one child for the election, and say how that ended."""
    try:
        inspector.require_fresh_inspection()
        attempt = daemon_spawn._start_owner(
            auth_root, config, timeout=timeout, inspector=inspector
        )
    except DaemonLockError:
        logger.warning("The daemon lock is unusable", exc_info=True)
        return election_core.StartRefused(op, time.monotonic(), lock_unusable=True)
    except OSError:
        logger.warning("The daemon could not be started", exc_info=True)
        return election_core.StartRefused(op, time.monotonic(), lock_unusable=False)
    if attempt is Attempt.ABORTED:
        logger.warning("A daemon child reported a permanent startup failure")
    elif attempt is Attempt.FAILED:
        logger.warning(
            "A daemon child failed; waiting for any concurrent election winner"
        )
    return election_core.StartEnded(op, time.monotonic(), attempt)


def _live_lookup(
    reach: Reachable,
    buried: set[str],
    *,
    inspector: _DescriptorInspector,
    may_ask_for_turnover: bool = True,
    wait_seconds: float = 0.0,
    deadline: float,
    witness: _ProbeWitness | None = None,
) -> tuple[OwnerLookup, bool, bool]:
    """Find an owner and prove it answers, or report it as leftovers.

    The one place ``ATTACHABLE`` turns from a claim about a file into a claim
    about a process; what each reading calls for is
    :mod:`linkedin_mcp_server.daemon.core.lookup`'s decision. Reads stay on the
    caller's one inspector, and a paced pass returns when its wait ends so the
    loop can try the lock again.

    Returns the lookup, whether turnover was requested, and whether the lookup
    came from a completed read, so settlement keeps its last concrete
    observation when a read times out. Written-off generations are added to
    *buried*, and what each read and probe heard is told to *witness*.
    """
    if witness is None:
        witness = _ProbeWitness()
    state, effects = lookup_core.look(
        now=time.monotonic(),
        deadline=deadline,
        wait=wait_seconds,
        may_ask=may_ask_for_turnover,
        buried=frozenset(buried),
        timings=lookup_core.Timings(
            read=daemon_discovery._DESCRIPTOR_READ_SECONDS,
            poll=daemon_discovery._ATTACH_POLL_SECONDS,
            probe=_REACHABLE_SECONDS,
        ),
    )
    read = OwnerLookup(state=OwnerState.UNTRUSTED)
    timed_out = ""
    while True:
        event: lookup_core.Event | None = None
        for effect in effects:
            match effect:
                case lookup_core.ReadDescriptor(op=op, timeout=timeout):
                    try:
                        read = _read_once(inspector, timeout)
                    except _DescriptorReadTimeout as exc:
                        timed_out = str(exc)
                        event = lookup_core.ReadTimedOut(op, time.monotonic())
                    else:
                        witness.read(read)
                        event = lookup_core.Read(op, time.monotonic(), _reading(read))
                case lookup_core.Pause(op=op, seconds=seconds):
                    time.sleep(seconds)
                    event = lookup_core.Paused(op, time.monotonic())
                case lookup_core.Probe(op=op, timeout=timeout):
                    # Told here, while the probed pair is in hand: the finding
                    # this pass concludes with no longer names it.
                    probed = _attachment(read)
                    verdict = reach(probed, timeout)
                    witness.probed(probed, timeout, verdict)
                    event = lookup_core.Probed(op, time.monotonic(), verdict)
                case lookup_core.AskToStandDown(because=because):
                    _turn_over(_attachment(read), because)
                case lookup_core.LeaveAlone():
                    logger.info(
                        "The running daemon speaks another protocol and is not "
                        "older than this build; leaving it alone"
                    )
                case lookup_core.Bury(instance=instance):
                    buried.add(instance)
                case lookup_core.Conclude(finding=finding, asked=asked, observed=seen):
                    return _what_was_found(finding, read, timed_out), asked, seen
        if event is None:
            raise AssertionError(f"the lookup core left the pass waiting on {effects}")
        state, effects = lookup_core.on_event(state, event)


def _read_once(inspector: _DescriptorInspector, timeout: float) -> OwnerLookup:
    """One bounded read of the canonical descriptor, on the shared inspector."""
    try:
        return inspector.inspect_until(timeout=timeout)
    except daemon_descriptor.DescriptorError as exc:
        # A completed, untrusted read is still a concrete observation. Paced like
        # every other unusable canonical state, so a persistent parse or
        # permission failure cannot create one reader thread per loop pass.
        return OwnerLookup(state=OwnerState.UNTRUSTED, reason=str(exc))


def _reading(lookup: OwnerLookup) -> lookup_core.Reading:
    """What the lookup core needs to know about one completed read."""
    attachment = lookup.attachment
    if attachment is None:
        return lookup_core.Reading(attachable=lookup.worth_connecting)
    descriptor = attachment.descriptor
    return lookup_core.Reading(
        instance=descriptor.instance_id,
        attachable=lookup.worth_connecting,
        control_only=attachment.control_only,
        other_protocol=lookup.mismatch is daemon_discovery.Mismatch.PROTOCOL,
        other_config=lookup.mismatch is daemon_discovery.Mismatch.CONFIGURATION,
        older=daemon_version.compare(
            owner=descriptor.package_version, frontend=__version__
        )
        is daemon_version.Skew.OWNER_IS_STALE,
        same_version=descriptor.package_version == __version__,
    )


def _attachment(read: OwnerLookup) -> Attachment:
    attachment = read.attachment
    assert attachment is not None  # the core acts only on a reading with one
    return attachment


def _turn_over(attachment: Attachment, because: lookup_core.Turnover) -> None:
    if because is lookup_core.Turnover.OLDER_VERSION:
        logger.info(
            "The running daemon is version %s and this server is %s; asking it "
            "to hand the browser over",
            attachment.descriptor.package_version,
            __version__,
        )
    else:
        logger.info(
            "The running daemon is version %s and speaks an older protocol; "
            "asking it to hand the browser over",
            attachment.descriptor.package_version,
        )
    _ask_to_stand_down(attachment)


def _what_was_found(finding: Finding, read: OwnerLookup, timed_out: str) -> OwnerLookup:
    """The lookup a pass reports, and the line it logs, for *finding*."""
    incompatible = OwnerState.INCOMPATIBLE
    match finding:
        case Finding.AS_READ:
            return read
        case Finding.NOT_READ:
            return OwnerLookup(
                state=OwnerState.UNTRUSTED,
                reason="the election deadline expired before descriptor read",
            )
        case Finding.READ_TIMED_OUT:
            return OwnerLookup(state=OwnerState.UNTRUSTED, reason=timed_out)
        case Finding.ALREADY_UNUSABLE:
            return OwnerLookup(
                state=incompatible,
                reason="the published daemon was already found unusable",
            )
        case Finding.OLDER_VERSION:
            return OwnerLookup(
                state=incompatible,
                reason="the running daemon is an older version than this server",
            )
        case Finding.NOT_PROBED:
            return OwnerLookup(
                state=incompatible,
                reason="the published daemon was not probed before the deadline",
                mismatch=read.mismatch,
            )
        case Finding.NOT_ANSWERED_YET:
            logger.info("The published daemon has not answered yet; will ask again")
            return OwnerLookup(
                state=incompatible,
                reason="the published daemon has not answered yet",
            )
        case Finding.LEFTOVERS:
            logger.info("The published daemon is not answering; electing a new one")
            # Deliberately not ABSENT. The file is still there, and calling it
            # absent invites a caller to clean it up; this says "not for us",
            # which is what both a corpse's descriptor and a superseded owner's
            # are.
            return OwnerLookup(
                state=incompatible,
                reason="the published daemon did not answer, so it is leftovers",
            )
        case Finding.WRITTEN_OFF:
            return OwnerLookup(
                state=incompatible, reason=read.reason, mismatch=read.mismatch
            )
        case Finding.LIVE_RIVAL | Finding.SILENT_RIVAL:
            fallback = (
                daemon_discovery.DirectFallback.LIVE_RIVAL
                if finding is Finding.LIVE_RIVAL
                else daemon_discovery.DirectFallback.SILENT_RIVAL
            )
            logger.info("Leaving the shared browser owner alone: %s", fallback.value)
            return OwnerLookup(
                state=incompatible,
                reason=fallback.value,
                mismatch=read.mismatch,
                fallback=fallback,
            )


def _ask_to_stand_down(attachment: Attachment) -> None:
    """Ask an older owner to give up the browser for this server.

    Best effort: whatever happens, the caller next tries the lock, and only the
    lock says the browser is free. Sent to the descriptor's instance through
    the one stable route and request version (``daemon.owner.STAND_DOWN_PATH``),
    whatever its tool protocol or configuration.
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

    The round trip the owner proved before publishing (``owner.prove_hop``): a
    TCP connect succeeds against any listener on a reused port, and an owner
    whose dependencies no longer serve the pinned version would answer an
    unpinned probe and then fail every call. A timeout is ``SILENT``, kept
    apart from a refusal, because a stalled owner may answer a moment later
    and must not be buried like a dead one.
    """
    import asyncio

    async def ask() -> Reach:
        try:
            await daemon_owner.prove_hop(attachment.descriptor.url, attachment.token)
        except Exception:
            # Answered, and wrongly: a refused connection, a rejected token, a
            # stranger on the port, an owner without the pinned version. All
            # of them say this descriptor is not an owner to attach to, and
            # saying so again would not change it.
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
