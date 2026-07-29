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
#: and reporting what it last saw. Covers a cold start of the whole server graph
#: plus the owner's own startup probe, and is what bounds the delay a user sees
#: when something goes wrong rather than when it goes right.
DEFAULT_ELECTION_SECONDS = 45.0

#: How long a published owner has to answer before it is treated as leftovers.
#: This is a loopback request to a process that is either serving or gone, so
#: the only thing a longer budget buys is a longer stall on a dead descriptor.
_REACHABLE_SECONDS = 5.0

#: How long a superseded owner has to acknowledge a stand-down request. Short:
#: the reply says only that the request arrived, and whether the browser is
#: actually free is settled by the lock afterwards, not by this.
_STAND_DOWN_SECONDS = 5.0

#: How long to pause before attempting the lock again, when the descriptor on
#: disk is readable but known unusable. Short, because the thing being waited for
#: is an owner finishing its shutdown, and long enough not to spin.
_RETRY_SECONDS = 0.2

#: Proves a published endpoint is live. Injectable so the election can be tested
#: without a real server; production passes nothing and gets a real round trip.
Reachable = Callable[[Attachment], bool]


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
    # Instances proved dead, so a corpse is not handed back a second time. The
    # descriptor stays on disk until a live owner overwrites it, and without
    # this the loop would re-read it, probe it again, and spin.
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

    if reach(attachment):
        return lookup, False

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
    import httpx

    descriptor = attachment.descriptor
    url = f"http://{descriptor.host if ':' not in descriptor.host else f'[{descriptor.host}]'}:{descriptor.port}{daemon_owner.STAND_DOWN_PATH}"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {attachment.token}"},
            timeout=_STAND_DOWN_SECONDS,
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


def _reachable(attachment: Attachment) -> bool:
    """Whether the published endpoint answers this token.

    An authenticated round trip rather than a TCP connect. A connect succeeds
    against any listener that happens to hold the port, including one the
    operating system handed to something else after the owner died, and against
    an owner whose token no longer matches the file this client just read.
    """
    import asyncio

    async def ask() -> bool:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        try:
            async with Client(
                StreamableHttpTransport(
                    attachment.descriptor.url, auth=attachment.token
                )
            ) as client:
                await client.ping()
        except Exception:
            logger.debug("The published daemon did not answer", exc_info=True)
            return False
        return True

    try:
        return asyncio.run(asyncio.wait_for(ask(), _REACHABLE_SECONDS))
    except Exception:
        # Includes the timeout, and a caller that is already inside a running
        # loop. Both mean the same thing here: no proof the owner is alive.
        logger.debug("The daemon reachability check failed", exc_info=True)
        return False


def _log_hint(auth_root: Path) -> Path:
    """Where a user should look when an owner refused to start.

    The owner is detached, so its failure is not on anyone's terminal. Without
    this the whole diagnosis a user gets is that nothing happened.
    """
    return daemon_owner.daemon_log_path(auth_root)


class _Started(enum.Enum):
    """What the startup handshake said, in the three ways it can end.

    Silence is its own answer and must not be folded into failure. A child that
    has neither answered nor exited is still coming up and still holds the lock
    it adopted; calling that a failure sends the caller off to drive its own
    browser against the profile that child is about to open.
    """

    #: The endpoint answered and the descriptor is published.
    YES = "yes"

    #: The child said it could not serve, or died trying.
    NO = "no"

    #: Neither, within the budget. It may still succeed.
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
            started = _spawn(auth_root, config, lock_fd=duplicate, timeout=timeout)
            if started is _Started.YES:
                outcome = _Attempt.STARTED
            elif started is _Started.STILL_TRYING:
                # The child is alive and holds the lock this process is about to
                # let go of, so from here it is indistinguishable from any other
                # owner coming up: wait for it rather than conclude anything.
                outcome = _Attempt.CONTENDED
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

    command = [sys.executable, "-m", "linkedin_mcp_server.daemon_owner"]
    if lock_fd is not None:
        command += ["--lock-fd", str(lock_fd)]

    # Opened append, so a restart adds to the record rather than erasing the
    # reason the last owner died.
    with open(log_path, "a", encoding="utf-8") as log:
        child = subprocess.Popen(
            command,
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
            # does not take the owner with it.
            start_new_session=True,
            close_fds=True,
        )

    try:
        assert child.stdin is not None and child.stdout is not None
        with child.stdin as handed:
            handed.write(daemon_config.encode(config).encode())
        return _await_ready(child, timeout=timeout)
    finally:
        # Closed once the verdict is in. The child has redirected its own
        # standard output to the log by then and writes nothing more here, so
        # this cannot cost it a broken pipe.
        if child.stdout is not None:
            child.stdout.close()
        _reap(child)


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
