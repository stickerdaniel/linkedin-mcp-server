"""Talking to the owner, from the frontend that forwards to it.

The other half of :mod:`linkedin_mcp_server.daemon.election`: that module gets an
owner running and hands back an attachment, and this one turns the attachment into
something a FastMCP server can serve tools from.

Kept out of ``server.py`` deliberately. That module answers which parts of a
server a role gets; the loopback address, the bearer token, the proxy-environment
refusal and the forwarding deadline are all daemon knowledge, and every one of
them is a way to leak a credential or to hang a call.

How a request crosses to the owner is :mod:`linkedin_mcp_server.daemon.forwarder`.
It reads the owner's address and token per operation from
:class:`DaemonProxyBackend`, so a proxy follows a replacement owner instead of
keeping an address it captured at startup. This module is the call shell around
it: the heartbeat preflight, recovery, auth repair and the call's deadline.

**Which owner that is changes while this process runs**, and the rest of this
module is about surviving it. ``@latest`` is the documented install, so the first
client to launch after an upgrade stands the old owner down, and every proxy
already attached to it would otherwise be dead until its own process restarts.
Reproduced end to end before any of this existed: a proxy serving 19 tools was
asked nothing, its owner was told to stand down the way a newer build does
(``daemon.election._ask_to_stand_down``), and the next listing failed with
``McpError: Client failed to connect``. Idle exit and a crashed owner end the
same way.

Noticing the departure is one half; repeating the call is the other, and it is
the half that can do damage. There is no dispatch acknowledgement anywhere in the
protocol, so a failure is classified where the boundary is known
(:class:`OwnerUnreachableError`) rather than diagnosed from a message afterwards.
Only a request that provably never left this process may be repeated when running
it twice would send a connection request twice.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import httpx2
import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp import MCPError

from linkedin_mcp_server.config.schema import AUTH_REPAIR_LOGIN_WAIT_FRACTION
from linkedin_mcp_server.daemon import auth_repair
from linkedin_mcp_server.daemon import owner as daemon_owner
from linkedin_mcp_server.daemon.auth_repair import a_repeat_could_change_something
from linkedin_mcp_server.daemon.core import call as call_core
from linkedin_mcp_server.daemon.descriptor import PROTOCOL_VERSION
from linkedin_mcp_server.daemon.forwarder import (
    CallBinding,
    DispatchRecord,
    OwnerFailure,
    OwnerUnreachableError,
    bound_to,
    unreachable_owner_in,
)
from linkedin_mcp_server.daemon.liveness import (
    CALL_HEADER,
    HEARTBEAT_PATH,
    HEARTBEAT_SECONDS,
    REFUSAL_KEY,
    RETIRING,
    UNMARKED_CALL,
    new_call_id,
    unknown_outcome,
)

if TYPE_CHECKING:
    from pathlib import Path

    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon.discovery import Attachment

logger = logging.getLogger(__name__)


#: The dispatch record of the attempt :class:`FrontendCallMiddleware` is making,
#: handed in to the binding the heartbeat middleware makes for it. Read back when
#: the call's deadline cuts the attempt off, because only the forwarder's record
#: says whether the tool request had left by then.
_attempt_dispatch: contextvars.ContextVar[DispatchRecord | None] = (
    contextvars.ContextVar("linkedin_mcp_attempt", default=None)
)

#: Added to the owner's tool timeout to get the frontend's deadlines: each hop's
#: request (:class:`~linkedin_mcp_server.daemon.forwarder.OwnerForwarder`), and
#: the whole call's (:func:`call_budget`). The owner is the one that should
#: report a timed-out tool, so the frontend has to outlast it; an equal value
#: races the owner's own error response and turns a diagnosable "tool timed out"
#: into a transport failure.
#:
#: It does not make room for a queued call, and no value derived from the tool
#: timeout could. The owner serializes in middleware, and a tool's timeout only
#: starts once the middleware lets it through — measured: a tool declared with a
#: one second timeout, queued two seconds behind another call, succeeded after
#: 2.02s. So under real concurrency the call's deadline can pass while it is
#: still waiting its turn. The frontend then stops waiting and stops its
#: heartbeats, and the owner cancels the call once they have expired.
_TIMEOUT_MARGIN_SECONDS = 30.0


def call_budget(tool_timeout: float) -> float:
    """The seconds one forwarded call may take, from its arrival at the frontend.

    The tool timeout a client configured, plus :data:`_TIMEOUT_MARGIN_SECONDS`:
    the bound a first attempt's hop always had, now covering everything else
    the call waits on too (``daemon.core.call``). No setting of its own, because
    ``TOOL_TIMEOUT`` already says how long a call may take.
    """
    return tool_timeout + _TIMEOUT_MARGIN_SECONDS


#: How many elections one recovery may join. The second exists only for a
#: caller whose burial the first election did not know about.
_ELECTIONS_PER_RECOVERY = 2


@dataclass(frozen=True)
class _Flight:
    """One election in progress, and which owners it was told to pass over."""

    task: asyncio.Task[Attachment | None]
    buried: frozenset[str]


class _WrittenOff(Exception):
    """The cause on a failure for an owner this process already wrote off."""

    def __init__(self, classification: OwnerFailure) -> None:
        super().__init__(f"the owner was already written off ({classification.value})")


def _refuse_control_only(attachment: Attachment) -> None:
    """Refuse to dispatch anything through a pair proved for control only.

    Checked wherever an attachment becomes somewhere to send a call, not just
    where it is produced. The lookup that makes one never marks it attachable,
    and that is one rule in one place; this makes the prohibition hold even if
    that place changes.
    """
    if attachment.control_only:
        raise ValueError(
            "A shared browser owner proved for control only cannot run tools"
        )


class DaemonProxyBackend:
    """The owner this proxy forwards to, and what it would take to find another.

    One object rather than two loose values, and the reason is the second half:
    an owner is replaced by every upgrade, so the address a proxy uses has to be
    a thing that can change rather than a value captured at startup. Nothing
    changes it yet — that is the next change — but the shape has to exist before
    anything can.

    It carries the election's inputs alongside the answer because nothing
    downstream has them otherwise. The forwarder is built from this object, and
    ``create_mcp_server`` has no configuration
    parameter at all, so the proxy layer could not have elected a replacement
    even if it had wanted to. Reaching for ``get_config()`` there instead would
    supply defaults for a directly constructed server, not the exact inputs that
    elected the owner being replaced.

    Not frozen, unlike the ``Attachment`` it holds. The attachment is a proved
    fact about one owner and must not be edited; which attachment is current is
    exactly the thing that moves.

    **Some owners are written off for good.** An owner that refused a call as
    retiring, answered the heartbeat route with 404 or anything unexpected, or
    refused an unmarked call is recorded in ``_unusable`` and never dispatched
    to again by this process. Every consumer asks: a new call, a listing, an
    election's result before it is adopted, and a caller that joined an election
    already in progress. A call already running keeps the owner it was bound to,
    because moving its heartbeats would get it cancelled by the owner running it.
    """

    def __init__(
        self,
        *,
        attachment: Attachment,
        auth_root: Path,
        profile: Path,
        config: AppConfig,
    ) -> None:
        _refuse_control_only(attachment)
        self._attachment = attachment
        #: What an election needs, kept rather than looked up again.
        self.auth_root = auth_root
        self.profile = profile
        self.config = config
        #: The one election in progress, or nothing. Only :meth:`recover` writes
        #: it, and only :meth:`_elect` writes ``_attachment``, which is what makes
        #: comparing instance ids a sound test of whether a failure is current.
        self._electing: _Flight | None = None
        #: Owners this process will not dispatch to again, and why. Only grows.
        self._unusable: dict[str, OwnerFailure] = {}

    @property
    def attachment(self) -> Attachment:
        """The owner to talk to right now."""
        return self._attachment

    def note_failure(self, instance_id: str, classification: OwnerFailure) -> None:
        """Write *instance_id* off if *classification* says it is unusable.

        Synchronous and first, before any election is joined or started, so a
        burial this caller learned is part of every decision made after it.
        """
        if classification.buries and instance_id not in self._unusable:
            logger.info(
                "Not using this shared browser owner again (%s)", classification.value
            )
            self._unusable[instance_id] = classification

    def attachment_for_a_call(self) -> Attachment:
        """The owner a new call may be dispatched to, or a failure saying why not."""
        attachment = self._attachment
        self.refuse_if_written_off(attachment)
        return attachment

    def refuse_if_written_off(
        self, attachment: Attachment, *, sent: bool = False
    ) -> None:
        """Raise if a new request may not go to *attachment*, as of right now.

        Asked again at every point a new request could still be stopped: when a
        call picks its owner, after its preflight returns, when its hop is
        opened, and before each request on it. Each of those follows an await during
        which another call can write the owner off, and a check made before the
        await says nothing about after it. Whether it refuses is
        ``daemon.core.call.binding_refusal``'s rule; *sent* is whether a request
        of this call may already have left.

        The failure says nothing was sent, which is true: nothing was. It names
        the owner that was written off, not whichever replacement the backend
        holds now, because that one has passed no preflight for this call.
        """
        _refuse_control_only(attachment)
        instance = attachment.descriptor.instance_id
        written_off = call_core.binding_refusal(
            instance, written_off=self._unusable, sent=sent
        )
        if written_off is not None:
            raise OwnerUnreachableError(
                instance_id=instance,
                nothing_was_sent=True,
                cause=_WrittenOff(written_off),
                classification=written_off,
            )

    async def recover(
        self,
        failed_instance: str,
        *,
        classification: OwnerFailure = OwnerFailure.UNREACHABLE,
    ) -> Attachment | None:
        """Find an owner to replace *failed_instance*, at most once at a time.

        Returns the attachment to use now, or ``None`` when none could be
        established.

        *classification* is why the failed owner failed. One that buries it is
        recorded before anything else happens (:meth:`note_failure`).

        **The identity check comes first, and it is what keeps a late failure
        cheap.** Several calls can be in flight against one owner. The first to
        fail elects a replacement; the others fail afterwards still carrying the
        old identity, and without this they would each elect again, against an
        owner that is already answering.

        **The latest burial wins over an election already running.** A caller
        that joins a flight started before it buried an owner cannot trust that
        flight's answer, because the election ran without knowing: a retiring
        owner still answers its probe. An answer naming a written-off owner is
        refused, and if the flight did not know everything this caller knows,
        one more flight is run with it. At most two per call here, so a
        recovery cannot spin.

        **The election runs in a thread, and that is correctness rather than
        latency.** ``obtain_owner`` probes liveness through ``asyncio.run``, which
        raises inside a running loop, and its own ``except`` turns that into
        ``Reach.REFUSED``. Called in-loop it would therefore bury a *healthy*
        owner for the whole election rather than merely blocking.

        **Shielded, and this one is measured.** Cancelling the task that awaits
        ``asyncio.to_thread`` returns to the awaiter at once while the worker runs
        to completion regardless: driven directly, an awaiter cancelled at 0.1s
        and a worker that still finished its 1.5s of work. So a caller that gives
        up must not clear this guard, or the next failure starts a second election
        while the first is still running.
        """
        self.note_failure(failed_instance, classification)
        current = self._attachment.descriptor.instance_id
        if current != failed_instance and current not in self._unusable:
            # Somebody already replaced it. Nothing to do but use the answer.
            return self._attachment

        for _ in range(_ELECTIONS_PER_RECOVERY):
            flight = self._electing
            if flight is None:
                # Created and stored with no await in between, so two callers
                # cannot both get past this and start one each. The burial set
                # is a snapshot, handed to a thread.
                buried = frozenset(self._unusable)
                flight = _Flight(asyncio.create_task(self._elect(buried)), buried)
                self._electing = flight
            # Shielded so this caller's deadline cannot cancel the shared election.
            found = await asyncio.shield(flight.task)
            if found is not None and found.descriptor.instance_id in self._unusable:
                found = None
            if found is not None:
                if (
                    classification is OwnerFailure.TOKEN_REJECTED
                    and found.descriptor.instance_id == failed_instance
                ):
                    # A token is minted per instance and never changes, so the
                    # same instance comes back with the same token that was just
                    # refused. Another attempt would only be refused again.
                    return None
                return found
            if self._unusable.keys() <= flight.buried:
                # The election knew everything this caller knows. Its answer
                # stands, and asking again would get the same one.
                return None
        return None

    async def _elect(self, buried: frozenset[str]) -> Attachment | None:
        """Run one election and adopt what it finds, unless it is written off."""
        from linkedin_mcp_server.daemon.election import obtain_owner

        try:
            outcome = await asyncio.to_thread(
                partial(
                    obtain_owner,
                    self.auth_root,
                    self.profile,
                    self.config,
                    buried=buried,
                )
            )
        except Exception:
            logger.warning(
                "Could not elect a replacement for the shared browser owner",
                exc_info=True,
            )
            return None
        finally:
            # Only after the worker has ended, and only if this is still the
            # registered flight: a caller that timed out may already have left,
            # and a later election must not be cleared by an earlier one.
            flight = self._electing
            if flight is not None and flight.task is asyncio.current_task():
                self._electing = None

        found = outcome.attachment_lookup.attachment
        if not outcome.worth_connecting or found is None or found.control_only:
            logger.warning(
                "No shared browser owner could be established (%s)",
                outcome.attachment_lookup.state.value,
            )
            return None

        # Against the latest set, not the snapshot the election ran with: an
        # owner written off while it ran must not become the one every new call
        # is sent to.
        if found.descriptor.instance_id in self._unusable:
            logger.info("The election found an owner already written off")
            return None

        if found.descriptor.instance_id != self._attachment.descriptor.instance_id:
            logger.info("Attached to a replacement shared browser owner")
            self._attachment = found
        return found


#: What a client is told once recovery has ended without an owner to forward to.
#: Fixed text and never the failure's own: that names the loopback address, and
#: through the transport's message it can carry whatever the owner's port said.
_OWNER_LOST = (
    "This server lost the shared browser process and could not reach a new one. "
    "Reconnect or restart your MCP client to start a new one."
)


def _owner_lost_from_a_listing() -> MCPError:
    """The listing failure a client of either protocol era can read.

    An ``MCPError`` because that is the one exception the SDK carries to the
    wire as it is. On a 2026-07-28 connection anything else becomes ``Internal
    server error`` (``mcp/server/runner.py``, ``modern_error_data``, SDK 2.2.0),
    and on a handshake-era one it arrives as the failure's own text, which says
    "connect" and not what to do about it. A ``ToolError`` would not help here:
    no listing handler converts one, so it is just another exception to the SDK.
    """
    return MCPError(code=mt.INTERNAL_ERROR, message=_OWNER_LOST)


def _owner_lost_from_a_call() -> ToolError:
    """The tool call failure a client of either protocol era can read.

    A ``ToolError`` rather than the ``MCPError`` a listing gets, because a call
    has an error result and a listing does not. FastMCP's ``tools/call`` handler
    turns a ``FastMCPError`` into a result carrying its text, in both eras,
    which is the shape a model reads; an ``MCPError`` would leave the handler
    as a protocol error instead. What it replaces is either the masked ``Error
    calling tool '<name>'`` or, when the heartbeat preflight found nobody, a
    plain exception the SDK masks in its turn.
    """
    return ToolError(_OWNER_LOST)


@dataclass(frozen=True)
class _Lost:
    """An owner failure as the call core carries it, opaquely.

    *caught* is what the attempt raised, kept so a report is chained from the
    whole failure; *failure* is the tag found inside it.
    """

    caught: Exception
    failure: OwnerUnreachableError


class _OutOfTime(Exception):
    """The call's deadline ended a wait, as opposed to the wait failing."""


