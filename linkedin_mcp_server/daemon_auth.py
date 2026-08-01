"""Getting a login done in the process that can actually show one.

The owner drives the browser, so it is the process that *notices* bad LinkedIn
auth, and it has nobody in front of it, so it is the one that cannot fix it. Not
because it cannot open a browser: it can, and does when configured to run headed.
An interactive login is different in kind, because it waits for someone to type
credentials. The frontend is the opposite: it was spawned by an MCP client, a user
is there, and it can ask. This module carries the fact across the loopback hop between
them, and acts on it at the other end.

**Why a result and not an exception.** The owner's refusal has to survive
``ProxyProvider`` forwarding with its meaning intact, and an exception does not.
Measured against fastmcp 3.4.4: a middleware that re-raises delivered
``meta=None, isError=True, content=["Error calling tool 'x'"]`` to the outer
client, because ``mask_error_details`` is on and the type is gone by then. The
same middleware returning ``ToolResult(..., meta=..., is_error=True)`` delivered
the marker intact, and a client that raises on error still got its ``ToolError``
with the original message. The proxy copies ``meta`` and ``isError`` explicitly
(``fastmcp/server/providers/proxy.py:212-225``).

**Why the exception chain and not a ContextVar.** The owner-side middleware reads
``__cause__`` to find out what happened. A ``ContextVar`` set by the tool body
would also work, but only by accident: fastmcp runs ``async def`` tools inline and
offloads plain ``def`` tools to a thread, where the value set inside is invisible
to the middleware. Every tool here is ``async def`` today, so both would pass, and
one of them would break silently the first time that changed.

That chain reaching this far depends on ``raise_tool_error`` passing an
already-shaped ``ToolError`` through rather than re-deriving it. Before that fix
the missing-session path arrived as ``['ToolError']`` with the cause severed, and
this module could not have worked at all.

**What the frontend does with it.** Two fields, and they answer different
questions. ``reason`` says what to repair: a missing session needs a login, a
stale one needs the old session retired first. ``replayable`` says whether the
call may be run again afterwards, which depends on whether any work had happened
when it failed. Both combinations occur, so neither field implies the other.

Only the owner can decide ``replayable``, because every origin looks the same by
the time a failure reaches a client: all 18 tool catch sites collapse them into
one ``except`` clause.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.config.schema import (
    AUTH_REPAIR_LOGIN_WAIT_FRACTION,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
)
from linkedin_mcp_server.session_state import PeerSessionInPlaceError
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    AuthenticationStartedError,
    AuthMissingOnOwnerError,
    OwnerCannotAuthenticateError,
)

logger = logging.getLogger(__name__)

#: The least a replay is given, so that a call which has spent nearly all of its
#: budget still makes a real attempt rather than failing on a zero deadline for a
#: session that now exists. Itself capped by the budget below: as a flat floor it
#: overran, giving a 1-second call a 5-second replay.
_MINIMUM_REPLAY_SECONDS = 5.0


def _how_long_to_wait_for_the_sign_in(budget: float, already_spent: float) -> float:
    """How much of this call is left to spend waiting for the login.

    A fraction of *budget* rather than a fixed number of seconds, because the
    bound that matters is the call this is spent inside: a fixed 150 seconds was
    measured to outlive its own call once ``TOOL_TIMEOUT`` was lowered, replacing
    a readable answer with a bare timeout.

    *already_spent* is subtracted rather than ignored, and measuring caught that
    too: the fraction alone still overran, because the owner call and the inline
    login wait had gone before this is reached, and both come out of the same
    budget. Never negative, so an already-overrun call skips the wait and answers
    rather than waiting on a deadline that has passed. Zero still reaches the
    readiness check below it, which is what notices a login that finished in
    exactly this race.
    """
    return max(0.0, budget * AUTH_REPAIR_LOGIN_WAIT_FRACTION - already_spent)


def _what_is_left_of_this_call(budget: float, already_spent: float) -> float:
    """How much of the client's deadline a read-only replay may still use.

    The whole budget rather than the fraction above: waiting deliberately leaves
    a share for this, and by here that share is whatever has not been spent. A
    floor keeps a call that has already overrun from being handed a zero or
    negative deadline, which would fail the replay before it started for a
    session that now exists.

    The floor is itself capped by the budget, because a floor that outlives its
    own call is the very overrun this function exists to prevent: measured at
    ``tool_timeout_seconds=1.0`` with 0.9s spent, a flat floor allowed 5 seconds.
    Only reached for read-only tools, where an abandoned replay costs a wasted
    page load; anything that changes something is never timed out here.
    """
    return min(budget, max(_MINIMUM_REPLAY_SECONDS, budget - already_spent))


#: Namespaced so a future fastmcp key cannot shadow it. Measured: a custom key
#: coexists with fastmcp's own ``{'fastmcp': {...}}`` entry as a separate
#: top-level key, so there is no clash to work around, only a name to keep
#: unmistakable.
MARKER_KEY = "linkedin.dev/auth"

#: Bumped when the shape below changes in a way an older reader would misread.
#:
#: Only forward tolerance is needed, and that asymmetry is deliberate rather than
#: lucky. An *older* frontend attaches to a newer owner on purpose
#: (``daemon_version.Skew.SERVICEABLE``), so it will meet markers from builds it
#: does not know and has to ignore them. The reverse cannot arise: a newer
#: frontend asks an older owner to stand down and elects a replacement, so it
#: never talks to an owner that predates a version it understands.
#:
#: 2 adds ``generation``, and the bump is what makes it safe. A version-1 reader
#: accepts a version-1 marker, ignores fields it does not know, and repairs
#: without passing the generation on, which is the unguarded path that rotates a
#: peer's fresh session away. Refusing the marker outright leaves that frontend
#: reporting the owner's failure instead, which is the worse-informed but honest
#: outcome, and it is the one this version number buys.
MARKER_VERSION = 2


def _auth_failure_in(exc: BaseException) -> OwnerCannotAuthenticateError | None:
    """Find the owner's auth refusal in an exception chain, if it is there.

    Walked rather than type-checked directly, because nothing that leaves a tool
    is the original exception: ``raise_tool_error`` wraps it in a ``ToolError``,
    and the middleware only ever sees that wrapper.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OwnerCannotAuthenticateError):
            return current
        current = current.__cause__
    return None


