"""Whether anybody is still waiting for a call the owner is running.

Cancellation does not cross the loopback hop, so a frontend that gives up
leaves its call running. The frontend therefore beats for as long as it waits,
and the owner cancels what nobody waits for. Both ends share these names, and
``daemon.owner`` and ``server`` must not import each other.

- Every call is marked, or it does not run: the owner refuses an unmarked call,
  and the frontend never forwards one whose preflight failed. Both halves are
  tool protocol (``daemon.descriptor.PROTOCOL_VERSION``).
- Admission and retirement are one decision, ``daemon.core.owner``'s.
  :class:`CallLiveness` runs its effects, and every method that feeds an event
  is synchronous, so nothing runs between a decision and the state it leaves.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.daemon.core import owner as lifecycle
from linkedin_mcp_server.daemon.descriptor import PROTOCOL_VERSION

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The route a frontend beats on for as long as it is still waiting, and the
#: preflight it beats on once before a call is dispatched at all.
HEARTBEAT_PATH = "/control/heartbeat"

#: Where an owner's refusal travels on an error result, beside the auth marker
#: ``daemon.auth_repair`` uses. Namespaced for the same reason: a key a future fastmcp
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


def unknown_outcome(
    *, tool: str, reason: str, owner: str = "stopped answering it"
) -> dict[str, Any]:
    """The structured half of a call whose effect nobody can vouch for.

    Uses `linkedin.contracts.message_action_result`'s field names (`status`,
    `retry_safe`) without importing from `linkedin/`. `sent` and the other
    unknowns are omitted rather than null, since a null reads as "no". Used by
    the frontend when a call's answer never arrived and by the owner when it
    cut a call off to stand down. *owner* says what the shared browser process
    did. The default claims no more than the lost answer: the process may still
    be running, as when only the hop dropped its answer or its heartbeats kept
    coming. The frontend replaces it when the owner did not answer its
    heartbeats or a recovery probe either, which still says nothing about
    whether it is gone.
    """
    return {
        "status": UNKNOWN_OUTCOME_STATUS,
        "message": (
            f"{tool} was in flight when the shared browser process {owner} "
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
#: `daemon.auth_repair` applies to markers travelling the other way.
_MARKER_PREFIX = "v1."

#: A call id is this many hex characters after the prefix. Bounded and checked,
#: because this arrives in an HTTP header from anything that can reach the port,
#: and it is used as a dictionary key on a long-lived process.
_ID_HEX = 32

#: How often a waiting frontend says it is still there. A beat is a cheap
#: loopback round trip; the owner stops a call after several cadences without
#: one (``daemon.core.owner.EXPIRY_SECONDS``). Measured:
#: docs/daemon-measurements.md.
HEARTBEAT_SECONDS = 2.0


def new_call_id() -> str:
    """A marker for one call, in the form the header carries."""
    return f"{_MARKER_PREFIX}{secrets.token_hex(_ID_HEX // 2)}"


def call_id_in(header_value: str | None) -> str | None:
    """The call id in *header_value*, or ``None`` if there is not one this build reads.

    Strict, because anything on the machine can reach the port. A value not
    understood makes the call unmarked, and an unmarked call is refused before
    it runs.
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


