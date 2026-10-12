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

What to do next is decided in ``daemon.core``: the election loop in
:mod:`~linkedin_mcp_server.daemon.core.election`, each look at the published
owner in :mod:`~linkedin_mcp_server.daemon.core.lookup`, and each child start in
:mod:`~linkedin_mcp_server.daemon.core.spawn`. This module carries out their
effects, reads the clock and feeds back what came of each.
"""

from __future__ import annotations

import contextlib
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
from linkedin_mcp_server.daemon.core import election as election_core
from linkedin_mcp_server.daemon.core import lookup as lookup_core
from linkedin_mcp_server.daemon.core import spawn as spawn_core
from linkedin_mcp_server.daemon.core.lookup import Finding, Reach
from linkedin_mcp_server.daemon.core.spawn import Attempt, Started
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

    The loop is :mod:`linkedin_mcp_server.daemon.core.election`'s, including the
    separate budgets of active election and quiescent settlement.

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
    reach = connect or _reachable
    inspector = _DescriptorInspector(auth_root, profile, config)
    # A copy, never the caller's own set: `_live_lookup` adds to it.
    buried = set(buried)
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
                return ElectionOutcome(cast(OwnerLookup, lookup), started_owner=started)
            case election_core.Look(op=op, may_ask=may_ask, wait=wait, deadline=until):
                found, asked, observed = _live_lookup(
                    auth_root,
                    profile,
                    config,
                    reach,
                    buried,
                    inspector=inspector,
                    may_ask_for_turnover=may_ask,
                    wait_seconds=wait,
                    deadline=until,
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
                event = _start(auth_root, profile, config, op, timeout, inspector)
        election, effects = election_core.on_event(election, event)


def _start(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    op: int,
    timeout: float,
    inspector: _DescriptorInspector,
) -> election_core.Event:
    """Start one child for the election, and say how that ended."""
    try:
        inspector.require_fresh_inspection()
        attempt = _start_owner(
            auth_root, profile, config, timeout=timeout, inspector=inspector
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
    :mod:`linkedin_mcp_server.daemon.discovery` reads disk; this connects. What
    each reading calls for is :mod:`linkedin_mcp_server.daemon.core.lookup`'s
    decision.

    Reads stay on the caller's one inspector. A paced pass may re-read the
    canonical descriptor, but returns when that pass's wait ends so the active
    loop can try the lock again instead of spending its whole budget on reads.

    Returns the lookup, whether turnover was requested, and whether the lookup
    came from a completed descriptor read. The last flag lets settlement retain
    its last concrete observation when a bounded read times out. Generations the
    pass writes off are added to *buried*.
    """
    del auth_root, profile, config  # carried by the reusable inspector
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
                        event = lookup_core.Read(op, time.monotonic(), _reading(read))
                case lookup_core.Pause(op=op, seconds=seconds):
                    time.sleep(seconds)
                    event = lookup_core.Paused(op, time.monotonic())
                case lookup_core.Probe(op=op, timeout=timeout):
                    verdict = reach(_attachment(read), timeout)
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
    if because is lookup_core.Turnover.OLDER_BUILD:
        logger.info(
            "The running daemon is version %s and this build is %s; asking it "
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
        case Finding.OLDER_BUILD:
            return OwnerLookup(
                state=incompatible,
                reason="the running daemon is older than this build",
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


def _start_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    timeout: float,
    inspector: _DescriptorInspector | None = None,
) -> Attempt:
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
) -> Attempt:
    """Start one child that competes for the lock and reports the outcome."""
    return spawn_core.attempt_after(
        _spawn(
            auth_root,
            config,
            timeout=timeout,
            inspector=inspector,
        )
    )


def _refusal_before_spawn(config: AppConfig) -> Started | None:
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
        return Started.ABORTED
    if installed != running:
        logger.warning(
            "The installed package changed after this server started (%s, now "
            "%s); restart the MCP client to use the shared browser",
            running,
            installed,
        )
        return Started.ABORTED
    if not startup.fits(startup.CONFIG, config=daemon_config.encode(config)):
        logger.warning(
            "The configuration is too large to hand to a shared browser owner "
            "(the startup protocol carries at most %d bytes)",
            startup.MAX_FRAME_BYTES,
        )
        return Started.ABORTED
    return None


