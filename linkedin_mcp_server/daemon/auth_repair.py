"""Getting a login done in the process that can actually show one.

The owner notices bad auth but has nobody in front of it to type credentials;
the frontend has a user. This module carries the fact across the hop and acts
on it in the frontend.

- The refusal travels as an error result carrying a marker, not as an
  exception: with ``mask_error_details`` a re-raised exception arrives as a bare
  ``Error calling tool``, while a result's ``meta`` and ``isError`` cross intact.
- The owner middleware finds the failure through ``__cause__``, not a
  ``ContextVar``, which a sync tool run in a worker thread would not share. This
  relies on ``raise_tool_error`` passing an already-shaped ``ToolError`` through.
- ``reason`` says what to repair (a missing session needs a login, a stale one
  needs retiring first); ``replayable`` says whether the call may run again.
  Only the owner can decide ``replayable``: by the time a failure reaches a
  client every origin looks the same.
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


#: Namespaced so a future fastmcp key cannot shadow it; it sits beside
#: fastmcp's own ``fastmcp`` entry.
MARKER_KEY = "linkedin.dev/auth"

#: Bumped when the shape below changes in a way an older reader would misread.
#: Only forward tolerance is needed: an older frontend may attach to a newer
#: owner (``daemon.version.Skew.SERVICEABLE``) and must ignore unknown markers,
#: while a newer frontend retires an older owner rather than talking to it.
#:
#: 2 added ``generation``. A version-1 reader would repair without it and could
#: rotate away a peer's fresh session, so it refuses the marker and reports the
#: owner's failure instead.
MARKER_VERSION = 2


def _auth_failure_in(exc: BaseException) -> OwnerCannotAuthenticateError | None:
    """Find the owner's auth refusal in an exception chain, if it is there.

    Walked, because the middleware only sees the ``ToolError`` that
    ``raise_tool_error`` wraps it in.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, OwnerCannotAuthenticateError):
            return current
        current = current.__cause__
    return None


class OwnerAuthSignalMiddleware(Middleware):
    """Turn the owner's auth refusal into something the hop preserves.

    Installed only on an owner: elsewhere it would announce a repair nobody
    asked for, and on a frontend it would compete with the code that acts on
    the marker.
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
                # Which session the owner found broken, so a second frontend
                # does not rotate away the first one's fresh login. Taken from
                # the failure, not read again: the lease is free by now, so a
                # fresh read could return another process's new generation.
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

    A login cannot start while this is true, since it takes the profile
    exclusively. Only a confirmed close clears it, the cautious direction.
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
        # Also not a failure: both repair paths report a *started* login by
        # raising while its window is still open. Read as a failure, every
        # repair would be abandoned one step before it worked.
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

    The reasons are the call core's. Two look like a missed chance:

    * ``PROFILE_HELD``: the owner's Chromium may still hold the profile, so a
      login would fail; by the next call the close will be confirmed.
    * ``COULD_CHANGE``: repaired but not run again. Cancellation does not
      cross the hop, so a bounded or client-cancelled replay can still land
      after a reported failure. The user knows whether the action happened;
      guessing wrong would send it twice.
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

    One rule for both replays, after signing in and after electing a
    replacement owner, so they cannot drift apart. Read from the tool's
    annotations, which survive the hop, never from a name list that would go
    stale in the unsafe direction. Unknown counts as changing something; only
    ``read_only_hint`` says otherwise, read under its Python name because the
    camelCase attribute is a FastMCP 4 shim a setting can switch off.
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

    Every field is checked: an older frontend may attach to a newer owner, so
    an unknown marker is ignored rather than half-read.
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

    Imported at the call, like the repair below: the login path pulls in the
    browser stack.
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