class CallLiveness:
    """The owner's admission and lifecycle state, and the tasks it decides about.

    One per owner process, on monotonic time. Every decision is
    ``daemon.core.owner.on_event``'s: this class reads the clock, runs the
    effects that concern calls, and leaves lifecycle effects to the serving
    loop in ``daemon.owner``.
    """

    def __init__(self) -> None:
        self._state = lifecycle.Owner()
        self._tasks: dict[int, asyncio.Future[Any]] = {}
        self._tickets: dict[asyncio.Future[Any], int] = {}

    @property
    def state(self) -> lifecycle.Owner:
        """The state the core decides on."""
        return self._state

    @property
    def retiring(self) -> bool:
        """Whether the owner is retiring, which closes admission for good.

        Admission is also closed before publication without retiring; ask
        ``admitting`` for whether a call would be admitted.
        """
        return self._state.retirement is not None

    @property
    def admitting(self) -> bool:
        """Whether a new call would be admitted: published and not retiring."""
        return lifecycle.admitting(self._state)

    @property
    def instance_id(self) -> str | None:
        """The owner's own instance, named in every refusal."""
        return self._state.instance

    def _decide(self, event: lifecycle.Event) -> list[lifecycle.Effect]:
        self._state, effects = lifecycle.on_event(self._state, event)
        return effects

    def _apply(self, event: lifecycle.Event) -> list[lifecycle.Effect]:
        """Decide *event* and carry out every effect that concerns a call."""
        effects = self._decide(event)
        for effect in effects:
            self.carry_out(effect)
        return effects

    def carry_out(self, effect: lifecycle.Effect) -> None:
        """Run *effect* if it concerns a call. Lifecycle effects are the loop's."""
        match effect:
            case lifecycle.Cancel(because=lifecycle.Fate.CUT):
                logger.warning("Cutting off call %s to stand down", effect.call_id)
                self._cancel(effect.ticket)
            case lifecycle.Cancel():
                logger.info(
                    "Nobody has waited for call %s in %.0fs; stopping it",
                    effect.call_id,
                    lifecycle.EXPIRY_SECONDS,
                )
                self._cancel(effect.ticket)
            case lifecycle.Forgave():
                logger.info(
                    "The owner was not scheduled for %.1fs; nobody is written off "
                    "for that",
                    effect.seconds,
                )

    def _cancel(self, ticket: int) -> None:
        task = self._tasks.get(ticket)
        if task is not None:
            task.cancel()

    def serving_as(self, instance_id: str) -> None:
        """Name the instance whose refusals this tracker signs."""
        self._apply(lifecycle.Named(instance_id))

    def admit(
        self, call_id: str, start: Callable[[], Coroutine[Any, Any, _T]]
    ) -> asyncio.Task[_T] | None:
        """Take one marked call, or refuse it: unpublished, or retiring.

        *start* is called only after admission, so a refused call has no task,
        and an admitted call is counted and watched before its task can take a
        step.
        """
        effects = self._apply(lifecycle.Admit(call_id, time.monotonic()))
        admitted = [e for e in effects if isinstance(e, lifecycle.StartTask)]
        if not admitted:
            return None
        ticket = admitted[0].ticket
        try:
            task = asyncio.ensure_future(start())
        except BaseException:
            self._apply(lifecycle.StartFailed(ticket, time.monotonic()))
            raise
        self._tasks[ticket] = task
        self._tickets[task] = ticket
        return task

    def finished(self, task: asyncio.Future[Any]) -> None:
        """Note that the admitted call running as *task* ended, however it ended."""
        ticket = self._tickets.pop(task, None)
        if ticket is None:
            return
        del self._tasks[ticket]
        self._apply(lifecycle.Finished(ticket, time.monotonic()))

    def stopped(self, task: asyncio.Future[Any]) -> lifecycle.Stopped | None:
        """How a call this owner cancelled is answered, or ``None`` if it did not."""
        return lifecycle.stopped(self._state, self._tickets.get(task))

    def body_began(self, call_id: str) -> None:
        """Note that *call_id* is past the point where it could still be refused."""
        self._apply(lifecycle.BodyBegan(call_id))

    def busy(self, *, background_work: bool = False) -> bool:
        """Whether retiring now would cut off somebody's work.

        *background_work* is detached work the caller knows about and this
        tracker does not, such as a browser setup still running.
        """
        return lifecycle.busy(self._state, background=background_work)

    def try_retire(
        self, reason: lifecycle.Retirement, *, background_work: bool = False
    ) -> bool:
        """Close admission if nothing is in flight or queued. True if it is closed."""
        self._apply(lifecycle.RetireIfIdle(reason, background=background_work))
        return self.retiring

    def retire(self, reason: lifecycle.Retirement) -> None:
        """Close admission unconditionally, whatever is in flight.

        No I/O, not even a log line: an owner whose publication is ambiguous
        closes admission before anything that could block on storage. Callers
        say why themselves.
        """
        self._apply(lifecycle.Retire(reason))

    def turn_over(self) -> None:
        """Close admission now, and owe the admitted calls the turnover drain.

        In the request itself, not on the loop's next tick: a call arriving
        between the reply and the tick would otherwise be admitted by an owner
        that has already said it is leaving.
        """
        self._apply(lifecycle.TurnOver())

    def background_activity_finished(self) -> None:
        """Start a fresh quiet period after detached work kept the owner active."""
        self._apply(lifecycle.BackgroundFinished(time.monotonic()))

    def the_endpoint_is_live(self) -> None:
        """Open admission and start the idle clock once the descriptor is published.

        Not at startup: counting import and Chromium launch as quiet would let
        a short timeout expire before the frontend could reach this owner.
        """
        self._apply(lifecycle.Published(time.monotonic()))

    def heard(self, call_id: str) -> bool:
        """Note that somebody is still waiting. False if this call is not watched.

        A finished, written-off, cut or never registered call all answer the
        same, and the frontend does not learn which.
        """
        self._apply(lifecycle.Heard(call_id, time.monotonic()))
        return lifecycle.watching(self._state, call_id)

    def abandoned(self, call_id: str) -> bool:
        """Whether *call_id* may no longer start browser work."""
        return lifecycle.abandoned(self._state, call_id, time.monotonic())

    def tick(
        self,
        *,
        idle_timeout: float = 0.0,
        wedged: str | None = None,
        setup_running: bool = False,
        setup_failed: bool = False,
        endpoint_stopped: bool = False,
    ) -> list[lifecycle.Effect]:
        """One turn of the owner's lifecycle loop, decided and not yet carried out.

        The loop runs the effects in order and hands each that concerns a call
        to :meth:`carry_out`, so its log reads in the order it was decided.
        """
        return self._decide(
            lifecycle.Tick(
                now=time.monotonic(),
                idle_timeout=idle_timeout,
                wedged=wedged,
                setup_running=setup_running,
                setup_failed=setup_failed,
                endpoint_stopped=endpoint_stopped,
            )
        )


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
    _liveness._state = lifecycle.Owner()
    _liveness._tasks.clear()
    _liveness._tickets.clear()