class OwnerAuthSignalMiddleware(Middleware):
    """Turn the owner's auth refusal into something the hop preserves.

    Installed only on an owner. On any other role it would announce a repair
    nobody asked for, and on a frontend it would compete with the middleware that
    is supposed to *act* on the marker.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except Exception as exc:
            failure = _auth_failure_in(exc)
            if failure is None:
                raise

            marker: dict[str, Any] = {
                "v": MARKER_VERSION,
                "reason": "missing"
                if isinstance(failure, AuthMissingOnOwnerError)
                else "stale",
                "replayable": failure.nothing_ran_yet,
                "browser_open": _browser_still_holds_the_profile(),
                # Which session the owner found broken, so the frontend can tell
                # "this is still the dead one" from "somebody else already fixed
                # it". Two frontends meeting one dead session would otherwise both
                # rotate, and the second would destroy the first's fresh login.
                #
                # Taken from the failure rather than read again here. By this
                # point the browser has closed and the profile lease is free, so a
                # fresh read can return a generation another process has just
                # written, and the frontend would then rotate away exactly the
                # session it was told about. The owner's latch was armed from the
                # same observation, so the two cannot disagree.
                "generation": failure.generation,
            }
            logger.info(
                "Asking the client to sign in (%s, replayable=%s)",
                marker["reason"],
                marker["replayable"],
            )
            # An error result rather than a raise, and `is_error` kept true so a
            # client that never learns about markers still sees a failure with the
            # owner's own wording rather than a success carrying no data.
            return ToolResult(
                content=[mt.TextContent(type="text", text=str(exc))],
                meta={MARKER_KEY: marker},
                is_error=True,
            )


def _browser_still_holds_the_profile() -> bool:
    """Whether Chromium may still be on the profile in this process.

    Reported to the frontend because a login cannot start while it is true: the
    login takes the profile exclusively and would fail on a lease it cannot get.
    The lease only clears this once a close is *confirmed*, so an unconfirmed
    teardown keeps it set, which is the cautious direction.
    """
    from linkedin_mcp_server.profile_lease import get_profile_lease

    try:
        return get_profile_lease().browser_open
    except Exception:  # noqa: BLE001 - a marker must not fail on a probe
        logger.debug("Could not read the profile lease state", exc_info=True)
        return True


class FrontendAuthRepairMiddleware(Middleware):
    """Sign in on the owner's behalf, then run the call again when that is safe.

    Installed only on a proxy. It is the process an MCP client spawned, so it has
    somewhere to put a login window, and it is the only process in the picture
    that does.
    """

    def __init__(self, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS) -> None:
        """*tool_timeout* is the deadline a client's call is answered within.

        Taken as an argument rather than read from the configuration singleton,
        because the two can disagree. ``create_mcp_server`` is handed the budget
        every tool it registers gets, and a server built directly rather than by
        ``cli_main`` may never have loaded a configuration at all: measured at
        ``tool_timeout=1.2`` with none loaded, this budgeted itself 149.8 seconds
        of a 1.2-second call, because the unloaded singleton falls back to
        parsing ``sys.argv``.
        """
        self._tool_timeout = tool_timeout

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        began = time.monotonic()
        result = await call_next(context)
        marker = _readable_marker(result)
        if marker is None:
            return result

        if marker["browser_open"]:
            # The owner's Chromium may still hold the profile, so a login would
            # fail on a lease it cannot take. Passing the failure through leaves
            # the owner's own wording, which says to retry, and by the next call
            # the close will have been confirmed or the owner restarted.
            logger.warning(
                "The shared browser has not released the profile yet; not "
                "signing in on this call"
            )
            return result

        try:
            await _repair_auth_locally(marker["reason"], marker["generation"])
        except PeerSessionInPlaceError:
            # Not a failure: another client signed in while this one was asking
            # to, so the session the call needs already exists. Treating it as a
            # failed repair would refuse the replay and send the user back for a
            # retry that was not needed.
            logger.info("Another client already signed in; using its session")
        except (AuthenticationStartedError, AuthenticationInProgressError):
            # Also not a failure, and the one that mattered most. Both functions
            # behind `_repair_auth_locally` report a *started* login by raising,
            # while the window is still open and nobody has typed anything yet.
            # Read as a failure, every repair this middleware exists to perform
            # was abandoned one step before it worked: measured on the stale path
            # with a login that finished successfully, the owner was called once
            # and the client still got the error. The missing path did the same
            # as soon as signing in took longer than the inline budget, which is
            # every login a person actually performs.
            #
            # So wait the login out here. This is the process that has somewhere
            # to show it, and the client is waiting on this one call.
            left = _how_long_to_wait_for_the_sign_in(
                self._tool_timeout, time.monotonic() - began
            )
            if not await _wait_for_the_sign_in(left):
                logger.info("The sign-in did not finish in time; not replaying")
                return result
            logger.info("The sign-in finished")
        except Exception:
            # The login path reports itself through its own exceptions, which the
            # tool layer turns into readable messages. Whatever happened, the
            # owner's failure is still the honest answer to this call.
            logger.warning("Signing in for the shared browser failed", exc_info=True)
            return result

        if not marker["replayable"]:
            # Work had already happened on the owner when it failed, and some of
            # these tools send messages and connection requests. Running it again
            # could repeat that, so the client is told to decide.
            logger.info("Signed in; not repeating a call that had already started")
            return result

        if await a_repeat_could_change_something(context):
            # Repaired, but not run again. The auth is fixed and the next call
            # will work; what this one gets is the owner's own message, which
            # already says to retry.
            #
            # An auto-replay cannot be made safe here, and two rounds of trying
            # showed why. Bounding it locally left the owner finishing the
            # mutation after the frontend had reported failure: cancellation is
            # not forwarded across the hop, which
            # `daemon_proxy._TIMEOUT_MARGIN_SECONDS` already records. Measured
            # over a real loopback owner, the client was answered at 0.66s with
            # an error and the effect landed 0.7s later. Removing the bound only
            # moved the deadline: an MCP client that gives up on its own call
            # produces exactly the same orphan, measured as effects `['sent']`
            # after the cancel and `['sent', 'sent']` after the retry.
            #
            # Nothing this process can do closes that, because the deadline that
            # matters belongs to a client it does not control. So the decision
            # goes where the information is: the user knows whether the message
            # was sent, and asking costs one retry, while guessing wrong sends it
            # twice. Read-only tools keep the replay, which is where it pays.
            logger.info("Signed in; not repeating a call that could change something")
            return result

        logger.info("Signed in; running the call again")
        # Read-only, so bounded by what is left of this call rather than given a
        # fresh budget. Nothing below this middleware bounds the whole path: the
        # forwarded call has its own deadline per hop
        # (`daemon_proxy.create_proxy_provider`), so the first call, the wait and
        # the replay each stay inside one while their sum stays inside nothing.
        # Measured shape: a sign-in finishing near the end of the wait, followed
        # by a replay taking a full budget of its own, put the client past its
        # deadline with the answer already in hand. An abandoned scrape costs a
        # wasted page load and nothing else.
        remaining = _what_is_left_of_this_call(
            self._tool_timeout, time.monotonic() - began
        )
        try:
            return await asyncio.wait_for(call_next(context), timeout=remaining)
        except (TimeoutError, asyncio.TimeoutError):
            logger.info("The replayed call ran out of time; reporting the failure")
            return result


async def a_repeat_could_change_something(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> bool:
    """Whether running this tool again could repeat an effect on LinkedIn.

    Public because two middlewares need the same answer: this module replays a
    call after signing in, and ``daemon_proxy`` replays one after electing a
    replacement owner. One rule, in one place, so the two cannot drift apart in
    the unsafe direction.

    Read from the tool's own annotations, which survive the hop
    (``ProxyProvider`` copies them), rather than from a list of names kept here:
    a list would go stale the first time a tool is added, and it would go stale
    silently in the unsafe direction.

    Unknown counts as changing something. Only ``readOnlyHint`` says otherwise,
    and a tool that declares nothing has not promised anything.
    """
    name = getattr(context.message, "name", None)
    fastmcp_context = context.fastmcp_context
    if name is None or fastmcp_context is None:
        return True
    try:
        tool = await fastmcp_context.fastmcp.get_tool(name)
    except Exception:  # noqa: BLE001 - an unresolvable tool is not a safe one
        logger.debug("Could not read the annotations of %s", name, exc_info=True)
        return True
    annotations = getattr(tool, "annotations", None)
    return not bool(annotations and annotations.readOnlyHint)


def _readable_marker(result: ToolResult) -> dict[str, Any] | None:
    """The marker on *result*, if there is one this build understands.

    Every field is checked rather than assumed. This runs on whatever an owner
    sent, and an older frontend deliberately attaches to a newer owner, so a
    marker from a build that does not exist yet has to be ignored rather than
    half-read.
    """
    marker = (result.meta or {}).get(MARKER_KEY)
    if not isinstance(marker, dict):
        return None
    if marker.get("v") != MARKER_VERSION:
        logger.info(
            "Ignoring an auth marker from a newer shared browser (version %s)",
            marker.get("v"),
        )
        return None
    if marker.get("reason") not in ("missing", "stale"):
        return None
    if not isinstance(marker.get("replayable"), bool) or not isinstance(
        marker.get("browser_open"), bool
    ):
        return None
    if not isinstance(marker.get("generation"), (str, type(None))):
        return None
    return marker


async def _wait_for_the_sign_in(timeout: float) -> bool:
    """Whether a session exists once the login this process started has run.

    Imported at the call rather than at module scope, like the repair below and
    for the same reason: the login path pulls in the browser stack, and this
    module is imported whenever a server is assembled.
    """
    from linkedin_mcp_server.bootstrap import wait_for_login_to_finish

    return await wait_for_login_to_finish(timeout)


async def _repair_auth_locally(reason: str, generation: str | None) -> None:
    """Get a usable session onto disk, in this process.

    Imported here rather than at module scope: this module is imported when a
    server is assembled, and the login path pulls in the browser stack.
    """
    from linkedin_mcp_server.bootstrap import (
        invalidate_auth_and_trigger_relogin,
        start_login_if_needed,
    )

    if reason == "missing":
        # The generation goes with it too. A missing session is diagnosed from
        # disk, so two frontends can reach this at once, and the login itself
        # rotates once it holds the profile: without the generation the second
        # one retires the session the first has just created.
        await start_login_if_needed(superseded_by=generation)
        return

    if reason == "stale":
        # The old session is still on disk and still does not work, so readiness
        # would keep passing it. This is the path that retires it first, and it
        # always raises: the login it starts is what the caller waits on.
        #
        # The generation goes with it so a frontend arriving late does not rotate
        # away a session another one has already established.
        await invalidate_auth_and_trigger_relogin(stale_generation=generation)
