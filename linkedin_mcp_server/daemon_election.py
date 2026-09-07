"""Getting an owner started, from the side of the process that wants one.

The counterpart to :mod:`linkedin_mcp_server.daemon`, which only reads. This is
where a process acts: it starts a detached owner and waits for the endpoint to
exist. It never becomes the owner itself, because it cannot: ``cli_main`` runs
the stdio server blocking, so a process that served the owner's HTTP could not
also serve its own client.

The child owns every operation that may mutate daemon state. It opens the log,
takes the lock, starts the endpoint, and publishes while holding that lock. This
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
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TypeVar, cast

from linkedin_mcp_server import (
    __version__,
    daemon_config,
    daemon_descriptor,
    daemon_owner,
    daemon_version,
)
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import (
    Attachment,
    OwnerLookup,
    OwnerState,
    _DescriptorInspector,
    look_up_owner,
)
from linkedin_mcp_server.daemon_lock import DaemonLockError
from linkedin_mcp_server.process_control import ControlListener
from linkedin_mcp_server.process_protocol import new_nonce, read_authenticated_status
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

#: How long a published owner has to answer before it is treated as *silent*.
#: This is a loopback request to a process that is either serving or gone, so
#: the only thing a longer budget buys is a longer stall on a dead descriptor —
#: and a dead one does not stall at all, it refuses. Measured against a real
#: descriptor and token: a closed port comes back in about ten milliseconds,
#: while a process holding a listening socket that nobody reads spends the whole
#: budget. That is the distinction :class:`Reach` exists to carry.
_REACHABLE_SECONDS = 5.0

#: How long a superseded owner has to acknowledge a stand-down request. Short:
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

#: How long one pass waits for the Windows exclusion namespace before looking
#: again. The election deadline is what bounds the whole wait; this only keeps
#: the loop observing the descriptor while it waits.
_EXCLUSION_SETTLE_SECONDS = 1.0

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


class _Exclusion(enum.Enum):
    """What the legacy Windows exclusion permits an election to do right now."""

    #: The tombstone exists. A child started now cannot take that name.
    READY = "ready"

    #: The inspection that would establish it is still running. Nothing may be
    #: started yet, and waiting is the thing to do.
    PENDING = "pending"

    #: The inspection stopped without establishing it. Waiting changes nothing,
    #: and starting anything is the hazard itself.
    UNAVAILABLE = "unavailable"


def _settled_exclusion(inspector: _DescriptorInspector) -> _Exclusion:
    """Read the exclusion's state without waiting for anything.

    ``READY`` comes only from the tombstone being known to exist, never from the
    inspection having stopped. Those are not the same claim and treating them as
    one is the whole defect: a reader can settle on a transient failure, an ACL
    that answered once, a descriptor that ran out, with no legacy directory
    anywhere, and a child started on the strength of that creates the very
    directory this guard exists to keep out of existence.

    The tombstone rather than the inspector is also what survives an election
    replacing its inspector, because it is a fact about a file and not about a
    thread.
    """
    if daemon_descriptor.windows_exclusion_established():
        return _Exclusion.READY
    if inspector.settled:
        return _Exclusion.UNAVAILABLE
    return _Exclusion.PENDING


def _exclusion_state(inspector: _DescriptorInspector, *, deadline: float) -> _Exclusion:
    """Whether an owner may be spawned yet, on this platform, right now.

    Only Windows has anything to wait for. There, ``prepare_daemon_state`` pins
    the account home and publishes the exclusion object that keeps every pre-v2
    release out of ``.mcp-server-linkedin``, and it does that inside the
    inspector's reader thread. A one second read budget expiring does not stop
    that thread; it only stops the *frontend* from hearing about it, and the old
    loop went straight on to spawn a child. Under a package rolled back on disk
    that child is a predecessor, which creates the legacy directory for itself
    and blocks the v2 namespace for good, invisibly to every current frontend.

    So the spawn waits for the reader instead of racing it, bounded by the
    caller's own deadline. A reader that stops having established nothing is not
    a reason to go ahead: it is a reason to stop, because this election cannot
    tell a transient failure from a permanent one and only one of those is safe
    to spawn against. The frontend falls back to its own browser and a later
    election, with private state readable again, elects an owner normally.
    """
    if not _IS_WINDOWS:
        return _Exclusion.READY
    state = _settled_exclusion(inspector)
    if state is not _Exclusion.PENDING:
        return state
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _Exclusion.PENDING
    logger.debug("Waiting for private daemon state before starting an owner")
    inspector.settle_within(timeout=min(remaining, _EXCLUSION_SETTLE_SECONDS))
    return _settled_exclusion(inspector)


def _report_unusable_exclusion(inspector: _DescriptorInspector) -> None:
    """Say why no owner can be started, in the reader's own words.

    The settled inspection is consumed here purely for its diagnosis. On Windows
    a reader that returns normally has already published the tombstone, so this
    state means it raised, and that exception names the thing to fix: a legacy
    directory to remove, or a permission that answered once and may not next
    time.
    """
    try:
        inspector.inspect_until(timeout=0.0)
    except Exception:
        logger.warning(
            "Private daemon state could not be established, so no shared owner "
            "can be started safely; this client will use its own browser",
            exc_info=True,
        )
        return
    logger.warning(  # pragma: no cover - a normal return publishes the tombstone
        "Private daemon state was read without establishing the exclusion a "
        "shared owner needs; this client will use its own browser"
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


#: Proves a published endpoint is live. Injectable so the election can be tested
#: without a real server; production passes nothing and gets a real round trip.
Reachable = Callable[[Attachment], Reach]


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
    connect: Reachable | None = None,
) -> ElectionOutcome:
    """Return an owner to talk to, starting one if nobody else has.

    The loop, and why it is a loop rather than "attach, else own, else give up":

    A frontend that loses the lock race has learned that *somebody is starting*,
    which is a reason to wait rather than to conclude anything. The tempting
    shortcut is to fall back to driving a browser in-process, and it is wrong in
    the ordinary case: two clients starting together is not an edge, and the one
    that lost would then drive its own Chromium against the same profile for its
    whole life. That is precisely the per-call handoff this feature exists to
    remove.

    Equally, winning the lock is not permission to keep it. This process cannot
    serve, so winning means starting a child and handing the lock over.

    *connect* is what settles liveness, and it is not optional. ``ATTACHABLE``
    is a statement about a *file*: an owner that dies after publishing leaves a
    descriptor and a token that pass every check there is. Reproduced on this
    tree — an owner was killed and the very next election read its leftovers as
    attachable and handed back the dead process's endpoint. Only a request that
    is answered establishes that anything is listening.
    """
    deadline = time.monotonic() + max(deadline_seconds, 0.0)
    started = False
    starts = 0
    next_start = 0.0
    start_retry_seconds = _OWNER_START_RETRY_SECONDS
    reach = connect or _reachable
    inspector = _DescriptorInspector(auth_root, profile, config)
    # Instances that answered *wrongly* — a refused connection, a rejected
    # token, a stranger on the port — so a corpse is not handed back a second
    # time. The descriptor stays on disk until a live owner overwrites it, and
    # without this the loop would re-read it, probe it again, and spin.
    #
    # Deliberately narrower than "did not answer". An instance that merely ran
    # out of time is never put here, because the next probe may well reach it:
    # a paused or overloaded owner holds its port and says nothing, and burying
    # it made a frontend refuse the healthy owner it had just started itself.
    buried: set[str] = set()
    # A turnover is asked for at most once per election, and the bound is not
    # decoration. A newer frontend replaces a stale owner and then reads the
    # replacement, and if that one still looked stale it would be asked to stand
    # down as well, and so on for the whole budget. Reproduced while testing the
    # turnover with a version override: every owner elected was immediately told
    # to hand over, and the run ended with no owner at all. One request settles
    # the case it exists for, and anything past that is a disagreement no amount
    # of restarting resolves.
    asked_for_turnover = False

    def look(wait_seconds: float = 0.0) -> OwnerLookup:
        nonlocal asked_for_turnover
        found, asked = _live_lookup(
            auth_root,
            profile,
            config,
            reach,
            buried,
            inspector=inspector,
            may_ask_for_turnover=not asked_for_turnover,
            wait_seconds=wait_seconds,
        )
        asked_for_turnover = asked_for_turnover or asked
        return found

    while True:
        lookup = look()
        if lookup.worth_connecting:
            return ElectionOutcome(lookup, started_owner=started)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ElectionOutcome(lookup, started_owner=started)

        # Asked only where a start is otherwise due, so the retry pacing is
        # unchanged, and asked before the clock is read for that pacing, because
        # the answer can involve waiting for an inspector still inside state
        # storage and a start scheduled off a stale reading would be paced from
        # before that wait.
        may_start = time.monotonic() >= next_start
        if may_start:
            exclusion = _exclusion_state(inspector, deadline=deadline)
            if exclusion is _Exclusion.UNAVAILABLE:
                # Nothing can be started against state this process cannot
                # establish, and nothing later in this election changes that.
                # Returning now is the fallback; the next election gets a fresh
                # reader and may find the same state perfectly usable.
                _report_unusable_exclusion(inspector)
                return ElectionOutcome(lookup, started_owner=started)
            may_start = exclusion is _Exclusion.READY
        now = time.monotonic()
        remaining = deadline - now
        if may_start and remaining > 0:
            starts += 1
            start_retry_seconds = _owner_start_delay_after(starts, start_retry_seconds)
            next_start = now + start_retry_seconds
            try:
                attempt = _start_owner(
                    auth_root,
                    profile,
                    config,
                    timeout=remaining,
                    inspector=inspector,
                )
            except DaemonLockError:
                # Not contention: a filesystem without usable locking, or state
                # this account cannot write. Waiting would never resolve it.
                logger.warning("The daemon lock is unusable", exc_info=True)
                return ElectionOutcome(lookup, started_owner=started)
            except OSError:
                # From the control listener, the Job, the command or the spawn
                # itself, and ``_spawn`` takes no lock on this path. Usually
                # before a child exists, though not always: ``Popen`` can raise
                # after the operating system made the process, when the parent's
                # own pipe cleanup fails. It makes no difference here, because
                # such a child is never handed its configuration and inherits no
                # lock, and this owner reads neither profile state nor the lock
                # before that handover. So the reasoning under ``_Attempt.FAILED``
                # does not reach this either way: nothing served, and nothing was
                # freed for a sibling frontend to pick up. Pacing retries here
                # would only spend the caller's whole election budget before it
                # can fall back to a direct browser, which is what the
                # predecessor avoided and what this had quietly given up.
                logger.warning("The daemon could not be started", exc_info=True)
                return ElectionOutcome(lookup, started_owner=started)
        else:
            # A delayed attempt remains scheduled, or private state is not ready
            # to have a child started against it. Descriptor observation continues
            # in the meantime without one process per polling pass.
            attempt = _Attempt.CONTENDED

        if attempt is _Attempt.ABORTED:
            logger.warning("A daemon child reported a permanent startup failure")
            return ElectionOutcome(look(), started_owner=started)
        if attempt is _Attempt.FAILED:
            # A local child failure proves only that child is gone. Another
            # frontend may already have a child holding the lock this one freed,
            # so falling back now could put two browsers on the profile.
            logger.warning(
                "A daemon child failed; waiting for any concurrent election winner"
            )
        if attempt is _Attempt.STARTED:
            started = True
            # A timed-out inspection may still be resolving a descriptor that this
            # child just replaced. The committed generation needs one fresh read.
            inspector = _DescriptorInspector(auth_root, profile, config)

        # A STARTED attempt returned only after this process atomically published
        # its prepared generation, so this re-read normally succeeds at once.
        # The wait is for the other
        # branch: this process lost the lock race, so somebody else is coming up
        # and the descriptor is not there yet.
        #
        # Bounded per pass rather than by the whole remaining budget, and that is
        # not a detail. Waiting out the full budget here consumes it inside a
        # single read, so the loop's own deadline check fires on the way back and
        # the lock is attempted exactly once — measured: one attempt against a
        # one second budget, in the case this loop exists to keep retrying. The
        # holder may also release without ever publishing, which only another
        # lock attempt can discover.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ElectionOutcome(look(), started_owner=started)
        lookup = look(min(_RETRY_SECONDS, remaining))
        if lookup.worth_connecting or time.monotonic() >= deadline:
            return ElectionOutcome(lookup, started_owner=started)


def _live_lookup(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    reach: Reachable,
    buried: set[str],
    *,
    inspector: _DescriptorInspector | None = None,
    may_ask_for_turnover: bool = True,
    wait_seconds: float = 0.0,
) -> tuple[OwnerLookup, bool]:
    """Find an owner and prove it answers, or report it as leftovers.

    The one place ``ATTACHABLE`` is turned from a claim about a file into a
    claim about a process. Everything in :mod:`linkedin_mcp_server.daemon` reads
    disk; this connects.

    A descriptor that fails the probe is downgraded rather than deleted, and the
    distinction matters: a live owner this client merely cannot reach right now
    would be stranded by a deletion, along with every other client attached to
    it. The file is left alone and the *lock* decides what happens next, which
    is the rule the whole module is built on.

    Failing the probe is itself two things, and only one of them is final. A
    refusal says nothing usable is at that address, so the instance is buried and
    not asked again. A silence says only that the answer did not arrive in time,
    so nothing is recorded and the next pass asks afresh.

    Every path here answers on the first readable descriptor, and the waiting
    happens inside :func:`look_up_owner` alone. That split is measured rather
    than tidy: an earlier version looped, and because nothing rewrites a
    descriptor except a *new* owner publishing, an unusable one was simply
    re-read until the budget ran out. After a version turnover that cost ninety
    seconds and produced no owner, while the lock the departing owner had freed
    sat there untouched, because taking it is the caller's job and this function
    never returned to let it.

    Returns the reading and whether a turnover was requested. The caller counts
    those, because asking twice in one election means telling a freshly elected
    owner to stand down as well, and that does not terminate.
    """
    lookup = look_up_owner(
        auth_root,
        profile,
        config,
        wait_seconds=wait_seconds,
        # Buried instances travel *into* the read, so a wait spends itself
        # watching for a generation that could actually be used. Passing them
        # only mattered once a wait was involved: a buried descriptor is still
        # compatible on disk, so the read returned it instantly, the downgrade
        # below turned it down, and the caller's retry loop came straight back
        # with none of its budget spent. Snapshotted rather than shared, since
        # the set is the caller's and this is the only thing here that reads it.
        ignore_instances=frozenset(buried),
        _inspector=inspector,
    )
    if not lookup.worth_connecting:
        return lookup, False

    attachment = lookup.attachment
    assert attachment is not None  # worth_connecting implies one
    instance = attachment.descriptor.instance_id
    if instance in buried:
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the published daemon was already found unusable",
            ),
            False,
        )

    if (
        may_ask_for_turnover
        and daemon_version.compare(
            owner=attachment.descriptor.package_version, frontend=__version__
        )
        is daemon_version.Skew.OWNER_IS_STALE
    ):
        # A newer frontend does not attach to an older owner, and it does not
        # merely refuse either: refusing without asking would leave it unable to
        # proceed at all, since it cannot take a lock the owner holds. So it
        # asks, and the replacement is elected from the ordinary path once the
        # lock comes free.
        logger.info(
            "The running daemon is version %s and this build is %s; asking it to "
            "hand the browser over",
            attachment.descriptor.package_version,
            __version__,
        )
        _ask_to_stand_down(attachment)
        buried.add(instance)
        return (
            OwnerLookup(
                state=OwnerState.INCOMPATIBLE,
                reason="the running daemon is older than this build",
            ),
            True,
        )

    verdict = reach(attachment)
    if verdict is Reach.ANSWERED:
        return lookup, False

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
    )


def _ask_to_stand_down(attachment: Attachment) -> None:
    """Ask a superseded owner to give up the browser.

    Best effort, and deliberately so. The owner may already be gone, may be an
    older build with no such route, or may be busy finishing a call. In every one
    of those cases the caller's next step is the same: try the lock, and wait if
    somebody still holds it. Raising here would turn a routine upgrade into a
    client that fails to start.

    Whether the owner *complied* is never assumed. The lock is what says the
    browser is free, and this function never claims otherwise.
    """

    descriptor = attachment.descriptor
    url = f"http://{descriptor.host if ':' not in descriptor.host else f'[{descriptor.host}]'}:{descriptor.port}{daemon_owner.STAND_DOWN_PATH}"
    try:
        with daemon_owner.direct_http_client(timeout=_STAND_DOWN_SECONDS) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {attachment.token}"},
            )
    except Exception:
        logger.debug("The stale daemon could not be asked to stand down", exc_info=True)
        return
    if response.status_code == 404:
        # An owner from before this route existed. It will not hand over on
        # request, so the honest outcome is that the upgrade takes effect when
        # that owner exits for its own reasons.
        logger.info(
            "The running daemon is too old to hand the browser over on request; "
            "it will be replaced when it stops"
        )
        return
    logger.debug("Stand-down request answered with %s", response.status_code)


def _reachable(attachment: Attachment) -> Reach:
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
                await client.ping()
        except Exception:
            # Answered, and wrongly: a refused connection, a rejected token, a
            # stranger on the port. All of them say this descriptor is not an
            # owner to attach to, and saying so again would not change it.
            logger.debug("The published daemon did not answer", exc_info=True)
            return Reach.REFUSED
        return Reach.ANSWERED

    try:
        return asyncio.run(asyncio.wait_for(ask(), _REACHABLE_SECONDS))
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

    #: The commit record may have reached the lock holder, which settles its own
    #: lifetime and must not be killed by the parent.
    UNCERTAIN = "uncertain"


class _BootstrapReport:
    """Collect one bounded, fixed diagnostic from the child's bootstrap pipe."""

    def __init__(self, stream: BinaryIO | None) -> None:
        self._result: queue.Queue[str | None] = queue.Queue(maxsize=1)
        if stream is None:
            self._result.put(None)
            return

        def collect() -> None:
            code: str | None = None
            remaining = 4096
            try:
                with stream:
                    while line := stream.readline(512):
                        if remaining <= 0:
                            continue
                        sample = line[:remaining]
                        remaining -= len(sample)
                        text = sample.decode("ascii", "ignore").strip()
                        prefix = f"{daemon_owner.BOOTSTRAP_PREFIX} "
                        if text.startswith(prefix):
                            candidate = text.removeprefix(prefix)
                            if candidate in {
                                daemon_owner.BOOTSTRAP_CONFIGURATION,
                                daemon_owner.BOOTSTRAP_STATE,
                                daemon_owner.BOOTSTRAP_LOG,
                                daemon_owner.BOOTSTRAP_ATTACHED,
                            }:
                                code = candidate
            except OSError:
                pass
            self._result.put(code)

        threading.Thread(
            target=collect,
            name="daemon-bootstrap",
            daemon=True,
        ).start()

    def read(self, *, timeout: float = _FAILURE_VERDICT_SECONDS) -> str | None:
        """Return the fixed record without letting diagnostics pin fallback."""
        try:
            return self._result.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None