def _spawn(
    auth_root: Path,
    config: AppConfig,
    *,
    timeout: float,
    on_spawned: Callable[[], None] | None = None,
    inspector: _DescriptorInspector | None = None,
) -> Started:
    """Start a detached owner and atomically commit its prepared descriptor.

    The exchange itself is described in :mod:`linkedin_mcp_server.daemon.startup`,
    and what follows each step of it, including when the child may be stopped, in
    :mod:`linkedin_mcp_server.daemon.core.spawn`. Standard error carries one
    bounded bootstrap diagnosis until the child opens its own log. Every
    operation against state storage remains inside the process that a timed-out
    frontend can kill.
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
    return _Startup(
        auth_root,
        config,
        child,
        windows_job=windows_job,
        gate_nonce=nonce,
        handshake_nonce=handshake_nonce,
        bootstrap=bootstrap,
        inspector=inspector,
    ).run(timeout=timeout, on_spawned=on_spawned)


class _Startup:
    """One child's startup exchange, run as ``daemon.core.spawn`` decides.

    The core says what comes next and when the child may be stopped; this runs
    each step against the child and reports what came of it. Every attempt ends
    in the core's :func:`~linkedin_mcp_server.daemon.core.spawn.wind_down`,
    however it ended.
    """

    def __init__(
        self,
        auth_root: Path,
        config: AppConfig,
        child: subprocess.Popen[bytes],
        *,
        windows_job: WindowsJob | None,
        gate_nonce: str | None,
        handshake_nonce: str,
        bootstrap: _BootstrapReport,
        inspector: _DescriptorInspector | None,
    ) -> None:
        self._auth_root = auth_root
        self._config = config
        self._child = child
        self._job = windows_job
        self._gate_nonce = gate_nonce
        self._handshake_nonce = handshake_nonce
        self._bootstrap = bootstrap
        self._inspector = inspector
        self._channel: startup.FrontendChannel | None = None
        #: Whether the Job owns the child, which is how it must be stopped.
        self._assigned = False
        self._state = spawn_core.spawned(
            contained=windows_job is not None,
            budgets=spawn_core.Budgets(
                failure_verdict=_FAILURE_VERDICT_SECONDS,
                prepared_read=_PREPARED_READ_SECONDS,
            ),
        )

    def run(self, *, timeout: float, on_spawned: Callable[[], None] | None) -> Started:
        try:
            effects = self._decide(self._connect(timeout, on_spawned))
            while True:
                event: spawn_core.Event | None = None
                for effect in effects:
                    match effect:
                        case spawn_core.Finish(started=started):
                            return started
                        case spawn_core.StopChild(silent=silent):
                            self._stop(silent=silent)
                        case spawn_core.ReportFailure():
                            _report_child_failure(self._bootstrap)
                        case _:
                            event = self._carry_out(effect)
                if event is None:
                    raise AssertionError(
                        f"the spawn core left the start waiting on {effects}"
                    )
                effects = self._decide(event)
        finally:
            for effect in spawn_core.wind_down(self._state):
                self._wind_down(effect)

    def _decide(self, event: spawn_core.Event) -> list[spawn_core.Effect]:
        self._state, effects = spawn_core.on_event(self._state, event)
        return effects

    def _connect(
        self, timeout: float, on_spawned: Callable[[], None] | None
    ) -> spawn_core.Event:
        """Hand the child to its Job, open the channel and say when that was."""
        try:
            if self._job is not None and self._gate_nonce is not None:
                self._job.assign_popen(self._child)
                self._assigned = True
                # No owner code exists until the parent owns the Job.
                release_windows_gate(self._child.stdin, self._gate_nonce)
            # Only now, because until the gate is released its input is the one
            # thing that can stop it, and that close belongs to ``_stop_child``.
            self._channel = startup.FrontendChannel(self._child, self._handshake_nonce)
            if on_spawned is not None:
                on_spawned()
        except BaseException:
            self._interrupted(0)
            raise
        return spawn_core.Connected(time.monotonic(), timeout)

    def _carry_out(self, effect: spawn_core.Effect) -> spawn_core.Event:
        """Run one step until it completes, and say how.

        Failures a step expects become its own events. Anything else is an
        interruption, which the core answers before it propagates.
        """
        if not isinstance(effect, spawn_core.WAITS):
            raise AssertionError(f"not an effect that waits: {effect!r}")
        try:
            return self._step(effect)
        except BaseException:
            self._interrupted(effect.op)
            raise

    def _step(self, effect: spawn_core.Effect) -> spawn_core.Event:
        channel = self._channel
        assert channel is not None
        match effect:
            case spawn_core.Greet(op=op, timeout=timeout):
                refusal = _greet(channel, timeout=timeout)
                return spawn_core.Greeted(op, time.monotonic(), refusal)
            case spawn_core.HandOver(op=op, timeout=timeout):
                try:
                    _hand_over_config(channel, self._config, timeout=timeout)
                except TimeoutError:
                    logger.warning(
                        "The daemon never read its configuration; stopping it so "
                        "the lock is not held by a process that cannot serve"
                    )
                    return spawn_core.HandOverFailed(op, time.monotonic())
                except Exception:
                    logger.warning(
                        "The daemon could not receive its configuration", exc_info=True
                    )
                    return spawn_core.HandOverFailed(op, time.monotonic())
                return spawn_core.HandedOver(op, time.monotonic())
            case spawn_core.AwaitPrepared(op=op, timeout=timeout):
                answer = _await_prepared(channel, timeout=timeout)
                return spawn_core.Prepared(op, time.monotonic(), answer)
            case spawn_core.Validate(op=op, instance=instance, until=until):
                try:
                    profile = _resolve_profile_until(
                        Path(self._config.browser.user_data_dir),
                        timeout=until - time.monotonic(),
                    )
                    _validate_prepared_until(
                        self._auth_root,
                        instance,
                        profile=profile,
                        config=self._config,
                        timeout=until - time.monotonic(),
                    )
                except TimeoutError:
                    return spawn_core.ValidationTimedOut(op, time.monotonic())
                except Exception:
                    logger.warning(
                        "The daemon prepared unusable startup state", exc_info=True
                    )
                    return spawn_core.ValidationFailed(op, time.monotonic())
                return spawn_core.Validated(op, time.monotonic())
            case spawn_core.SendCommit(op=op, instance=instance, timeout=timeout):
                try:
                    _send_commit(channel, instance, timeout=timeout)
                except Exception:
                    logger.warning(
                        "The daemon commit request could not be delivered",
                        exc_info=True,
                    )
                    return spawn_core.CommitUndelivered(op, time.monotonic())
                return spawn_core.CommitSent(op, time.monotonic())
            case spawn_core.AwaitVerdict(op=op, timeout=timeout):
                verdict = _await_committed(channel, timeout=timeout)
                return spawn_core.Verdict(op, time.monotonic(), verdict)
            case spawn_core.SettleCommit(op=op, instance=instance, timeout=timeout):
                verdict = _settle_commit_result(
                    self._auth_root,
                    instance,
                    self._child,
                    timeout=timeout,
                    inspector=self._inspector,
                )
                return spawn_core.Settled(op, time.monotonic(), verdict)
        raise AssertionError(f"not an effect that waits: {effect!r}")

    def _interrupted(self, op: int) -> None:
        """Stop the child for an interruption, if the core says this one may."""
        for effect in self._decide(spawn_core.Interrupted(op)):
            if isinstance(effect, spawn_core.StopChild):
                self._stop(silent=effect.silent)

    def _stop(self, *, silent: bool) -> None:
        if silent:
            logger.warning(
                "The daemon did not prepare an endpoint; stopping it so the lock "
                "is not held by a process that never served"
            )
        try:
            _stop_child(self._child, windows_job=self._job, assigned=self._assigned)
        finally:
            if self._job is not None:
                self._job.close()

    def _wind_down(self, effect: spawn_core.Effect) -> None:
        match effect:
            case spawn_core.StopChild(silent=silent):
                self._stop(silent=silent)
            case spawn_core.ReleaseJob():
                if self._job is not None:
                    self._job.close()
            case spawn_core.HangUp():
                if self._channel is not None:
                    self._channel.close()
                else:
                    # The channel never took the pipes, so no thread is inside them.
                    for stream in (self._child.stdin, self._child.stdout):
                        if stream is not None:
                            with contextlib.suppress(OSError, ValueError):
                                stream.close()
                _reap(self._child)
            case spawn_core.DiscardPrepared(instance=instance):
                try:
                    _discard_prepared_until(
                        self._auth_root,
                        instance,
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


def _greet(channel: startup.FrontendChannel, *, timeout: float) -> Started | None:
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
        return Started.NO
    except (OSError, ValueError):
        logger.info("The daemon could not be greeted", exc_info=True)
        return Started.NO
    try:
        ack = startup.expect(answer, startup.ACK, nonce=channel.nonce)
    except startup.ProtocolError:
        logger.info("The daemon did not answer in the startup protocol")
        return Started.NO
    running = daemon_version.build_identity()
    if ack["protocol"] != startup.PROTOCOL or ack["build"] != running:
        logger.warning(
            "The daemon that started is build %s, not this server's %s; the "
            "installed package changed, so restart the MCP client to use the "
            "shared browser",
            ack["build"],
            running,
        )
        return Started.ABORTED
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
) -> Started:
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
        return Started.UNCERTAIN
    if committed_instance == instance_id:
        return Started.YES
    # After the COMMIT frame may have arrived, a dead child is not proof of an
    # uncommitted generation. NFS may report a failed rename and keep returning a
    # stale canonical entry until after that child exits. The next successful
    # owner removes abandoned pending files and superseded tokens while holding
    # the lock.
    return Started.UNCERTAIN


def _await_committed(channel: startup.FrontendChannel, *, timeout: float) -> Started:
    """Wait for the lock holder's final commit verdict."""
    try:
        answer = channel.receive(timeout=timeout)
    except TimeoutError:
        return Started.STILL_TRYING
    try:
        verdict = startup.expect(answer, startup.VERDICT, nonce=channel.nonce)
    except startup.ProtocolError:
        return Started.NO
    status = verdict["status"]
    if status == startup.COMMITTED:
        return Started.YES
    if status == startup.ABORTED:
        return Started.ABORTED
    if status == startup.RETRY:
        return Started.RETRY
    if status == startup.UNCERTAIN:
        return Started.UNCERTAIN
    return Started.NO


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
) -> str | Started:
    """Wait for a prepared generation id, a failure, or silence."""
    try:
        answer = channel.receive(timeout=timeout)
    except TimeoutError:
        return Started.STILL_TRYING
    if answer is None:
        logger.info("The daemon exited before it prepared an endpoint")
        return Started.NO
    try:
        if answer.get("type") == startup.PREPARED:
            prepared = startup.expect(answer, startup.PREPARED, nonce=channel.nonce)
            return cast(str, prepared["instance"])
        verdict = startup.expect(answer, startup.VERDICT, nonce=channel.nonce)
    except startup.ProtocolError:
        logger.info("The daemon answered outside the startup protocol")
        return Started.NO
    status = verdict["status"]
    if status == startup.FAILED:
        logger.info("The daemon reported that it could not start")
        return Started.NO
    if status == startup.ABORTED:
        logger.info("The daemon reported a terminal startup failure")
        return Started.ABORTED
    if status == startup.RETRY:
        logger.info("The daemon released its startup position for another attempt")
        return Started.RETRY
    return Started.NO
