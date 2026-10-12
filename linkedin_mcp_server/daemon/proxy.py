"""Talking to the owner, from the frontend that forwards to it.

:mod:`linkedin_mcp_server.daemon.election` gets an owner running; this module
serves tools from it. The forwarder (:mod:`linkedin_mcp_server.daemon.forwarder`)
reads the owner's address and token per operation from
:class:`DaemonProxyBackend`, so a proxy follows a replacement owner. This module
is the call shell around it: heartbeat preflight, recovery, auth repair and the
call's deadline.

The owner changes while this process runs: an upgrade, an idle exit or a crash
replaces it. The protocol has no dispatch acknowledgement, so a failure is
classified where the boundary is known (:class:`OwnerUnreachableError`), and only
a request that provably never left this process may be repeated when running it
twice could repeat a LinkedIn write.
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
#: the whole call's (:func:`call_budget`). The frontend has to outlast the owner
#: so the owner reports a timed-out tool; an equal value races that report.
#:
#: It does not cover a queued call: the owner's tool timeout starts only once
#: its serializing middleware admits the call. Under concurrency the frontend
#: can stop waiting first; its heartbeats then stop and the owner cancels the
#: call once they expire.
_TIMEOUT_MARGIN_SECONDS = 30.0


def call_budget(tool_timeout: float) -> float:
    """The seconds one forwarded call may take, from its arrival at the frontend.

    The configured tool timeout plus :data:`_TIMEOUT_MARGIN_SECONDS`, covering
    every wait of the call (``daemon.core.call``).
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

    Checked wherever an attachment becomes a dispatch target, not only where
    the lookup produces it, so the prohibition survives a change there.
    """
    if attachment.control_only:
        raise ValueError(
            "A shared browser owner proved for control only cannot run tools"
        )


class DaemonProxyBackend:
    """The owner this proxy forwards to, and what it would take to find another.

    Mutable because every upgrade replaces the owner; the frozen ``Attachment``
    it holds is a proof about one owner. Carries the election's inputs because
    ``create_mcp_server`` has no configuration, and ``get_config()`` would give
    defaults rather than the inputs that elected the current owner.

    Some owners are written off for good: one that refused a call as retiring,
    answered the heartbeat route unexpectedly or refused an unmarked call is
    recorded in ``_unusable`` and never dispatched to again. Every consumer
    checks: a new call, a listing, an election's result before adoption, and a
    caller joining an election in progress. A running call keeps its bound
    owner, because moving its heartbeats would get it cancelled.
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

        Asked again after every await where another call could write the owner
        off: when a call picks its owner, after its preflight, when its hop
        opens, and before each request. The rule is
        ``daemon.core.call.binding_refusal``; *sent* is whether a request of
        this call may already have left. The failure names the written-off
        owner, not the backend's current one, which passed no preflight for this
        call.
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
        established. A *classification* that buries the failed owner is
        recorded first (:meth:`note_failure`).

        - The identity check comes first, so the other calls failing against an
          owner already replaced do not each elect again.
        - The latest burial wins: a flight started before it may return a
          written-off owner (a retiring owner still answers its probe). That
          answer is refused, and one more flight runs with everything this
          caller knows, at most :data:`_ELECTIONS_PER_RECOVERY` in all.
        - The election runs in a thread for correctness: ``obtain_owner`` uses
          ``asyncio.run``, which fails inside a running loop and would bury a
          healthy owner.
        - Shielded, because cancelling the awaiter of ``asyncio.to_thread``
          leaves the worker running; a caller that gives up must not clear the
          guard while that election still runs.
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

    An ``MCPError``, the one exception the SDK carries to the wire as it is; on
    a 2026-07-28 connection anything else becomes ``Internal server error``.
    No listing handler converts a ``ToolError``.
    """
    return MCPError(code=mt.INTERNAL_ERROR, message=_OWNER_LOST)


def _owner_lost_from_a_call() -> ToolError:
    """The tool call failure a client of either protocol era can read.

    A ``ToolError``, which FastMCP's ``tools/call`` handler turns into an error
    result a model reads in both eras; an ``MCPError`` would leave the handler
    as a protocol error.
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

    The outer of the two middlewares a proxy installs. The core decides; this
    runs each effect and feeds the completion back: an attempt is
    ``call_next`` (through :class:`FrontendCallHeartbeatMiddleware`), a
    recovery is the backend's election, a repair and a sign-in wait are
    ``daemon.auth_repair``'s.

    One deadline per call (:func:`call_budget`). It ends only this call's wait
    for shared work: the election is shielded in
    :meth:`DaemonProxyBackend.recover`, and a sign-in runs as its own task
    (:data:`_SIGN_INS`). A cut forwarded request is cancelled, which ends its
    heartbeats so the owner cancels it too.

    An invocation is at most two attempts (one preflight and at most one
    dispatch each) and one recovery of at most :data:`_ELECTIONS_PER_RECOVERY`
    elections; a sign-in adds at most one read-only replay. One client call is
    therefore at most four preflights, four dispatches and four election joins.

    All four listings recover too, because clients list before they call and
    ``provider_error_strategy = "raise"`` surfaces a departed owner. Resource
    reads and prompt renders are not covered because no owner registers either;
    registering one needs ``on_read_resource`` and ``on_get_prompt`` here.
    """

    def __init__(self, backend: DaemonProxyBackend, *, budget: float) -> None:
        """*budget* is the seconds a call may take from its arrival here.

        An argument rather than the configuration singleton, which a directly
        built server may never have installed, so its default would disagree
        with the tool timeout ``create_mcp_server`` was given.
        """
        self._proxy_backend = backend
        self._call_budget = budget

    async def _repeat_the_listing(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        """List once, and once more against a replacement if the owner was lost.

        Only a failure recognised as the owner's becomes
        :func:`_owner_lost_from_a_listing`; anything else leaves as it arrived.
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

        The protocol cannot say whether the departed owner already sent the
        request, and guessing wrong sends it twice, so the user decides.

        A result rather than a raise, because only a result carries ``status``
        and ``retry_safe``, the fields a client acts on; a re-raised
        ``OwnerUnreachableError`` is masked to ``Error calling tool``.
        ``is_error`` stays true so a client reading no structured content still
        sees a failure, and so the SDK skips validating this stand-in against
        the called tool's output schema.
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

    The other half of `daemon.liveness`: cancellation does not cross the hop,
    so the owner learns a client has gone only when the beats stop.

    No call leaves unmarked. The first beat is a preflight, and only a
    validated 200 dispatches; every other answer or failure raises
    :class:`OwnerUnreachableError` with ``nothing_was_sent`` before the call is
    handed on, as does the owner's own signed refusal of the call.

    The inner of the two middlewares a proxy installs, so every attempt
    :class:`FrontendCallMiddleware` makes gets its own call id and heartbeats
    against its current owner.
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

        The not-sent answer is about the tool request, not the preflight, which
        may have arrived. Neither ``UNREACHABLE`` nor ``OWNER_ERROR`` buries the
        owner: a restart or a stall looks like either, and the election's probe
        decides.
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
        """Beat until cancelled; a failed beat does not end the run.

        A failed beat is no evidence the call is over, and giving up would
        expire a running call. Only the caller's cancellation ends this.
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

        The descriptor's URL with its path replaced, not rebuilt from host and
        port, which would lose an IPv6 literal's brackets. The proxy-free client
        keeps the bearer token away from ``HTTP_PROXY``.
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