def _report_child_failure(report: _BootstrapReport) -> None:
    """Log the most actionable safe diagnosis available from the child."""
    code = report.read()
    if code == daemon_owner.BOOTSTRAP_CONFIGURATION:
        logger.warning("The daemon rejected its startup configuration")
    elif code == daemon_owner.BOOTSTRAP_STATE:
        logger.warning("The daemon could not resolve its profile state")
    elif code == daemon_owner.BOOTSTRAP_LOG:
        logger.warning("The daemon log could not be opened")
    elif code == daemon_owner.BOOTSTRAP_ATTACHED:
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
        lock_fd=None,
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


def _spawn(
    auth_root: Path,
    config: AppConfig,
    *,
    lock_fd: int | None,
    timeout: float,
    on_spawned: Callable[[], None] | None = None,
    inspector: _DescriptorInspector | None = None,
) -> _Started:
    """Start a detached owner and atomically commit its prepared descriptor.

    Standard error carries one bounded bootstrap diagnosis until the child opens
    its own log. Every operation against state storage remains inside the process
    that a timed-out frontend can kill.
    """
    # Bound before the child exists, because its address travels in the
    # configuration record and a bind that fails here has no child to stop.
    control = ControlListener.open()
    windows_job = WindowsJob.named("owner") if os.name == "nt" else None
    try:
        nonce = release_nonce() if windows_job is not None else None
        target = _spawn_command(
            lock_fd=lock_fd,
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
            pass_fds=() if lock_fd is None else (lock_fd,),
            start_new_session=True,
            close_fds=True,
            creationflags=_detachment_flags(),
        )
    except BaseException:
        control.close()
        if windows_job is not None:
            windows_job.close()
        raise

    try:
        # Created only after Popen returns and carried only by the existing
        # configuration pipe. Startup output written before that delivery cannot
        # know the token the parent will accept.
        handshake_nonce = new_nonce()
        # Inside the same handler as the spawn: the diagnosis starts a thread,
        # and a host that has run out of them would otherwise leave this child
        # running with nothing holding it, waiting out its handover timeout for
        # a configuration record no one is left to send.
        bootstrap = _BootstrapReport(getattr(child, "stderr", None))
    except BaseException:
        _stop_child(child, windows_job=windows_job, assigned=False)
        control.close()
        if windows_job is not None:
            windows_job.close()
        raise
    prepared_instance_id: str | None = None
    #: Whether the child may already own itself, either because a commit record
    #: may have reached it or because it reported a publication a predecessor
    #: protocol performs on its own authority. Past this, no exception licenses a
    #: kill.
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
        assert child.stdin is not None and child.stdout is not None
        # Before the configuration record, and therefore before the child can
        # reach the port at all. The rendezvous has been bound since before the
        # spawn, so anything running as this account may already be sitting in
        # its queue; a parent that only accepts once the child has reported a
        # prepared generation waits for a report the child cannot make, because
        # the child is blocked connecting into a queue nobody is emptying.
        control.start_accepting(nonce=handshake_nonce, timeout=timeout)
        if windows_job is not None and nonce is not None:
            windows_job.assign_popen(child)
            assigned = True
            # No owner code exists until the parent owns the Job.
            release_windows_gate(child.stdin, nonce)
        if on_spawned is not None:
            try:
                on_spawned()
            except BaseException:
                stop_child()
                raise
        started = time.monotonic()
        try:
            _hand_over_config(
                child,
                config,
                handshake_nonce=handshake_nonce,
                control=control,
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "The daemon never read its configuration; stopping it so the "
                "lock is not held by a process that cannot serve"
            )
            verdict = _await_prepared(
                child,
                handshake_nonce=handshake_nonce,
                timeout=_FAILURE_VERDICT_SECONDS,
            )
            if isinstance(verdict, str):
                prepared_instance_id = verdict
            stop_child()
            _report_child_failure(bootstrap)
            return _Started.RETRY if verdict is _Started.RETRY else _Started.NO
        except Exception:
            logger.warning(
                "The daemon could not receive its configuration", exc_info=True
            )
            verdict = _await_prepared(
                child,
                handshake_nonce=handshake_nonce,
                timeout=_FAILURE_VERDICT_SECONDS,
            )
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
                child,
                handshake_nonce=handshake_nonce,
                timeout=timeout - (time.monotonic() - started),
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
        if verdict is _Started.YES:
            # A predecessor owner, started by this current frontend after the
            # package was replaced on disk. Its protocol has no commit record and
            # no prepared generation to validate: it published on its own
            # authority and says so, and this process no longer gets to stop it.
            # The election's own read of the published descriptor is what decides
            # whether that owner is usable.
            logger.info("The daemon published on the predecessor startup protocol")
            owner_may_be_adopted = True
            return _Started.YES

        if not isinstance(verdict, str):  # pragma: no cover - handled above
            raise AssertionError(f"Unexpected startup verdict: {verdict}")
        prepared_instance_id = verdict
        prepared_deadline = time.monotonic() + min(
            _PREPARED_READ_SECONDS,
            max(timeout - (time.monotonic() - started), 0.0),
        )
        try:
            # Already collected: the child attaches before it prepares anything
            # and the queue has been drained since before the child could reach
            # it, so a prepared generation means the authenticated connection is
            # in hand. Still required before commit, because nothing may be
            # authorized until a peer has presented the post-spawn nonce.
            control.attached_within(timeout=prepared_deadline - time.monotonic())
        except (TimeoutError, OSError):
            # Nothing was authorized, so this child cannot publish and stopping it
            # frees the lock for the next attempt. Not terminal: another election
            # pass may reach a child that can be authorized.
            logger.warning(
                "The daemon did not attach to its control channel", exc_info=True
            )
            stop_child()
            return _Started.NO
        except BaseException:
            stop_child()
            raise

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
            # No commit record was sent, so this child cannot become canonical.
            # Waiting for a verdict would leave it holding the lock until its own
            # authorization timeout expires.
            stop_child()
            return _Started.NO
        except Exception:
            logger.warning("The daemon prepared unusable startup state", exc_info=True)
            verdict = _await_committed(
                child,
                handshake_nonce=handshake_nonce,
                timeout=_FAILURE_VERDICT_SECONDS,
            )
            stop_child()
            return _Started.RETRY if verdict is _Started.RETRY else _Started.ABORTED
        except BaseException:
            stop_child()
            raise

        # The child owns the daemon lock and therefore owns publication. Once the
        # commit record may have reached it, no exception authorizes a kill: the
        # child either commits and serves or observes EOF and discards its state.
        owner_may_be_adopted = True
        try:
            _send_commit(control, handshake_nonce=handshake_nonce)
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
            child,
            handshake_nonce=handshake_nonce,
            timeout=timeout - (time.monotonic() - started),
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
                # The exact COMMIT frame precedes owner adoption. Closing this copy
                # cannot revoke an adopted owner, and kills one that never adopted.
                release_job()
            else:
                stop_child()
        # End of file aborts a prepared child that never received the commit
        # record. After that record it is only a lease: the lock holder settles
        # publication. The configuration pipe was closed with its record, so a
        # child on a predecessor protocol has long since been released.
        control.close()
        # Only a spawn that failed before the handover still holds this. Past
        # it the writer owns the stream and this is ``None``.
        if child.stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                child.stdin.close()
        _release_handshake(child)
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
            windows_job.wait_until_empty(timeout=max(deadline - time.monotonic(), 0.0))
        else:
            # Before assignment EOF is the only contained stop. If the isolated
            # gate does not accept it, stop and reap that direct child explicitly.
            # Past the handover this is ``None``: the kill below is bounded, a
            # close racing the writer's blocked write is not.
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


