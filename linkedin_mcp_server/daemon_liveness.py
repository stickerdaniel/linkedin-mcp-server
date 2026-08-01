"""Whether anybody is still waiting for a call the owner is running.

A frontend that gives up does not take its call with it. Cancellation is not
forwarded across the loopback hop: measured in this repository, the client was
answered at 0.66s with an error and the effect landed 0.7s later, against a
LinkedIn account nobody was watching any more. The owner has no way to notice,
because a call it is already running looks exactly like one somebody wants.

So the frontend says so, repeatedly, for as long as it is still waiting, and the
owner cancels what nobody is waiting for. Kept in its own module rather than in
``daemon_owner`` or ``server`` because both ends need the same names and those
two must not import each other.

**No ``PROTOCOL_VERSION`` bump, which is a departure from the rule and is meant
to be read as one.** The descriptor says the field is bumped when the control
routes or the call metadata change, and this is both. A bump would also make
every currently installed owner unreadable to a new frontend, and an unreadable
owner cannot be asked to stand down: the upgrade path would break in exactly the
way the recovery work just repaired. The additions are optional in both
directions instead, which is what makes that safe. A new frontend against an old
owner gets 404 on the heartbeat route and proceeds without one. An old frontend
against a new owner sends no call marker, and a call with no marker counts as
active for as long as it runs and is never cancelled. Both directions are
tested, because compatibility that is only asserted is the thing the invariant
was written to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

logger = logging.getLogger(__name__)

#: The route a frontend beats on for as long as it is still waiting. Part of the
#: daemon's own protocol; see the module docstring for why the version is not
#: bumped for it.
HEARTBEAT_PATH = "/control/heartbeat"

#: The header naming which call a request belongs to. A header rather than MCP
#: call metadata, because the identifier has to reach the owner on both the tool
#: call *and* the heartbeat, and only one of those is an MCP message at all.
CALL_HEADER = "x-linkedin-mcp-call"

#: Version prefix on the header value. Versioned so an owner meeting a shape it
#: does not know can ignore it rather than half-read it, which is the same rule
#: `daemon_auth` applies to markers travelling the other way.
_MARKER_PREFIX = "v1."

#: A call id is this many hex characters after the prefix. Bounded and checked,
#: because this arrives in an HTTP header from anything that can reach the port,
#: and it is used as a dictionary key on a long-lived process.
_ID_HEX = 32

#: How often a waiting frontend says it is still there. Measured on this machine
#: against a server built the way the owner builds one: a heartbeat round trip
#: costs 0.9ms median while the owner is idle and 1.8ms while it is driving
#: Chromium, so this cadence occupies an owner about a thousandth of its time
#: per waiting call.
HEARTBEAT_SECONDS = 2.0

#: How long a call may go unheard before the owner stops it. Five cadences, and
#: the margin is the point rather than the number. Measured worst cases: 9.5ms
#: for a heartbeat served while a page is being driven, 3.6ms of timer lateness
#: in a detached process over 25 beats, and 130ms for the longest the owner's
#: synchronous text extraction holds its event loop, on a 2MiB page far larger
#: than a real one. A live call therefore needs a stall roughly seventy times
#: worse than anything measured, or three consecutive beats lost, before it is
#: wrongly cancelled. The relationship is what matters and what the tests pin;
#: the two numbers are only a comfortable point on it.
EXPIRY_SECONDS = 10.0

#: A gap between two of the owner's own expiry scans that means the owner was
#: not running rather than that a frontend went quiet. The serving loop polls
#: ten times a second, so anything approaching this is the process itself having
#: missed its turn: a laptop that slept, a machine under heavy load, or a long
#: synchronous stretch on the event loop. Expiring on the tick after one of
#: those would cancel calls whose frontends were beating the whole time and
#: never got heard, which is the opposite of what any of this is for.
_STALL_SECONDS = 1.0


def new_call_id() -> str:
    """A marker for one call, in the form the header carries."""
    return f"{_MARKER_PREFIX}{secrets.token_hex(_ID_HEX // 2)}"


def call_id_in(header_value: str | None) -> str | None:
    """The call id in *header_value*, or ``None`` if there is not one this build reads.

    Strict rather than forgiving, and every part of it is checked: an older
    frontend sends nothing, a newer one may send a shape that does not exist yet,
    and anything on the machine can reach the port and send whatever it likes.
    A value that is not understood makes the call unmarked, which is the safe
    answer, because an unmarked call is never cancelled.
    """
    if header_value is None:
        return None
    if not header_value.startswith(_MARKER_PREFIX):
        return None
    body = header_value[len(_MARKER_PREFIX) :]
    if len(body) != _ID_HEX:
        return None
    try:
        int(body, 16)
    except ValueError:
        return None
    return header_value


@dataclass
class _Waiting:
    """One call the owner is running, and when it was last asked for."""

    task: asyncio.Task[object]
    last_heard: float


@dataclass
class CallLiveness:
    """Which calls somebody is still waiting for.

    One instance per owner process. Times are monotonic, so a clock the user
    changes mid-call cannot expire one.
    """

    _waiting: dict[str, _Waiting] = field(default_factory=dict)

    #: How many calls are running, marked or not. Separate from ``_waiting`` on
    #: purpose: an unmarked call cannot be cancelled, but it is emphatically not
    #: idleness, and an owner that exited during one would cut off the very
    #: frontends this build promises to keep serving.
    _in_flight: int = 0

    #: When this owner last did anything for anybody, or ``None`` while it is
    #: still starting. Idleness is only meaningful once the endpoint is
    #: published: before that nobody could have called, and an owner that
    #: counted its own startup as idle time would exit during it.
    _quiet_since: float | None = None

    #: When the owner last got round to asking who was still waiting. Compared
    #: against the next scan so a stall in this process is not charged to the
    #: frontends.
    _last_scan: float | None = None

    def watch(self, call_id: str, task: asyncio.Task[object]) -> None:
        """Start counting for *call_id*, running as *task*.

        The first heartbeat arrives before the call is dispatched, so a call is
        registered already knowing it was heard, rather than starting from a
        deadline it has to catch up with.
        """
        self._waiting[call_id] = _Waiting(task=task, last_heard=time.monotonic())

    def call_started(self) -> None:
        """Note a call arriving, whether or not this build can identify it."""
        self._in_flight += 1
        self._quiet_since = None

    def call_finished(self) -> None:
        """Note a call ending, however it ended."""
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._quiet_since = time.monotonic()

    def the_endpoint_is_live(self) -> None:
        """Start the idle clock, once there is something to be idle *at*.

        Called where the descriptor is published rather than at startup. An
        owner spends its first seconds importing and launching Chromium, and
        counting that as quiet would let a short timeout expire before the
        frontend that asked for this owner had any way to reach it.
        """
        if self._quiet_since is None and self._in_flight == 0:
            self._quiet_since = time.monotonic()

    def quiet_for(self) -> float | None:
        """Seconds since this owner last had anything to do, if it is free now.

        ``None`` while a call is running, and while the endpoint has not been
        published, which are the two states where the question does not apply.

        The ``_in_flight`` half of that test is redundant today and is kept
        anyway, which is worth saying rather than leaving to be discovered: what
        actually protects a running call is :meth:`call_started` clearing the
        clock, and removing this check breaks no test. It is here so the answer
        stays right if the two ever drift, and it should not be counted as a
        rule anything verifies.
        """
        if self._in_flight > 0 or self._quiet_since is None:
            return None
        return time.monotonic() - self._quiet_since

    def heard(self, call_id: str) -> bool:
        """Note that somebody is still waiting. False if this call is unknown.

        Unknown covers both an id for a call that already finished and one that
        was never registered, and neither is worth distinguishing: the answer to
        the frontend is the same and it does not get to learn which.
        """
        entry = self._waiting.get(call_id)
        if entry is None:
            return False
        entry.last_heard = time.monotonic()
        return True

    def release(self, call_id: str) -> None:
        """Stop counting for *call_id*, however its call ended."""
        self._waiting.pop(call_id, None)

    def cancel_the_abandoned(self) -> list[str]:
        """Cancel every call nobody has asked for lately, and say which.

        Called from the owner's own polling loop rather than from a timer of its
        own, so this costs one dictionary scan per tick on a process that is
        already ticking.
        """
        now = time.monotonic()
        since_last = now - self._last_scan if self._last_scan is not None else 0.0
        self._last_scan = now
        if since_last > _STALL_SECONDS:
            # The owner itself was not running, so nobody had a chance to be
            # heard. Charging that silence to the frontends would cancel calls
            # that were being asked for the whole time. Everyone's clock starts
            # again instead, rather than moving by the length of the stall,
            # which for a stall longer than the expiry would push deadlines into
            # the future. A call that really was abandoned gets one more cycle,
            # which is the cheaper of the two mistakes.
            logger.info(
                "The owner was not scheduled for %.1fs; nobody is written off for that",
                since_last,
            )
            for entry in self._waiting.values():
                entry.last_heard = now
            return []

        deadline = now - EXPIRY_SECONDS
        abandoned = [
            call_id
            for call_id, entry in self._waiting.items()
            if entry.last_heard < deadline
        ]
        for call_id in abandoned:
            entry = self._waiting.pop(call_id)
            logger.info(
                "Nobody has waited for call %s in %.0fs; stopping it",
                call_id,
                EXPIRY_SECONDS,
            )
            entry.task.cancel()
        return abandoned


#: The tracker for this process. Module state rather than something threaded
#: through, for the same reason the browser is: the middleware that registers a
#: call and the serving loop that expires it have no object in common.
_liveness = CallLiveness()


def get_liveness() -> CallLiveness:
    """The tracker for this process."""
    return _liveness


def reset_liveness_for_testing() -> None:
    """Forget every watched call, so one test cannot leak into the next."""
    _liveness._waiting.clear()
    _liveness._in_flight = 0
    _liveness._quiet_since = None
    _liveness._last_scan = None


class OwnerCallLivenessMiddleware(Middleware):
    """Stop running a call once nobody is waiting for it.

    Registered outside the serializing middleware, which is deliberate: a call
    can spend most of its life queued behind another one, and a frontend that
    gives up while its call is still in the queue is the commonest way to end up
    with work nobody wants. Inside, the call would only become cancellable once
    it had already taken the browser.

    A call with no marker this build understands runs to completion. That covers
    an older frontend, which sends nothing, and a newer one whose marker this
    build cannot read, and in both cases refusing to cancel is the only safe
    answer: the alternative is stopping a call somebody is waiting for because
    its heartbeats were addressed to a shape we did not parse.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        from fastmcp.server.dependencies import get_http_headers

        call_id = call_id_in(get_http_headers().get(CALL_HEADER))

        # Counted before anything else, and counted even when the marker is
        # missing. Cancelling an unidentified call is not safe, but calling it
        # idleness is worse: the owner would exit in the middle of one, and the
        # frontends that send no marker are exactly the older ones this build
        # promises to keep serving.
        _liveness.call_started()
        try:
            if call_id is None:
                return await call_next(context)

            # A task rather than a plain await, because cancelling is the whole
            # point and there is nothing to cancel until the work has a handle.
            # `create_task` copies the current context, so everything the call
            # reads from it downstream is the same as it would have been.
            running: asyncio.Task[ToolResult] = asyncio.ensure_future(
                call_next(context)
            )
            _liveness.watch(call_id, running)
            try:
                return await running
            except asyncio.CancelledError:
                outer = asyncio.current_task()
                if outer is not None and outer.cancelling():
                    # Somebody cancelled *this* middleware, which is shutdown
                    # rather than an abandoned call. Reporting it as one would
                    # tell whoever asked us to stop that the call merely lost
                    # its client. The same counter tells the two apart in
                    # `browser_lifespan`, for the same reason.
                    raise
                if running.cancelled() and call_id not in _liveness._waiting:
                    # Ours: this call was expired above. Reported as an error
                    # rather than re-raised, so the cancellation stops here
                    # instead of travelling on into a server nobody asked to
                    # shut down.
                    raise ToolError(
                        "Stopped because the client that asked for it stopped waiting"
                    ) from None
                raise
            finally:
                _liveness.release(call_id)
        finally:
            _liveness.call_finished()
