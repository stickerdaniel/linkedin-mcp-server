"""Getting an owner started, from the side of the process that wants one.

The counterpart to :mod:`linkedin_mcp_server.daemon`, which only reads. This is
where a process acts: it takes the lock, starts a detached owner, and waits for
the endpoint to exist. It never becomes the owner itself, because it cannot —
``cli_main`` runs the stdio server blocking, so a process that served the owner's
HTTP could not also serve its own client.

**Election is two sides, and one function cannot be both.** The process that wins
the lock is not the process that serves. What that means splits by platform, and
the split is measured rather than stylistic:

*POSIX.* The frontend takes the lock first, then hands the descriptor to the
child (``DaemonLock.inheritable_copy``). Taking it first is what removes the
window: between releasing a lock and a child taking it, another client would see
the position free and start a second browser against the same profile.

*Windows.* A held lock cannot be handed over there at all — measured, 20 of 20
(``daemon_lock.py:54-71``) — so the frontend takes no lock and the child competes
for it. Every frontend then waits for whoever wins to publish. Trying to hand a
lock over anyway would produce a child that believes it owns a browser while the
lock sits free behind it.

**The parent must let go of the lock before it serves anything.** Both
descriptors refer to one locked open file description, so the lock lives as long
as *either* is open. A frontend that kept its original would keep the daemon lock
alive after the owner died, and every recovery afterwards would be locked out by
a process that is not the owner and does not know it is holding anything.
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
from dataclasses import dataclass
from pathlib import Path

from linkedin_mcp_server import (
    __version__,
    daemon_config,
    daemon_owner,
    daemon_version,
)
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import (
    Attachment,
    OwnerLookup,
    OwnerState,
    look_up_owner,
)
from linkedin_mcp_server.daemon_lock import DaemonLock, DaemonLockError

logger = logging.getLogger(__name__)

#: How long a frontend waits for an owner to become attachable before giving up
#: and reporting what it last saw. It is what bounds the delay a user sees when
#: something goes wrong rather than when it goes right.
#:
#: At least twice the owner's own startup allowance
#: (``daemon_owner._STARTUP_PROBE_SECONDS``), which a test enforces. A frontend
#: stops a child that has said nothing by the time this runs out, so an owner
#: permitted to take longer would be killed on a slow machine while still inside
#: its own rules. The remainder covers what is spent before the owner's clock
#: starts: handing the configuration over, and the lock attempts before that.
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
    reach = connect or _reachable
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

        try:
            attempt = _start_owner(auth_root, profile, config, timeout=remaining)
        except DaemonLockError:
            # The lock could not be used at all, which is not contention: a
            # filesystem without usable locking, or state this account cannot
            # write. Waiting would never resolve it.
            logger.warning("The daemon lock is unusable", exc_info=True)
            return ElectionOutcome(lookup, started_owner=started)
        except OSError:
            logger.warning("The daemon could not be started", exc_info=True)
            return ElectionOutcome(lookup, started_owner=started)

        if attempt is _Attempt.FAILED:
            # This process held the lock, started a child, and the child said it
            # could not serve. Nobody else is coming: the lock is free again and
            # a fresh attempt would fail the same way. Waiting out the deadline
            # here is what turned a broken owner into a client that hangs for
            # the better part of a minute before reporting nothing at all.
            logger.warning("The daemon could not start; see %s", _log_hint(auth_root))
            return ElectionOutcome(look(), started_owner=started)
        started = started or attempt is _Attempt.STARTED

        # A started owner has already published by the time it reports ready, so
        # this re-read normally succeeds at once. The wait is for the other
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
    lookup = look_up_owner(auth_root, profile, config, wait_seconds=wait_seconds)
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


def _log_hint(auth_root: Path) -> Path:
    """Where a user should look when an owner refused to start.

    The owner is detached, so its failure is not on anyone's terminal. Without
    this the whole diagnosis a user gets is that nothing happened.
    """
    return daemon_owner.daemon_log_path(auth_root)


class _Started(enum.Enum):
    """What the startup handshake said.

    Silence is a third answer while the wait is running, and ``_spawn`` resolves
    it rather than passing it on: a child that has said nothing by the end of
    the budget is stopped, so what reaches the caller is only ever "it serves"
    or "it does not". Leaving silence as an outcome is what let a child keep the
    inherited lock while never serving.
    """

    #: The endpoint answered and the descriptor is published.
    YES = "yes"

    #: The child could not serve, said so, died trying, or was stopped for
    #: having said nothing at all.
    NO = "no"

    #: Neither, within the budget — used only inside ``_spawn``, which decides
    #: what to do about it before returning.
    STILL_TRYING = "still_trying"


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

    #: The child this process started said it could not serve. Nothing is
    #: holding the lock on its behalf, so waiting on it specifically is delay.
    FAILED = "failed"


def _start_owner(
    auth_root: Path, profile: Path, config: AppConfig, *, timeout: float
) -> _Attempt:
    """Get an owner running, if this process is the one that may."""
    del profile  # the owner derives it from the configuration it is handed
    if _hands_over_locks():
        return _start_holding_the_lock(auth_root, config, timeout=timeout)
    return _start_contending_for_the_lock(auth_root, config, timeout=timeout)


def _hands_over_locks() -> bool:
    """Whether a held lock can be given to a child on this platform.

    Read from the lock module rather than from ``os.name`` again, so the
    measurement that established it lives in one place.
    """
    from linkedin_mcp_server import daemon_lock

    return daemon_lock._INHERITED_LOCKS_TRANSFER


def _start_holding_the_lock(
    auth_root: Path, config: AppConfig, *, timeout: float
) -> _Attempt:
    """POSIX: take the lock, hand it over, then let go of it entirely."""
    lock = DaemonLock(auth_root)
    if not lock.try_acquire():
        return _Attempt.CONTENDED

    outcome = _Attempt.FAILED
    try:
        duplicate = lock.inheritable_copy()
        try:
            if _spawn(auth_root, config, lock_fd=duplicate, timeout=timeout) is (
                _Started.YES
            ):
                outcome = _Attempt.STARTED
        finally:
            # Closed whether or not the child got going. It shares the kernel
            # lock, so a leaked copy would hold the daemon lock for this
            # frontend's whole life with nothing able to release it.
            with contextlib.suppress(OSError):
                os.close(duplicate)
    finally:
        # Released in every case, and this is the load-bearing line. The child
        # holds the lock through its own copy by now; this descriptor is the
        # parent's share of the same open file description, and keeping it would
        # mean the lock outlives the owner. Measured on this tree: with the
        # parent's copy left open, killing the owner freed nothing and no
        # replacement could be elected until the frontend exited too.
        lock.release()

    return outcome


def _start_contending_for_the_lock(
    auth_root: Path, config: AppConfig, *, timeout: float
) -> _Attempt:
    """Windows: start a child that competes for the lock itself.

    The frontend takes no lock here, so it cannot tell "my child lost the race"
    from "my child could not serve": both arrive as a failed handshake. So a
    failure here is reported as contention, which costs a wait when nothing is
    coming and avoids giving up while a rival owner is still starting. The
    frontend on this platform has no better information to act on.
    """
    if _spawn(auth_root, config, lock_fd=None, timeout=timeout) is _Started.YES:
        return _Attempt.STARTED
    return _Attempt.CONTENDED


def _spawn(
    auth_root: Path, config: AppConfig, *, lock_fd: int | None, timeout: float
) -> _Started:
    """Start the detached owner and wait for its ready handshake.

    The owner has to outlive the client that caused it to start, or the whole
    feature degrades to a slower version of what it replaces. ``start_new_session``
    is what buys that: the child leads its own session and process group, so a
    signal aimed at the client's group does not reach it.

    Measured on this tree rather than assumed, because the issue claims otherwise
    — that only a double-forked child survives and ``start_new_session`` is not
    enough. On macOS it is: after ``SIGKILL`` to the frontend's entire process
    group, the owner was still running and had reparented to pid 1. A double fork
    would add a layer whose only job is to be exited, so it is not done here.

    Not measured: Windows, and Linux under a supervisor that kills a whole
    cgroup rather than a process group. The second is the one that could still
    take the owner down, and it is worth revisiting if a Linux user reports the
    daemon dying with their client.
    """
    log_path = daemon_owner.daemon_log_path(auth_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = _spawn_command(lock_fd=lock_fd)

    # Opened append, so a restart adds to the record rather than erasing the
    # reason the last owner died.
    with open(log_path, "a", encoding="utf-8") as log:
        child = subprocess.Popen(
            command,
            env=_owner_environment(),
            stdin=subprocess.PIPE,
            # The handshake channel. Not a separate inherited descriptor,
            # because ``pass_fds`` is POSIX-only (``subprocess.py:1464``) and
            # this path has to work on Windows, where the child competes for the
            # lock and the frontend has nothing *but* the handshake to go on.
            stdout=subprocess.PIPE,
            # To the log file from the first instruction, so an interpreter that
            # dies during imports still leaves its traceback somewhere. A
            # detached process has no terminal to write it to.
            stderr=log,
            # Empty on Windows, which refuses the argument outright. The lock is
            # only ever handed over where that works.
            pass_fds=() if lock_fd is None else (lock_fd,),
            # Its own session, so a signal aimed at the client's process group
            # does not take the owner with it. POSIX-only, which is why the
            # Windows equivalent is passed separately below rather than assumed.
            start_new_session=True,
            close_fds=True,
            # Zero on POSIX, where `subprocess` ignores it, and the real
            # detachment on Windows, where `start_new_session` is what gets
            # ignored. Passed positionally rather than unpacked from a mapping,
            # which would defeat the overload the type checker resolves against.
            creationflags=_detachment_flags(),
        )

    try:
        assert child.stdin is not None and child.stdout is not None
        started = time.monotonic()
        try:
            _hand_over_config(child, config, timeout=timeout)
        except TimeoutError:
            # Killed rather than left to itself, and the difference is a wedge
            # that never heals. A child still waiting on its configuration is
            # inside ``sys.stdin.read()``, which comes *before* it adopts the
            # lock — but on POSIX it already inherited the descriptor, so the
            # kernel lock is alive through it. Walking away leaves that child
            # holding the daemon lock forever while never serving anything, and
            # every later election contends against it.
            #
            # Safe precisely because of that ordering: a child that has not
            # finished reading cannot have reached ``_take_lock``, so nothing is
            # being interrupted mid-adoption. Waiting longer is not an option
            # either, since the thing being waited on is a process that is not
            # reading.
            logger.warning(
                "The daemon never read its configuration; stopping it so the "
                "lock is not held by a process that cannot serve"
            )
            _stop_child(child)
            return _Started.NO
        # One budget across both halves. Handing the configuration over and
        # waiting for the verdict are two ways of waiting on the same child, and
        # giving each the full budget would let a slow one spend twice what the
        # caller allowed.
        verdict = _await_ready(child, timeout=timeout - (time.monotonic() - started))
        if verdict is _Started.STILL_TRYING:
            # Stopped, not left to itself, and this is the last of the lock
            # wedges. "Still trying" reads as generous — the child may yet come
            # up — but by now it has had the whole budget and said nothing, and
            # on POSIX it holds the inherited lock descriptor. Left alone it
            # keeps that lock while never serving, and every later election
            # contends against a process that will never publish.
            #
            # Measured with an ordinary configuration and a child that only
            # sleeps: the lock was still held afterwards. The kill path added
            # for the configuration timeout only covered the case where the
            # *write* blocked, which needs a configuration large enough to fill
            # a pipe; this is the same wedge reached by the ordinary route.
            #
            # Safe for the same reason: an owner opens no browser before it
            # answers, so nothing is interrupted mid-flight. And on POSIX the
            # frontend still holds its own lock until this returns, so the
            # position cannot be taken by anyone else in between.
            logger.warning(
                "The daemon did not finish starting; stopping it so the lock is "
                "not held by a process that never served"
            )
            _stop_child(child)
            return _Started.NO
        return verdict
    finally:
        _release_handshake(child)
        _reap(child)


#: How long to wait for a killed child to be collected. Short by design: this
#: follows ``SIGKILL``, which the process cannot decline, so anything but a
#: prompt exit means the process is stuck in the kernel and no amount of waiting
#: here will change that.
_STOP_CHILD_SECONDS = 2.0


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    """End a child that never took its configuration, and collect it.

    Killed outright rather than asked politely first. The usual courtesy buys a
    process time to clean up, and this one has nothing to clean up: it never
    read its configuration, so it has opened no browser and holds no state
    beyond the lock descriptor it inherited and must give back.

    The courtesy is not free either. A ``SIGTERM`` grace period is time added
    *after* the caller's budget is already spent, so a child that ignores the
    signal turns a half-second election into five and a half, and the documented
    ceiling stops being a ceiling.

    The wait is what makes "the lock is free" true before this returns: a
    signalled process has not necessarily been reaped, and until it is, the
    descriptor may still be open.
    """
    with contextlib.suppress(OSError):
        child.kill()
    try:
        child.wait(timeout=_STOP_CHILD_SECONDS)
    except subprocess.TimeoutExpired:
        # Uninterruptible sleep, or a kernel that has not finished with it.
        # Reported rather than waited out, since the caller has a deadline and
        # this is not something a longer wait resolves.
        logger.warning(
            "The daemon has not exited after being killed; the profile may stay "
            "locked until it does"
        )


def _hand_over_config(
    child: subprocess.Popen[bytes], config: AppConfig, *, timeout: float
) -> None:
    """Write the configuration to the child, without waiting on it forever.

    The obvious ``child.stdin.write(...)`` blocks once the pipe buffer is full,
    and the buffer is small — 64 KiB on Linux, less on some platforms — while
    the configuration has no size limit at all: ``user_agent``, ``proxy_bypass``
    and the paths are free-form strings. A child that neither reads nor exits
    therefore blocks this write indefinitely, *before* the handshake timeout is
    ever reached. Reproduced with a 10 MiB user agent and a child that only
    sleeps: the outer process timeout fired and the wait below was never
    entered. Both processes hold the daemon lock while that happens.

    Written on a thread for the same reason the verdict is read on one: it is
    the one mechanism that bounds a blocking pipe operation on every platform.
    The thread is a daemon, so a write that never completes cannot keep the
    frontend from exiting, and the descriptor it holds is closed by the caller.
    """
    stream = child.stdin
    assert stream is not None
    payload = daemon_config.encode(config).encode()

    done: queue.Queue[BaseException | None] = queue.Queue()

    def hand_over() -> None:
        try:
            with stream:
                stream.write(payload)
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
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


def _spawn_command(*, lock_fd: int | None) -> list[str]:
    """How the owner is invoked.

    ``-I`` is isolated mode: it keeps the working directory *and* ``PYTHONPATH``
    off ``sys.path``. Without it, a directory containing
    ``linkedin_mcp_server/daemon_owner.py`` is imported in preference to the
    installed package, and an MCP client started in such a workspace would hand
    that code the inherited lock descriptor and the whole configuration on
    standard input, ``proxy_password`` included.

    ``-P`` alone is not enough, which is worth stating because it is the obvious
    choice and it looks sufficient: it drops the implicit working directory and
    leaves ``PYTHONPATH`` in force, so the very common ``PYTHONPATH=.`` puts the
    workspace back at the front. Both were measured from a prepared directory
    with this project's own interpreter — ``-P`` loaded the local file, ``-I``
    loaded the installed module.

    That is also what makes ``daemon_config``'s "both ends are the same
    installation" true rather than hopeful. Isolated mode is safe for the owner
    because it needs nothing from the environment's import configuration: it is
    the same interpreter and the same installation, and everything it is
    configured with arrives on standard input.
    """
    command = [sys.executable, "-I", "-m", "linkedin_mcp_server.daemon_owner"]
    if lock_fd is not None:
        command += ["--lock-fd", str(lock_fd)]
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
    ``Ctrl+Break``, and ``DETACHED_PROCESS`` gives it no console at all, which
    is right for a process whose output already goes to a log file.

    Unmeasured, unlike the POSIX side, and said so where it matters: nothing in
    this repository runs Windows outside CI, and a job object that kills its
    tree ignores both flags. This is the documented mechanism rather than a
    verified outcome.
    """
    if os.name != "nt":
        return 0
    return (  # pragma: no cover - exercised on the Windows runner
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    )