def _hand_over_config(
    child: subprocess.Popen[bytes],
    config: AppConfig,
    *,
    handshake_nonce: str,
    control: ControlListener,
    timeout: float,
) -> None:
    """Write one config record and end the pipe on it.

    Closed with the record rather than held open, and that is the whole of the
    rollback fix. Every owner older than the commit protocol frames its
    configuration by reading to end of file, so a pipe kept open for a later
    commit record leaves such a child blocked in its very first read while this
    process waits for a verdict it will never send. Authorization is on
    *control* instead, which no predecessor looks for and none of them needs.

    The close runs on the writing thread, so a pipe that cannot be closed is
    bounded by the same wait as one that cannot be written. The stream leaves
    ``Popen`` to get there, so the writer owns it from here: a blocked ``write``
    holds the ``BufferedWriter`` lock ``close`` needs, and a cross-thread close
    would wait for it without a timeout of its own. Nothing after the record
    needs the pipe, because authorization travels on *control*.
    """
    stream, child.stdin = child.stdin, None
    assert stream is not None
    endpoint = daemon_config.ControlEndpoint(control.host, control.port)
    payload = (
        daemon_config.encode_handover(config, handshake_nonce, endpoint).encode()
        + b"\n"
    )

    done: queue.Queue[BaseException | None] = queue.Queue()

    def hand_over() -> None:
        try:
            stream.write(payload)
            stream.flush()
            stream.close()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            with contextlib.suppress(OSError, ValueError):
                stream.close()
            done.put(exc)
        else:
            done.put(None)

    writer = threading.Thread(target=hand_over, name="daemon-config", daemon=True)
    writer.start()
    try:
        failure = done.get(timeout=max(timeout, 0.0))
    except queue.Empty:
        raise TimeoutError(
            "The daemon did not read its configuration in time"
        ) from None
    if failure is not None:
        raise failure


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