async def _by[T](deadline: float, awaitable: Awaitable[T]) -> T:
    """Await *awaitable* until *deadline* on the loop's clock, at the latest.

    Raises :class:`_OutOfTime` only for this deadline. A ``TimeoutError`` of
    the awaitable's own is its failure and leaves as it is.
    """
    try:
        async with asyncio.timeout_at(deadline) as limit:
            return await awaitable
    except TimeoutError:
        if limit.expired():
            raise _OutOfTime from None
        raise


#: Sign-ins started for a call that may stop waiting for them. A login is the
#: profile's, not the call's, so a call that runs out of time leaves it running;
#: held here so a task nobody awaits any more is not collected while it runs.
_SIGN_INS: set[asyncio.Task[Any]] = set()


class FrontendCallMiddleware(Middleware):
    """Carry one tool call through to an answer, as ``daemon.core.call`` decides.

    The shell around the call core, and the outer of the two middlewares a
    proxy installs. The core decides; this runs each effect and feeds the
    completion back: an attempt is ``call_next``, which enters
    :class:`FrontendCallHeartbeatMiddleware` and the provider below it; a
    recovery is the backend's one election; a classification is the tool's
    annotations; a repair and a sign-in wait are ``daemon.auth_repair``'s.

    **One deadline per call** (:func:`call_budget`, from arrival here). Every
    wait above is cut there at the latest. Two of them are shared, and the
    deadline ends only this call's wait for them, never the work: the
    election is shielded inside :meth:`DaemonProxyBackend.recover`, and a
    sign-in runs as a task of its own (:data:`_SIGN_INS`). A forwarded request
    that is cut is this call's own and is cancelled, which ends its heartbeats
    so the owner cancels it in turn.

    **What one tool call can cost, counted.** An invocation is at most two
    attempts, each one heartbeat preflight and at most one dispatch, and one
    recovery joining at most :data:`_ELECTIONS_PER_RECOVERY` elections; the
    second attempt's failure is recorded without electing. A sign-in adds at
    most one read-only replay, a second invocation. So one client call is at
    most four preflights, four dispatches and four election joins, all inside
    one deadline.

    Discovery is covered here as well, and it is the half a client hits first:
    many list before they call. A recovery that only covered ``tools/call``
    would leave a client whose owner died unable to list, and therefore unable
    to make the call that would have triggered the recovery. All four listings,
    not only tools, although this role serves nothing but tools: a client's
    opening exchange asks for resources and prompts too, and with
    ``provider_error_strategy = "raise"`` a departed owner turns that answer
    into an error the user sees. Reads and renders are left out because no
    owner here registers a resource or a prompt; the forwarder asks the owner's
    listing for those lookups too, and the day one is registered they need the
    same treatment as the listings, along with ``on_read_resource`` and
    ``on_get_prompt`` here.
    """

    def __init__(self, backend: DaemonProxyBackend, *, budget: float) -> None:
        """*budget* is the seconds a call may take from its arrival here.

        Taken as an argument rather than read from the configuration singleton,
        because the two can disagree: ``create_mcp_server`` is handed the tool
        timeout every tool it registers gets, and a server built directly may
        never have installed a configuration at all. Measured that way once, a
        1.2-second call budgeted itself 149.8 seconds from the singleton's default.
        """
        self._proxy_backend = backend
        self._call_budget = budget

    async def _repeat_the_listing(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        """List once, and once more against a replacement if the owner was lost.

        When no owner is left, either because recovery found none or because
        the replacement failed the repeat as well, the client is told so in
        words it can act on (:func:`_owner_lost_from_a_listing`). Only a
        failure recognised as the owner's is put that way; anything else leaves
        as it arrived, and the SDK masks it as it would on any server.
        """
        try:
            return await call_next(context)
        except Exception as exc:
            failure = unreachable_owner_in(exc)
            if failure is None:
                raise
            logger.info("Listing failed against a departed owner; looking for another")
            replacement = await self._proxy_backend.recover(
                failure.instance_id, classification=failure.classification
            )
            if replacement is None:
                raise _owner_lost_from_a_listing() from exc
        # Unconditional, because listing changes nothing on LinkedIn. There is
        # no effect a second one could repeat.
        try:
            return await call_next(context)
        except Exception as again:
            if unreachable_owner_in(again) is None:
                raise
            raise _owner_lost_from_a_listing() from again

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_list_resources(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_list_resource_templates(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_list_prompts(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        return await self._repeat_the_listing(context, call_next)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        loop = asyncio.get_running_loop()
        call, effects = call_core.arrive(
            now=loop.time(),
            budget=self._call_budget,
            sign_in_share=AUTH_REPAIR_LOGIN_WAIT_FRACTION,
        )
        while True:
            waiting: call_core.Effect | None = None
            for effect in effects:
                if isinstance(effect, call_core.Bury):
                    # Synchronously, before anything awaits: a burial must be
                    # part of every decision made after it, this call's retry
                    # and every other call's included.
                    lost = cast(_Lost, effect.failure).failure
                    self._proxy_backend.note_failure(
                        lost.instance_id, lost.classification
                    )
                elif isinstance(effect, call_core.TERMINAL):
                    return self._finish(context, effect)
                else:
                    waiting = effect
            if waiting is None:
                raise AssertionError(
                    f"the call core left the call waiting on {effects}"
                )
            event = await self._carry_out(waiting, call.deadline, context, call_next)
            call, effects = call_core.on_event(call, event)

    async def _carry_out(
        self,
        effect: call_core.Effect,
        deadline: float,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> call_core.Event:
        """Run one effect until it completes or *deadline*, and say which."""
        loop = asyncio.get_running_loop()
        try:
            match effect:
                case call_core.Attempt(op=op, kind=kind):
                    if kind is call_core.AttemptKind.RETRY:
                        logger.info(
                            "Attached to a replacement owner; running the call again"
                        )
                    elif kind is call_core.AttemptKind.REPLAY:
                        logger.info("Signed in; running the call again")
                    return await self._attempt(op, deadline, context, call_next)
                case call_core.Recover(op=op, owner=owner, failure=failure):
                    found = await _by(
                        deadline,
                        self._proxy_backend.recover(
                            owner,
                            classification=cast(_Lost, failure).failure.classification,
                        ),
                    )
                    return call_core.Recovered(op, loop.time(), found is not None)
                case call_core.Classify(op=op):
                    # A fresh forwarded listing, the forwarder keeps no
                    # component cache, and the call's own, so it is cut
                    # at the deadline like any other wait of this call's. It
                    # catches its own failures and answers `True`, the cautious
                    # side.
                    could_change = await _by(
                        deadline, a_repeat_could_change_something(context)
                    )
                    return call_core.Classified(op, loop.time(), could_change)
                case call_core.RepairAuth(op=op, reason=reason, generation=generation):
                    # A task of its own, shielded: the login belongs to the
                    # profile, and a call that stops waiting must not cancel
                    # one somebody may be typing into.
                    task = asyncio.ensure_future(
                        auth_repair.start_sign_in(reason, generation)
                    )
                    _SIGN_INS.add(task)
                    task.add_done_callback(_SIGN_INS.discard)
                    repaired, error = await _by(deadline, asyncio.shield(task))
                    return call_core.Repaired(op, loop.time(), repaired, error)
                case call_core.WaitForSignIn(op=op, until=until):
                    signed_in, error = await _by(
                        deadline,
                        auth_repair.wait_for_sign_in(max(0.0, until - loop.time())),
                    )
                    return call_core.SignedIn(op, loop.time(), signed_in, error)
        except _OutOfTime:
            return call_core.Expired(_op_of(effect), loop.time())
        raise AssertionError(f"not an effect that waits: {effect!r}")

    async def _attempt(
        self,
        op: int,
        deadline: float,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> call_core.Event:
        """One attempt, through the heartbeat middleware and the provider.

        The attempt's dispatch record is handed in so the binding made below
        writes to it. When the deadline cuts the attempt off, that record is
        the only proof of whether its tool request had left.
        """
        loop = asyncio.get_running_loop()
        record = DispatchRecord()
        handed_in = _attempt_dispatch.set(record)
        try:
            result = await _by(deadline, call_next(context))
        except _OutOfTime:
            return call_core.Expired(
                op, loop.time(), request_left=not record.nothing_was_sent
            )
        except Exception as exc:
            failure = unreachable_owner_in(exc)
            if failure is None:
                return call_core.Raised(op, loop.time(), exc)
            # With its cause: a loss starts a recovery, and the cause is the
            # only record of why this owner was given up on.
            logger.info(
                "Lost the shared browser owner (%s; %s)",
                failure.classification.value,
                "nothing was sent"
                if failure.nothing_was_sent
                else "the request may have left",
                exc_info=exc,
            )
            return call_core.OwnerLost(
                op,
                loop.time(),
                owner=failure.instance_id,
                nothing_was_sent=failure.nothing_was_sent,
                failure=_Lost(exc, failure),
            )
        finally:
            _attempt_dispatch.reset(handed_in)
        return call_core.Answered(
            op, loop.time(), result, auth=auth_repair.auth_request(result)
        )

    def _finish(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        effect: call_core.Effect,
    ) -> ToolResult:
        """Give the client the answer the call core ended the call with."""
        match effect:
            case call_core.Return(result=result, because=because):
                auth_repair.say_why(because)
                return cast(ToolResult, result)
            case call_core.ReportUnknown(failure=failure, expired=expired):
                if expired:
                    logger.info(
                        "The call ran out of time after its request may have "
                        "reached the owner; reporting an unknown outcome"
                    )
                    reason = (
                        f"it did not answer within this call's "
                        f"{self._call_budget:g}-second limit"
                    )
                else:
                    logger.info(
                        "Owner lost mid-call; reporting an unknown outcome rather "
                        "than repeating a call that could change something"
                    )
                    reason = str(cast(_Lost, failure).failure)
                return self._report_an_unknown_outcome(context, reason)
            case call_core.ReportLost(failure=failure, expired=expired):
                if expired:
                    logger.info("The call ran out of time before any owner took it")
                if failure is None:
                    raise _owner_lost_from_a_call()
                raise _owner_lost_from_a_call() from cast(_Lost, failure).caught
            case call_core.Reraise(error=error):
                raise error
            case call_core.ReportRestricted(error=error):
                return auth_repair.restricted_answer(error)
        raise AssertionError(f"not an effect that ends a call: {effect!r}")

    def _report_an_unknown_outcome(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        reason: str,
    ) -> ToolResult:
        """Say that nobody here knows whether the call acted.

        The one case where the honest answer is to report the failure. Nothing in
        the protocol says whether the departed owner had already sent the
        connection request or the message, and asking costs one retry while
        guessing wrong sends it twice. The user knows which happened; this
        process does not.

        A result rather than a raise, and what decides that is the payload rather
        than the masking. Two raises have to be told apart, both measured against
        fastmcp 3.4.7 with the masking this server switches on (``server.py``,
        ``mask_error_details=True``). Re-raising the failure that arrived says
        nothing: ``OwnerUnreachableError`` is a plain ``Exception``, so the call
        below has already replaced it with ``ToolError("Error calling tool
        'send_message'")`` (``fastmcp/server/server.py:1342-1358``), and that is
        the whole of what the client gets on the one call that most needs to
        hear a retry can deliver the message twice. A ``ToolError`` raised
        *here* keeps its own text: the masking sits below this middleware, and
        even there a ``FastMCPError`` — which ``ToolError`` is — is re-raised
        untouched (``:1327-1331``). What neither raise carries is ``status`` and
        ``retry_safe``, the two fields a client can act on without reading
        prose, and they are why this answer is a result.

        ``is_error`` stays true so a client that reads no structured content
        still sees a failure rather than a success carrying no data, the way
        ``daemon.auth_repair`` answers a sign-in.

        Keeping it true is also what lets this payload ignore the tool's declared
        output schema: an error result travels as a whole ``CallToolResult``,
        which ``mcp.server.lowlevel.server`` returns unchanged instead of
        validating it. A success-shaped dict would be checked against the schema
        of whichever tool was called, and this one is a stand-in for all of them.
        """
        answer = unknown_outcome(tool=context.message.name, reason=reason)
        return ToolResult(
            content=[mt.TextContent(type="text", text=answer["message"])],
            structured_content=answer,
            is_error=True,
        )


def _op_of(effect: call_core.Effect) -> int:
    """The operation a waiting effect is, for the completion that answers it."""
    op = getattr(effect, "op", None)
    if not isinstance(op, int):
        raise AssertionError(f"not an effect that waits: {effect!r}")
    return op


class _NotAnOwnerAnswer(Exception):
    """The cause on a preflight that was answered, but not with a go-ahead."""

    def __init__(self, status: int, classification: OwnerFailure) -> None:
        super().__init__(
            f"the heartbeat preflight was answered with HTTP {status} "
            f"({classification.value})"
        )


class _OwnerRefusedTheCall(Exception):
    """The cause on a call the owner signed as never having reached a tool."""

    def __init__(self, classification: OwnerFailure) -> None:
        super().__init__(f"the owner refused the call ({classification.value})")


def _json_object(response: httpx2.Response) -> dict[str, Any] | None:
    """The response body as a JSON object, or ``None`` for anything else."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _signed_by(marker: object, attachment: Attachment, kind: str) -> bool:
    """Whether *marker* is this owner's own refusal of the given *kind*.

    Every field is checked against the owner this call was bound to. The
    protocol is compared as an exact ``int`` because ``True == 1`` in Python,
    and a marker from another owner, another protocol or of another shape is
    not proof that this call never ran.
    """
    if not isinstance(marker, dict):
        return False
    protocol = marker.get("protocol")
    return (
        marker.get("daemon") == kind
        and type(protocol) is int
        and protocol == PROTOCOL_VERSION
        and marker.get("instance") == attachment.descriptor.instance_id
    )


def _classify_preflight(
    response: httpx2.Response, attachment: Attachment
) -> OwnerFailure | None:
    """What a heartbeat preflight's answer means, or ``None`` to go ahead.

    Total: every status has an outcome, and only one of them dispatches. A 200
    must carry the body this owner's route sends, so a stranger on the port that
    answers everything with 200 is not taken for the owner.
    """
    status = response.status_code
    if status == 200:
        body = _json_object(response)
        if body is not None and isinstance(body.get("watched"), bool):
            return None
        return OwnerFailure.UNEXPECTED_STATUS
    if status == 401:
        return OwnerFailure.TOKEN_REJECTED
    if status == 404:
        return OwnerFailure.ROUTE_MISSING
    if status == 409:
        if _signed_by(_json_object(response), attachment, RETIRING):
            return OwnerFailure.RETIRING
        return OwnerFailure.UNEXPECTED_STATUS
    if 500 <= status <= 599:
        return OwnerFailure.OWNER_ERROR
    return OwnerFailure.UNEXPECTED_STATUS


_REFUSALS = {
    RETIRING: OwnerFailure.RETIRING,
    UNMARKED_CALL: OwnerFailure.UNMARKED_REFUSED,
}


def _owner_refused(result: object, attachment: Attachment) -> OwnerFailure | None:
    """The owner's own refusal on *result*, if it is one it signed for this call.

    Anything else is tool data, whatever it looks like, and proves nothing
    about whether the tool ran.
    """
    if not isinstance(result, ToolResult) or not result.is_error:
        return None
    marker = (result.meta or {}).get(REFUSAL_KEY)
    for kind, classification in _REFUSALS.items():
        if _signed_by(marker, attachment, kind):
            return classification
    return None


class FrontendCallHeartbeatMiddleware(Middleware):
    """Mark every call, and say for as long as this frontend waits that it still is.

    The other half of `daemon.liveness`. Cancellation does not cross the hop, so
    an owner cannot tell a call somebody wants from one whose client has gone;
    this is the frontend answering that question over and over until it stops
    caring, at which point the answer stops arriving and the owner draws the
    obvious conclusion.

    **No call leaves unmarked.** The first beat is a preflight, and only a
    validated 200 dispatches the call. Every other answer, and every failure to
    get one, raises :class:`OwnerUnreachableError` with ``nothing_was_sent``
    true and a classification, before the call is handed on. The same holds for
    an owner that answered the call itself with its signed refusal: the tool
    never ran, and recovery may repeat it.

    Inner of the two a proxy installs, and that is what makes a repeat behave:
    :class:`FrontendCallMiddleware` sits outside, so every attempt it makes
    enters here afresh and gets its own identifier and its own heartbeats
    against whichever owner it is now talking to.
    """

    def __init__(self, backend: DaemonProxyBackend) -> None:
        self._proxy_backend = backend

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        call_id = new_call_id()
        # Captured once, and every beat for this call goes here even if another
        # call adopts a replacement owner meanwhile. Beating at the new owner
        # would name a call it has never heard of, while the owner actually
        # running this one stopped being told about it. Refused here if the
        # current owner was written off or proved for control only.
        attachment = self._proxy_backend.attachment_for_a_call()

        refused = await self._preflight(attachment, call_id)
        if refused is not None:
            raise refused
        # Asked again, because the preflight was an await: another call may have
        # found this owner retiring and written it off meanwhile. The go-ahead
        # this preflight got is older than that news.
        self._proxy_backend.refuse_if_written_off(attachment)

        beating = asyncio.create_task(self._keep_saying(attachment, call_id))
        dispatch = _attempt_dispatch.get()
        binding = CallBinding(
            call_id, attachment, dispatch if dispatch is not None else DispatchRecord()
        )
        try:
            with bound_to(binding):
                result = await call_next(context)
        finally:
            # In a finally, and unconditionally: a task left running would go on
            # keeping a finished call alive in the owner's tracker, which is the
            # exact state this exists to prevent.
            beating.cancel()

        classification = _owner_refused(result, attachment)
        if classification is not None:
            logger.info(
                "The shared browser owner refused the call before running it (%s)",
                classification.value,
            )
            raise OwnerUnreachableError(
                instance_id=attachment.descriptor.instance_id,
                nothing_was_sent=True,
                cause=_OwnerRefusedTheCall(classification),
                classification=classification,
            )
        return result

    async def _preflight(
        self, attachment: Attachment, call_id: str
    ) -> OwnerUnreachableError | None:
        """Beat once before dispatching, and say why not if the call must not go.

        Its not-sent answer is about the tool request, which this makes certain
        has not left, and not about the preflight, which may well have arrived.
        A connection that was never opened is ``UNREACHABLE``; one that opened
        and then failed is ``OWNER_ERROR``. Neither buries the owner: a restart
        or a stall looks like either, and the election's own probe decides.
        """
        try:
            response = await self._beat(attachment, call_id)
        except Exception as exc:
            logger.info(
                "The shared browser owner did not answer the call preflight",
                exc_info=True,
            )
            return OwnerUnreachableError(
                instance_id=attachment.descriptor.instance_id,
                nothing_was_sent=True,
                cause=exc,
            )
        classification = _classify_preflight(response, attachment)
        if classification is None:
            return None
        logger.info(
            "The shared browser owner refused the call preflight (HTTP %s, %s)",
            response.status_code,
            classification.value,
        )
        return OwnerUnreachableError(
            instance_id=attachment.descriptor.instance_id,
            nothing_was_sent=True,
            cause=_NotAnOwnerAnswer(response.status_code, classification),
            classification=classification,
        )

    async def _keep_saying(self, attachment: Attachment, call_id: str) -> None:
        """Beat until cancelled, and never let a failed beat end the run.

        A beat that fails is not evidence the call is over: the owner may be
        momentarily busy, and giving up here would expire a call that is running
        perfectly well. The frontend stops saying it is waiting only when it
        stops waiting, which is what the cancellation in the caller's `finally`
        expresses.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await self._beat(attachment, call_id)
            except Exception:
                logger.debug("A heartbeat did not arrive", exc_info=True)

    @staticmethod
    async def _beat(attachment: Attachment, call_id: str) -> httpx2.Response:
        """One heartbeat, returning the owner's answer with its body read.

        The address is the published one with its path replaced, rather than
        rebuilt from host and port: the descriptor's URL already brackets an
        IPv6 literal, and rebuilding is how that bracket gets lost.

        The owner's own client factory, for the reason it exists: httpx2 honours
        ``HTTP_PROXY`` even for loopback unless ``NO_PROXY`` happens to say
        otherwise, and this request carries the bearer token.
        """
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(attachment.descriptor.url)
        url = urlunsplit((parts.scheme, parts.netloc, HEARTBEAT_PATH, "", ""))
        async with daemon_owner.direct_async_http_client(
            headers={
                "Authorization": f"Bearer {attachment.token}",
                CALL_HEADER: call_id,
            },
            timeout=httpx2.Timeout(HEARTBEAT_SECONDS),
        ) as client:
            # Not streamed, so the body is read before the client closes and the
            # response stays readable after it.
            return await client.post(url)
