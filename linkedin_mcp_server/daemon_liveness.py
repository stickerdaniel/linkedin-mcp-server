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

**Every call is marked, or it does not run.** A call the owner cannot identify
is a call it cannot cancel, so the owner refuses one that arrives without a
marker, and the frontend never forwards a call whose heartbeat preflight failed.
That is tool protocol 2 (``daemon_descriptor.PROTOCOL_VERSION``). Protocol 1
made both halves optional so a mixed pair could share tools; the default-on
contract drops that pairing, and a mismatched owner is now only ever asked to
stand down.

**Admission and retirement are one decision.** Whether this owner takes another
call and whether it may go away are both answered here, on one tracker and with
no ``await`` between checking and setting. On one event loop that makes the two
orderings the only ones possible: the call wins and retirement sees it, or
retirement wins and the call is refused before anything below it runs.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.daemon_descriptor import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The route a frontend beats on for as long as it is still waiting, and the
#: preflight it beats on once before a call is dispatched at all.
HEARTBEAT_PATH = "/control/heartbeat"

#: Where an owner's refusal travels on an error result, beside the auth marker
#: ``daemon_auth`` uses. Namespaced for the same reason: a key a future fastmcp
#: might add cannot shadow it.
REFUSAL_KEY = "linkedin.dev/daemon"

#: The two refusals an owner gives before a call has run. Both prove the call
#: was never dispatched to a tool, which is what lets a frontend repeat it.
RETIRING = "retiring"
UNMARKED_CALL = "unmarked_call"

#: What an abandoned call is told, wherever that is noticed.
_ABANDONED = "Stopped because the client that asked for it stopped waiting"

#: What a client is told when a mutating call may or may not have acted. A value
#: of its own rather than messaging's `send_unconfirmed`, which already says a
#: submission was attempted: here even that is unknown, and the same answer has
#: to fit a connection request as well as a message.
UNKNOWN_OUTCOME_STATUS = "outcome_unknown"


def unknown_outcome(*, tool: str, reason: str) -> dict[str, Any]:
    """The structured half of a call whose effect nobody can vouch for.

    The field names are `scraping.contracts.message_action_result`'s, so a client
    that already reads `status` and `retry_safe` on a send needs nothing new for
    this one. Built here rather than imported from there because no daemon module
    reaches into `scraping/`: this is not a scraping outcome but the transport
    saying it knows nothing, and it answers for every mutating tool.

    `url`, `sent` and `recipient_selected` are left out rather than set to null.
    `sent` is the one thing nobody here knows, and a null reads as a "no" to any
    client that tests the value rather than the key's presence.

    Both ends use it: the frontend when an owner vanished with a call in flight,
    and the owner when it cut a call off to stand down.
    """
    return {
        "status": UNKNOWN_OUTCOME_STATUS,
        "message": (
            f"{tool} was in flight when the shared browser process went away "
            f"({reason}). Whether the action reached LinkedIn is unknown. Check "
            "LinkedIn before calling again, because a repeat may perform the "
            "action a second time."
        ),
        "retry_safe": False,
    }


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
    answer, because an unmarked call is refused before it runs.
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

    task: asyncio.Task[Any]
    last_heard: float