def _send_commit(control: ControlListener, *, handshake_nonce: str) -> None:
    """Tell the lock-holding child to publish its validated generation."""
    control.send(f"{daemon_owner.HANDSHAKE} {handshake_nonce} {daemon_owner.COMMIT}\n")


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
    # After the commit record may have arrived, a dead child is not proof of an
    # uncommitted generation. NFS may report a failed rename and keep returning a
    # stale canonical entry until after that child exits. The next successful
    # owner removes abandoned pending files and superseded tokens while holding
    # the lock.
    return _Started.UNCERTAIN


def _await_committed(
    child: subprocess.Popen[bytes], *, handshake_nonce: str, timeout: float
) -> _Started:
    """Wait for the lock holder's authenticated final commit verdict."""
    verdict = _read_owner_verdict(
        child,
        handshake_nonce=handshake_nonce,
        timeout=timeout,
        thread_name="daemon-commit",
    )
    if isinstance(verdict, _Started):
        return verdict

    status, _ = verdict
    if status in (daemon_owner.READY, daemon_owner.COMMITTED):
        return _Started.YES
    if status == daemon_owner.ABORTED:
        return _Started.ABORTED
    if status == daemon_owner.RETRY:
        return _Started.RETRY
    if status == daemon_owner.UNCERTAIN:
        return _Started.UNCERTAIN
    return _Started.NO