def _release_handshake(child: subprocess.Popen[bytes]) -> None:
    """Stop listening to the child, without waiting for it to stop talking.

    ``child.stdout.close()`` is the obvious call and it is a trap. The reader
    thread is parked inside iteration holding the buffered reader's I/O lock,
    and ``BufferedReader.close()`` waits for that lock — so closing here blocks
    for exactly as long as the child stays silent. Measured: a child that said
    nothing for thirty seconds made this call block 29.27 of them, and one that
    neither answers nor exits would block forever. That is the timeout above
    being handed straight back, in the one case it exists for.

    So the *descriptor* is closed instead. That is not held by the reader
    thread: the pending read fails, the thread unwinds, and the caller is free.

    ``detach()`` first, so the buffer gives up the descriptor instead of trying
    to close it again later, and then close the raw file object it hands back
    rather than the bare number. Closing the number directly leaves that raw
    object owning a descriptor that no longer exists, and its finalizer prints
    ``Bad file descriptor`` from a context nothing can catch. Both variants were
    tried; this is the one that is quiet.
    """
    stream, child.stdout = child.stdout, None
    if stream is None:
        return
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


def _await_ready(child: subprocess.Popen[bytes], *, timeout: float) -> _Started:
    """Wait for the child's verdict, or for it to die trying.

    Three outcomes, and the third is why this is a pipe rather than a file.
    ``ready`` means the endpoint answered a real request and the descriptor is
    on disk. ``failed`` means the child said so. End of file with neither means
    the child is gone, which covers a crash during imports and a kill from
    outside, and it arrives at once instead of after the timeout.

    Lines are scanned rather than only the first one read. The child redirects
    its standard output away as soon as it can, but that is not the very first
    thing it does, and a library that greets the terminal on import would
    otherwise turn a healthy owner into a failed election.

    Read on a thread rather than with a readiness check on the descriptor.
    ``select`` accepts only sockets on Windows, so the obvious portable-looking
    loop would block inside ``readline`` well past the deadline on exactly the
    platform where this handshake is the *only* thing the frontend has to go on.
    A thread bounds the wait everywhere with one mechanism.
    """
    stream = child.stdout
    assert stream is not None

    verdicts: queue.Queue[str | None] = queue.Queue()

    def collect() -> None:
        try:
            seen = 0
            for line in stream:
                verdict = line.decode("utf-8", "replace").strip()
                if verdict in (daemon_owner.READY, daemon_owner.FAILED):
                    verdicts.put(verdict)
                    return
                # Bounded, so a child stuck printing cannot keep this thread
                # alive for the owner's whole lifetime.
                seen += len(line)
                if seen > 4096:
                    break
        except OSError:  # pragma: no cover - the stream closed under us
            pass
        finally:
            # End of file, or a child that talked without ever answering. Either
            # way the caller must stop waiting, so absence is a message too.
            verdicts.put(None)

    # A daemon thread: if the child neither answers nor exits, the frontend must
    # still be able to shut down. It holds only this pipe, which the caller
    # closes.
    reader = threading.Thread(target=collect, name="daemon-handshake", daemon=True)
    reader.start()

    try:
        verdict = verdicts.get(timeout=max(timeout, 0.0))
    except queue.Empty:
        # Not a failure, and the difference matters. The child said nothing and
        # did not exit, so it is still starting and still holds the lock it
        # adopted. Reporting this as "the child could not serve" would send the
        # caller off to drive its own browser against the profile the child is
        # about to open — two browsers, from a slow machine rather than a bug.
        logger.warning(
            "The daemon has not finished starting; leaving it to come up on its own"
        )
        return _Started.STILL_TRYING

    if verdict is None:
        logger.info("The daemon exited before it was ready")
        return _Started.NO
    if verdict == daemon_owner.FAILED:
        logger.info("The daemon reported that it could not start")
        return _Started.NO
    return _Started.YES
