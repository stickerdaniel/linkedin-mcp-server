"""Helpers used by MCP tools after bootstrap gating."""

import logging
from typing import NoReturn

from fastmcp import Context

from linkedin_mcp_server.bootstrap import (
    RuntimePolicy,
    current_login_generation,
    ensure_tool_ready_or_raise,
    get_runtime_policy,
    go_auth_quiescent,
    invalidate_auth_and_trigger_relogin,
    invalidate_browser_setup,
)
from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    AuthStaleOnOwnerError,
    BrowserBinaryMissingError,
    BrowserShutdownUnconfirmedError,
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
)
from linkedin_mcp_server.profile_lease import get_profile_lease
from linkedin_mcp_server.scraping import LinkedInExtractor
from linkedin_mcp_server.server_role import (
    ServerRole,
    a_held_profile_means_this_owner_must_go,
    process_role,
)

logger = logging.getLogger(__name__)


def _is_linux_browser_dependency_error(error: Exception) -> bool:
    message = str(error).lower()
    markers = (
        "host system is missing dependencies",
        "install-deps",
        "shared libraries",
        "libnss3",
        "libatk",
    )
    return any(marker in message for marker in markers)


def _is_browser_binary_missing_error(error: Exception) -> bool:
    message = str(error).lower()
    markers = (
        "executable doesn't exist at",
        "looks like playwright was just installed or updated",
    )
    return any(marker in message for marker in markers)


async def handle_auth_error(
    error: AuthenticationError,
    ctx: Context | None,
    *,
    nothing_ran_yet: bool = False,
) -> NoReturn:
    """Close the stale browser and trigger interactive re-login.

    In Docker mode a GUI browser cannot be opened, so we raise
    ``DockerHostLoginRequiredError`` for a consistent user message.

    *nothing_ran_yet* says whether the tool had done any work before the failure,
    which decides whether a client may safely run the call again after signing in.
    Only :func:`get_ready_extractor` can answer yes: it is the first statement of
    every tool body, so a failure there means nothing has been scraped. The 18
    catch sites in the tool bodies leave it at the default, because by then the
    scrape may be part done, and some of these tools send messages and connection
    requests. A 19th added later is non-replayable until someone says otherwise,
    which is the safe direction for a default to point.
    """
    if get_runtime_policy() == RuntimePolicy.DOCKER:
        raise DockerHostLoginRequiredError(
            "No valid LinkedIn session is available in Docker. "
            "Run --login on the host machine to create a session, "
            "then retry this tool."
        ) from error

    # Read before the close rather than after it, deliberately, though the
    # ordering is defensive rather than load-bearing today. On the forwarded tool
    # path `SequentialToolExecutionMiddleware` holds a lease reference of its own
    # from before the tool body until after this function returns
    # (`sequential_tool_middleware.py:116-130`), and `close_browser` releases only
    # the browser's reference, so no other process can write a generation in
    # between. What the order costs is nothing, and what it buys is not depending
    # on that: any future caller reaching here without the middleware, or any
    # change that releases the lease sooner, would otherwise latch onto a
    # generation written by the login it is about to ask for.
    # Read for every role, not only the owner. A DIRECT server rotates through
    # `invalidate_auth_and_trigger_relogin` exactly as a frontend does, so
    # without this it reaches the rotation with nothing to compare against and
    # the guard downstream reads the dead session as a peer's repair.
    broken_generation = current_login_generation()

    logger.warning("Stale session detected; closing browser and triggering re-login")
    try:
        await close_browser()
    except Exception as close_exc:
        logger.warning("Failed to close stale browser (ignored): %s", close_exc)
    finally:
        # In a `finally`, and that is the whole point of it. `close_browser`
        # defers cancellation until teardown finishes and then re-raises
        # `CancelledError` (`drivers/browser.py:747-757`), which is a
        # `BaseException` and passes straight through the `except Exception`
        # above. A tool timeout landing during the close therefore skipped
        # everything below, and a wedged owner stayed wedged.
        #
        # Measured: `wait_for(handle_auth_error(...), 0.02)` against a close that
        # defers its cancel left `stand_down_reason()` at None with the lease
        # still held.
        #
        # Only the request is made here. The exception below still has to be
        # raised on the normal path, and a cancelled call has nobody left to
        # raise it to, so what has to survive the cancellation is exactly this.
        a_held_profile_means_this_owner_must_go()

    if get_profile_lease().browser_open:
        # The teardown could not be confirmed, so Chromium may still be on the
        # profile, and the lease is deliberately kept until this process exits
        # (`drivers/browser.py:786-798`). Nothing that follows can work: a shared
        # owner would send a client after a lease it can never take, and a
        # single-process server would start a login whose own rotation is refused
        # for the same reason, after telling the user a window had opened.
        #
        # Checked for every role, ahead of the split. It was owner-only once, on
        # the reasoning that only a marker could mislead a client. Measured
        # otherwise: the single-process server reported "a login browser window
        # has been opened", opened none, and left a state only a restart clears.
        raise BrowserShutdownUnconfirmedError() from error

    if process_role() is ServerRole.OWNER:
        # The browser is down and the lease is free, so the client can take over.
        # Recorded before raising so the next forwarded call is answered from the
        # latch rather than by opening Chromium again.
        go_auth_quiescent(broken_generation)
        raise AuthStaleOnOwnerError(
            "The shared LinkedIn browser's session stopped working, and it "
            "cannot sign in by itself. Retry this tool: the client will open a "
            "login window.",
            nothing_ran_yet=nothing_ran_yet,
            # The same observation the latch was armed with, so the two can never
            # name different sessions.
            generation=broken_generation,
        ) from error

    await invalidate_auth_and_trigger_relogin(
        ctx, stale_generation=broken_generation
    )  # always raises


async def get_ready_extractor(
    ctx: Context | None,
    *,
    tool_name: str,
) -> LinkedInExtractor:
    """Run bootstrap gating, then acquire an authenticated extractor."""
    try:
        await ensure_tool_ready_or_raise(tool_name, ctx)
        browser = await get_or_create_browser()
        await ensure_authenticated()
        return LinkedInExtractor(browser.page)
    except AuthenticationError as e:
        # The first statement of every tool body, so a failure here means the
        # scrape has not started and the client may safely run the call again once
        # it has signed in.
        await handle_auth_error(e, ctx, nothing_ran_yet=True)  # always raises
    except Exception as e:
        if isinstance(e, NetworkError) and _is_browser_binary_missing_error(e):
            invalidate_browser_setup()
            raise_tool_error(
                BrowserBinaryMissingError(
                    "Patchright Chromium browser is missing. Run 'uv run patchright install chromium', or restart the server to auto-install."
                ),
                tool_name,
            )
        if isinstance(e, NetworkError) and _is_linux_browser_dependency_error(e):
            raise_tool_error(
                LinuxBrowserDependencyError(
                    "Chromium could not start because required system libraries are missing on this Linux host. Install the needed browser dependencies or use the Docker setup instead."
                ),
                tool_name,
            )
        raise_tool_error(e, tool_name)  # NoReturn