def _spawn_command(*, lock_fd: int | None, job_name: str | None = None) -> list[str]:
    """How the owner is invoked.

    ``-P`` keeps the working directory off ``sys.path`` while preserving the
    interpreter's user site, where a supported ``pip install --user`` may have put
    this package. An explicitly isolated frontend is different: weakening its
    ``-I``, ``-s`` or ``-E`` mode would restore startup code it refused before the
    configuration secrets cross stdin. The child environment drops ``PYTHONPATH``
    separately, so a workspace cannot put a shadow
    ``linkedin_mcp_server/daemon_owner.py`` back in front and receive the inherited
    lock descriptor or configuration secrets.
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
        "linkedin_mcp_server.daemon_owner",
    ]
    if lock_fd is not None:
        command += ["--lock-fd", str(lock_fd)]
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

    ``daemon_config`` hands the configuration over on standard input precisely
    so that a password does not sit in a readable environment. That is only half
    the job while the child inherits the frontend's environment unchanged: the
    documented way to configure a proxy is ``PROXY_PASSWORD``
    (``config/loaders.py:107-108``), so the owner would hold the raw value for
    as long as it runs, which is far longer than the process it came from.

    Dropped rather than emptied. An empty ``PROXY_USERNAME`` is meaningful to
    the configuration parser (``daemon_descriptor.py:480``), and the owner is
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


class _ActiveRead:
    """Who may close a handshake pipe, so that exactly one side does.

    The reader owns the close while its read is in flight, the caller once that
    read is over. Both answers are given under ``_active_reads_lock``.
    """

    def __init__(self) -> None:
        self._reading = True
        self._abandoned = False

    def finish(self, stream: Any) -> None:
        """End the read, closing the pipe if the caller has already left it."""
        with _active_reads_lock:
            self._reading = False
            abandoned = self._abandoned
            if _active_reads.get(stream) is self:
                del _active_reads[stream]
        if abandoned:
            _close_handshake_stream(stream)

    def abandon(self) -> bool:
        """Whether the caller may close: true once the read is over."""
        with _active_reads_lock:
            if not self._reading:
                return True
            self._abandoned = True
            return False


#: The read currently on a handshake pipe, keyed by that pipe and never more than
#: one deep: a second read starts only after the first one has returned.
_active_reads: weakref.WeakKeyDictionary[Any, _ActiveRead] = weakref.WeakKeyDictionary()
_active_reads_lock = threading.Lock()


def _close_handshake_stream(stream: Any) -> None:
    """Close the descriptor rather than the buffer, which a parked reader holds.

    ``BufferedReader.close()`` waits for that reader's I/O lock, so it blocks for
    as long as the child stays silent: measured at 29.27 of thirty seconds.
    ``detach()`` first, or the raw object's finalizer prints ``Bad file
    descriptor`` from a context nothing can catch.
    """
    # `Popen.stdout` is declared as a plain `IO`, which has no `detach`; the
    # object is a BufferedReader in practice, and the fallback covers a caller
    # that handed us something else rather than assuming.
    detach = getattr(stream, "detach", None)
    try:
        raw = detach() if callable(detach) else stream
        raw.close()
    except (OSError, ValueError):
        # Already gone, which is the ordinary case for a child that exited.
        logger.debug("The handshake pipe was already closed", exc_info=True)


def _release_handshake(child: subprocess.Popen[bytes]) -> None:
    """Stop listening to the child, without waiting for it to stop talking.

    Closing the descriptor under a parked reader is what makes this prompt on
    POSIX: the pending read fails and the thread unwinds. Windows closes through
    the UCRT, which waits for the same handle lock the blocked ``ReadFile``
    holds, so after a commit whose verdict timed out the close would wait for an
    owner that keeps stdout open for as long as it runs. There a read still in
    flight keeps its pipe and closes it itself at EOF, from the daemon thread
    that is already parked on it. The pipe leaves ``Popen`` either way, so
    nothing closes it behind that reader's back.
    """
    stream, child.stdout = child.stdout, None
    if stream is None:
        return
    with _active_reads_lock:
        active = _active_reads.get(stream)
    if _IS_WINDOWS and active is not None and not active.abandon():
        logger.debug("The handshake pipe stays with its reader until end of file")
        return
    _close_handshake_stream(stream)


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


_OwnerVerdict = tuple[str, str | None]


def _reported_owner_verdict(frame: bytes, handshake_nonce: str) -> _OwnerVerdict | None:
    marker = f"{daemon_owner.HANDSHAKE} {handshake_nonce} ".encode("ascii")
    if not frame.startswith(marker) or not frame.endswith(b"\n"):
        return None
    try:
        payload = frame[len(marker) : -1].decode("ascii")
    except UnicodeDecodeError:
        return None
    if payload in (
        daemon_owner.READY,
        daemon_owner.COMMITTED,
        daemon_owner.FAILED,
        daemon_owner.ABORTED,
        daemon_owner.RETRY,
        daemon_owner.UNCERTAIN,
    ):
        return payload, None
    prefix = f"{daemon_owner.PREPARED} "
    if payload.startswith(prefix):
        instance_id = payload.removeprefix(prefix)
        if instance_id and not any(character.isspace() for character in instance_id):
            return daemon_owner.PREPARED, instance_id
    return None


def _read_owner_verdict(
    child: subprocess.Popen[bytes],
    *,
    handshake_nonce: str,
    timeout: float,
    thread_name: str,
) -> _OwnerVerdict | _Started:
    """Read one authenticated owner record within the caller's deadline.

    Startup hooks may write arbitrary bytes first, including plain status-shaped
    lines and an unterminated prefix. They ran before the post-spawn nonce existed,
    so bounded scanning can discard them and extract only the exact owner frame.
    """
    stream = child.stdout
    assert stream is not None
    verdicts: queue.Queue[_OwnerVerdict | None] = queue.Queue()
    active = _ActiveRead()
    with _active_reads_lock:
        _active_reads[stream] = active

    def collect() -> None:
        try:
            marker = f"{daemon_owner.HANDSHAKE} {handshake_nonce} ".encode("ascii")
            reported = read_authenticated_status(
                cast(BinaryIO, stream),
                marker=marker,
                parse=lambda frame: _reported_owner_verdict(frame, handshake_nonce),
            )
            verdict = reported[1] if reported is not None else None
        except OSError:  # pragma: no cover - the stream closed under us
            verdict = None
        finally:
            # Before the verdict is handed over, so a caller that reads one and
            # then releases the pipe finds a read that is over and closes it.
            active.finish(stream)
        verdicts.put(verdict)

    # A daemon thread: if the child neither answers nor exits, the frontend must
    # still be able to shut down. It holds only this pipe, which it closes itself
    # when the caller released it mid-read.
    reader = threading.Thread(target=collect, name=thread_name, daemon=True)
    reader.start()
    try:
        verdict = verdicts.get(timeout=max(timeout, 0.0))
    except queue.Empty:
        return _Started.STILL_TRYING
    return _Started.NO if verdict is None else verdict


def _await_prepared(
    child: subprocess.Popen[bytes], *, handshake_nonce: str, timeout: float
) -> str | _Started:
    """Wait for a prepared generation id, a publication, failure, or silence.

    ``READY`` is the one verdict that is not about this protocol at all. It comes
    from an owner whose build predates the prepare/commit boundary, started by
    this current frontend because the package on disk was replaced under it, and
    it means the descriptor is already published. There is nothing left to
    authorize and nothing this process may still stop.
    """
    verdict = _read_owner_verdict(
        child,
        handshake_nonce=handshake_nonce,
        timeout=timeout,
        thread_name="daemon-handshake",
    )
    if isinstance(verdict, _Started):
        if verdict is _Started.NO:
            logger.info("The daemon exited before it prepared an endpoint")
        return verdict

    status, instance_id = verdict
    if status == daemon_owner.PREPARED and instance_id is not None:
        return instance_id
    if status == daemon_owner.READY:
        return _Started.YES
    if status == daemon_owner.FAILED:
        logger.info("The daemon reported that it could not start")
        return _Started.NO
    if status == daemon_owner.ABORTED:
        logger.info("The daemon reported a terminal startup failure")
        return _Started.ABORTED
    if status == daemon_owner.RETRY:
        logger.info("The daemon released its startup position for another attempt")
        return _Started.RETRY
    return _Started.NO