@dataclass
class CallLiveness:
    """Which calls somebody is still waiting for, and whether any more may come.

    One instance per owner process. Times are monotonic, so a clock the user
    changes mid-call cannot expire one.

    Admission and retirement are decided here and nowhere else. Every method
    that reads one of them and writes the other is plain rather than ``async``,
    which is the whole mechanism: on one event loop nothing can run between the
    check and the set, so a call and a retirement cannot both win.
    """

    _waiting: dict[str, _Waiting] = field(default_factory=dict)

    #: How many calls were admitted and have not finished, whether they are
    #: running or still queued behind another one. Counted from admission,
    #: which is outside the serializing middleware, so a queued call is as
    #: busy as a running one.
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

    #: Set once, and never cleared for the life of the process. A retiring owner
    #: admits nothing more, and the heartbeat route tells a frontend so before
    #: it dispatches anything.
    retiring: bool = False

    #: Why, for the log and for tests. Only the first reason is kept.
    retire_reason: str | None = None

    #: The owner's own instance, named in every refusal so a frontend can tell
    #: this owner's answer from arbitrary tool output.
    instance_id: str | None = None

    #: Calls this owner cut off itself while standing down, as opposed to calls
    #: whose client went away. The middleware reports these as an unknown
    #: outcome, because they may have acted and somebody is still waiting.
    _cut: set[str] = field(default_factory=set)

    def serving_as(self, instance_id: str) -> None:
        """Name the instance whose refusals this tracker signs."""
        self.instance_id = instance_id

    def admit(
        self, call_id: str, start: Callable[[], Coroutine[Any, Any, _T]]
    ) -> asyncio.Task[_T] | None:
        """Take one marked call, or refuse it because this owner is retiring.

        *start* builds the call's work, and it is called only once the call has
        been admitted. That order is the point: a refused call has no task, so
        nothing below this could have been scheduled, and none of the counters
        moved. An admitted call is counted and watched before its task can take
        a single step, because creating a task only schedules it.
        """
        if self.retiring:
            return None
        self.call_started()
        try:
            task = asyncio.ensure_future(start())
        except BaseException:
            self.call_finished()
            raise
        self.watch(call_id, task)
        return task

    def busy(self, *, background_work: bool = False) -> bool:
        """Whether retiring now would cut off somebody's work.

        *background_work* is detached work the caller knows about and this
        tracker does not, such as a browser setup still running.
        """
        return self._in_flight > 0 or bool(self._waiting) or background_work

    def try_retire(self, reason: str, *, background_work: bool = False) -> bool:
        """Close admission if nothing is in flight or queued. True if it did.

        The retirement an owner chooses for itself, such as the idle exit.
        """
        if self.retiring:
            return True
        if self.busy(background_work=background_work):
            return False
        self.retire(reason)
        return True

    def retire(self, reason: str) -> None:
        """Close admission unconditionally, whatever is in flight.

        The retirement something else decided: a newer build taking over, a
        browser that can no longer be driven, a publication that failed. What
        is already admitted keeps running for whatever drain the caller allows.

        No I/O, not even a log line: one caller is on its way to a hard exit
        that must not wait on a diagnostic. Callers say why themselves.
        """
        if not self.retiring:
            self.retiring = True
            self.retire_reason = reason

    def watch(self, call_id: str, task: asyncio.Task[Any]) -> None:
        """Start counting for *call_id*, running as *task*.

        The first heartbeat arrives before the call is dispatched, so a call is
        registered already knowing it was heard, rather than starting from a
        deadline it has to catch up with.
        """
        self._waiting[call_id] = _Waiting(task=task, last_heard=time.monotonic())

    def call_started(self) -> None:
        """Note a call being admitted."""
        self._in_flight += 1
        self._quiet_since = None

    def call_finished(self) -> None:
        """Note an admitted call ending, however it ended."""
        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._quiet_since = time.monotonic()

    def calls_in_flight(self) -> int:
        """How many admitted calls have not finished yet."""
        return self._in_flight

    def background_activity_finished(self) -> None:
        """Start a fresh quiet period after detached work kept the owner active."""
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

        ``None`` while a call is admitted, and while the endpoint has not been
        published, which are the two states where the question does not apply.
        A hint for the idle timer only: whether the owner may actually go is
        :meth:`try_retire`'s answer, taken at the moment it acts.
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
        self._cut.discard(call_id)

    def abandoned(self, call_id: str) -> bool:
        """Whether *call_id* may no longer start browser work.

        An absent entry counts as abandoned, and that is deliberate rather than
        cautious. A running call is registered from admission until it ends, so
        a missing entry is one the expiry already wrote off or one that never
        got through admission; neither was heard from, and treating either as
        freshly heard would hand authority to a call nobody vouched for.

        An entry older than the expiry is abandoned too, even before the next
        scan pops it, unless this owner itself has not been scanning: the same
        stall rule :meth:`cancel_the_abandoned` applies, for the same reason.
        """
        entry = self._waiting.get(call_id)
        if entry is None:
            return True
        now = time.monotonic()
        if self._last_scan is not None and now - self._last_scan > _STALL_SECONDS:
            return False
        return now - entry.last_heard > EXPIRY_SECONDS

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

    def cut_off_the_rest(self) -> list[str]:
        """Cancel every call still running because this owner is going away.

        Only after admission closed and the drain ran out. Each call cut here is
        reported to its client as an unknown outcome rather than as abandoned,
        because its client is still waiting and the call may already have acted.
        """
        cut = list(self._waiting)
        for call_id in cut:
            entry = self._waiting.pop(call_id)
            self._cut.add(call_id)
            logger.warning("Cutting off call %s to stand down", call_id)
            entry.task.cancel()
        return cut


#: The tracker for this process. Module state rather than something threaded
#: through, for the same reason the browser is: the middleware that registers a
#: call and the serving loop that expires it have no object in common.
_liveness = CallLiveness()

