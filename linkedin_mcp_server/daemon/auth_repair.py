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

import logging
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.core.exceptions import AccountRestrictedError
from linkedin_mcp_server.daemon.core.call import AuthRequest, Because, Repair, SignIn
from linkedin_mcp_server.session_state import PeerSessionInPlaceError
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    AuthenticationStartedError,
    AuthMissingOnOwnerError,
    OwnerCannotAuthenticateError,
)

logger = logging.getLogger(__name__)


#: Namespaced so a future fastmcp key cannot shadow it. Measured: a custom key
#: coexists with fastmcp's own ``{'fastmcp': {...}}`` entry as a separate
#: top-level key, so there is no clash to work around, only a name to keep
#: unmistakable.
MARKER_KEY = "linkedin.dev/auth"

#: Bumped when the shape below changes in a way an older reader would misread.
#:
#: Only forward tolerance is needed, and that asymmetry is deliberate rather than
#: lucky. An *older* frontend attaches to a newer owner on purpose
#: (``daemon.version.Skew.SERVICEABLE``), so it will meet markers from builds it
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


# --- The frontend's half ---------------------------------------------------------
#
# What a frontend does with the marker is decided in `daemon.core.call`, which
# owns every wait a call makes under one deadline, and carried out by
# `daemon.proxy.FrontendCallMiddleware`. What stays here is the auth knowledge
# those two need: reading a marker, starting a sign-in and waiting for it, and the
# words a client gets.


def auth_request(result: object) -> AuthRequest | None:
    """The owner's request to sign in on *result*, if it carries one this reads."""
    if not isinstance(result, ToolResult):
        return None
    marker = _readable_marker(result)
    if marker is None:
        return None
    return AuthRequest(
        reason=marker["reason"],
        replayable=marker["replayable"],
        browser_open=marker["browser_open"],
        generation=marker["generation"],
    )


async def start_sign_in(
    reason: str, generation: str | None
) -> tuple[Repair, BaseException | None]:
    """Start the repair a marker asked for, and say how starting it ended.

    Never raises an ``Exception``: every outcome is an answer the call core acts
    on, and the caller may have stopped waiting for it by the time it arrives.
    """
    try:
        await _repair_auth_locally(reason, generation)
    except PeerSessionInPlaceError:
        # Not a failure: another client signed in while this one was asking to,
        # so the session the call needs already exists. Treating it as a failed
        # repair would refuse the replay and send the user back for a retry that
        # was not needed.
        logger.info("Another client already signed in; using its session")
    except AccountRestrictedError as exc:
        return Repair.RESTRICTED, exc
    except (AuthenticationStartedError, AuthenticationInProgressError):
        # Also not a failure, and the one that mattered most. Both functions
        # behind `_repair_auth_locally` report a *started* login by raising,
        # while the window is still open and nobody has typed anything yet. Read
        # as a failure, every repair this exists to perform was abandoned one step
        # before it worked: measured on the stale path with a login that finished
        # successfully, the owner was called once and the client still got the
        # error. The missing path did the same as soon as signing in took longer
        # than the inline budget, which is every login a person actually performs.
        return Repair.STARTED, None
    except Exception as exc:
        # The login path reports itself through its own exceptions, which the
        # tool layer turns into readable messages. Whatever happened, the owner's
        # failure is still the honest answer to this call.
        logger.warning("Signing in for the shared browser failed", exc_info=True)
        return Repair.FAILED, exc
    return Repair.READY, None


async def wait_for_sign_in(timeout: float) -> tuple[SignIn, BaseException | None]:
    """Wait up to *timeout* for the login this process started.

    Zero still reaches the readiness check, which is what notices a login that
    finished in exactly the race between starting it and waiting for it.
    """
    try:
        signed_in = await _wait_for_the_sign_in(timeout)
    except AccountRestrictedError as exc:
        return SignIn.RESTRICTED, exc
    if not signed_in:
        return SignIn.NOT_YET, None
    logger.info("The sign-in finished")
    return SignIn.SIGNED_IN, None


#: What the log says when a call ends on the owner's own answer after it asked
#: for a sign-in. The differential rows read these lines, so they are kept as
#: they were when this module decided them itself.
_KEPT_THE_OWNERS_ANSWER = {
    Because.PROFILE_HELD: (
        logging.WARNING,
        "The shared browser has not released the profile yet; not signing in "
        "on this call",
    ),
    Because.SIGN_IN_UNFINISHED: (
        logging.INFO,
        "The sign-in did not finish in time; not replaying",
    ),
    Because.NOT_REPLAYABLE: (
        logging.INFO,
        "Signed in; not repeating a call that had already started",
    ),
    Because.COULD_CHANGE: (
        logging.INFO,
        "Signed in; not repeating a call that could change something",
    ),
    Because.REPLAY_EXPIRED: (
        logging.INFO,
        "The replayed call ran out of time; reporting the failure",
    ),
    Because.EXPIRED: (
        logging.INFO,
        "The call ran out of time before it could run again; reporting the "
        "owner's answer",
    ),
}


def say_why(because: Because) -> None:
    """Log why a call ended on the answer it already had.

    The reasons themselves are the call core's. Two are worth spelling out,
    because both look like a missed chance:

    * ``PROFILE_HELD``: the owner's Chromium may still hold the profile, so a
      login would fail on a lease it cannot take. The owner's own wording says
      to retry, and by the next call the close will have been confirmed.
    * ``COULD_CHANGE``: repaired, not run again. An auto-replay cannot be made
      safe here, and two rounds of trying showed why. Bounding it locally left
      the owner finishing the mutation after the frontend had reported failure,
      because cancellation is not forwarded across the hop: measured over a
      real loopback owner, the client was answered at 0.66s with an error and
      the effect landed 0.7s later. Removing the bound only moved the deadline:
      an MCP client that gives up on its own call produces the same orphan,
      measured as effects ``['sent']`` after the cancel and ``['sent', 'sent']``
      after the retry. So the user decides, who knows whether the message was
      sent: asking costs one retry, while guessing wrong sends it twice.
    """
    line = _KEPT_THE_OWNERS_ANSWER.get(because)
    if line is not None:
        logger.log(*line)


def restricted_answer(exc: BaseException | None) -> ToolResult:
    """The client's answer once LinkedIn has refused the account.

    In place of the owner's result, whose wording asks for a retry that would
    only be refused again, and without a marker, so nothing downstream reads it
    as bad auth to repair.
    """
    logger.warning("LinkedIn restricted the account; not signing in")
    return ToolResult(
        content=[mt.TextContent(type="text", text=str(exc))],
        is_error=True,
    )


async def a_repeat_could_change_something(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> bool:
    """Whether running this tool again could repeat an effect on LinkedIn.

    Public because two replays need the same answer: one after signing in, and
    one after electing a replacement owner. ``daemon.proxy`` asks it for both,
    whenever the call core asks for a classification. One rule, in one place, so
    the two cannot drift apart in the unsafe direction.

    Read from the tool's own annotations, which survive the hop
    (``ProxyProvider`` copies them), rather than from a list of names kept here:
    a list would go stale the first time a tool is added, and it would go stale
    silently in the unsafe direction.

    Unknown counts as changing something. Only ``readOnlyHint`` says otherwise,
    and a tool that declares nothing has not promised anything. Read under its
    Python name, ``read_only_hint``: the camelCase attribute is a deprecation
    shim in FastMCP 4 that a setting can switch off.
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
    return getattr(annotations, "read_only_hint", None) is not True


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