def abandoned_before_browser_work() -> bool:
    """Whether the call this task runs for was given up before it began.

    Asked once, after every queued wait (the serializing lock, the profile
    lease) and before the browser is touched. It catches a call whose waits
    all completed in the tick its expiry fired, which cancellation alone
    misses. False on a Direct server; on an owner an unknown call id counts as
    abandoned.
    """
    call_id = _current_call.get()
    if call_id is None:
        return False
    return _liveness.abandoned(call_id)


def browser_work_begins() -> None:
    """Record that the call this task runs for is starting its tool body.

    Called by the serializing middleware after its last abandonment check,
    with nothing awaited in between, so the line it draws is exactly the one
    between a call that was only ever queued and a call that may have acted.
    Does nothing on a Direct server, which marks no calls.
    """
    call_id = _current_call.get()
    if call_id is not None:
        _liveness.body_began(call_id)


def abandoned_call_error() -> ToolError:
    """What a call stopped for its absent client reports."""
    return ToolError(_ABANDONED)


def _refusal(kind: str, message: str) -> ToolResult:
    """An error result proving this call never reached a tool.

    Signed with the protocol and this owner's instance, so a frontend accepts
    it only from the owner it dispatched to. ``is_error`` stays true for
    clients that read no metadata.
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

    Registered outside the serializing middleware on purpose: a call abandoned
    while queued must be cancellable, and a queued call must count as busy when
    the owner decides whether it may retire. An unmarked call is refused before
    anything runs, because it could not be cancelled.
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
            if not running.cancelled():
                raise
            match _liveness.stopped(running):
                case lifecycle.Stopped.NOT_RUN:
                    # Cut by this owner standing down while only ever queued,
                    # so nothing ran. The same signed refusal a retiring owner
                    # gives a new call, which a frontend already reads as not
                    # sent and may repeat.
                    return _refusal(
                        RETIRING,
                        "The shared browser shut down before this call "
                        "started. Nothing was run; retry it.",
                    )
                case lifecycle.Stopped.UNKNOWN:
                    # Cut after its tool body began, so it may have acted. Its
                    # client is still waiting and is told exactly that.
                    answer = unknown_outcome(
                        tool=context.message.name,
                        reason="it was replaced and stopped waiting for this call",
                    )
                    return ToolResult(
                        content=[mt.TextContent(type="text", text=answer["message"])],
                        structured_content=answer,
                        is_error=True,
                    )
                case lifecycle.Stopped.ABANDONED:
                    # Expired because nobody was waiting. Reported as an error
                    # rather than re-raised, so the cancellation stops here
                    # instead of travelling on into a server nobody asked to
                    # shut down.
                    raise abandoned_call_error() from None
                case None:
                    raise
        finally:
            # Only an admitted call reaches this, so the count stays balanced.
            _liveness.finished(running)