#: The call the current task is running for, on an owner. Set inside the call's
#: own task, so it is visible to everything that call awaits and to nothing
#: else. Never set on a Direct server.
_current_call: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "linkedin_mcp_owner_call", default=None
)


def get_liveness() -> CallLiveness:
    """The tracker for this process."""
    return _liveness


def reset_liveness_for_testing() -> None:
    """Forget every watched call, so one test cannot leak into the next."""
    _liveness._waiting.clear()
    _liveness._in_flight = 0
    _liveness._quiet_since = None
    _liveness._last_scan = None
    _liveness.retiring = False
    _liveness.retire_reason = None
    _liveness.instance_id = None
    _liveness._cut.clear()


def abandoned_before_browser_work() -> bool:
    """Whether the call this task runs for was given up before it began.

    Asked once, after every wait a call can spend queued (the serializing lock
    and the profile lease) and before it touches the browser. Cancellation
    already stops an abandoned call at its next suspension; this is the check
    that holds even for a call whose waits all completed in the tick the expiry
    fired.

    False on a Direct server, which marks no calls. On an owner a call id that
    the tracker does not know counts as abandoned (:meth:`CallLiveness.abandoned`).
    """
    call_id = _current_call.get()
    if call_id is None:
        return False
    return _liveness.abandoned(call_id)


def abandoned_call_error() -> ToolError:
    """What a call stopped for its absent client reports."""
    return ToolError(_ABANDONED)


def _refusal(kind: str, message: str) -> ToolResult:
    """An error result proving this call never reached a tool.

    Signed with the protocol and this owner's instance, so a frontend accepts
    it only from the owner it dispatched to. ``is_error`` stays true so a
    client that reads no metadata still sees a failure rather than a success
    carrying no data, which is how ``daemon_auth`` marks its own refusals.
    """
    return ToolResult(
        content=[mt.TextContent(type="text", text=message)],
        meta={
            REFUSAL_KEY: {
                "daemon": kind,
                "protocol": PROTOCOL_VERSION,
                "instance": _liveness.instance_id,
            }
        },
        is_error=True,
    )


class OwnerCallLivenessMiddleware(Middleware):
    """Admit a call only if it is marked and the owner is not retiring.

    Registered outside the serializing middleware, which is deliberate: a call
    can spend most of its life queued behind another one, and a frontend that
    gives up while its call is still in the queue is the commonest way to end up
    with work nobody wants. Inside, the call would only become cancellable once
    it had already taken the browser, and a queued call would not count as busy
    when the owner decides whether it may retire.

    A call with no marker this build understands is refused before anything
    runs. It could not be cancelled when its client went away, and after the
    protocol bump the only sender of one is a client this owner never promised
    to serve.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        from fastmcp.server.dependencies import get_http_headers

        call_id = call_id_in(get_http_headers().get(CALL_HEADER))
        if call_id is None:
            logger.warning("Refusing a call that carries no call marker")
            return _refusal(
                UNMARKED_CALL,
                "The shared browser refused a call it could not identify. "
                "Update the client and retry.",
            )

        async def run() -> ToolResult:
            # Inside the call's own task, so the id is visible to everything the
            # call awaits and cannot leak into anything else.
            _current_call.set(call_id)
            return await call_next(context)

        # The first thing that can change state, and nothing awaits before it
        # returns. `admit` decides and builds the task in one step.
        running = _liveness.admit(call_id, run)
        if running is None:
            return _refusal(
                RETIRING,
                "The shared browser is shutting down and did not start this call.",
            )

        try:
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
                if running.cancelled() and call_id in _liveness._cut:
                    # Ours, and not because the client left: this owner is
                    # standing down and the drain ran out. The client is still
                    # waiting, and the call may already have acted.
                    answer = unknown_outcome(
                        tool=context.message.name,
                        reason="it was replaced and stopped waiting for this call",
                    )
                    return ToolResult(
                        content=[mt.TextContent(type="text", text=answer["message"])],
                        structured_content=answer,
                        is_error=True,
                    )
                if running.cancelled() and call_id not in _liveness._waiting:
                    # Ours: this call was expired above. Reported as an error
                    # rather than re-raised, so the cancellation stops here
                    # instead of travelling on into a server nobody asked to
                    # shut down.
                    raise abandoned_call_error() from None
                raise
            finally:
                _liveness.release(call_id)
        finally:
            # Only an admitted call reaches this, so the count stays balanced.
            _liveness.call_finished()
