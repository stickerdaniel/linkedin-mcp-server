"""Helpers used by MCP tools after bootstrap gating."""

import logging
from typing import NoReturn

from fastmcp import Context

from linkedin_mcp_server.bootstrap import (
    RuntimePolicy,
    ensure_tool_ready_or_raise,
    get_runtime_policy,
    invalidate_auth_and_trigger_relogin,
)
from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
)
from linkedin_mcp_server.scraping import LinkedInExtractor

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


async def handle_auth_error(
    error: AuthenticationError,
    ctx: Context | None,
) -> NoReturn:
    """Close the stale browser and trigger interactive re-login.

    In Docker mode a GUI browser cannot be opened, so we raise
    ``DockerHostLoginRequiredError`` for a consistent user message.
    """
    if get_runtime_policy() == RuntimePolicy.DOCKER:
        raise DockerHostLoginRequiredError(
            "No valid LinkedIn session is available in Docker. "
            "Run --login on the host machine to create a session, "
            "then retry this tool."
        ) from error

    logger.warning("Stale session detected; closing browser and triggering re-login")
    try:
        await close_browser()
    except Exception as close_exc:
        logger.warning("Failed to close stale browser (ignored): %s", close_exc)
    await invalidate_auth_and_trigger_relogin(ctx)  # always raises


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
        await handle_auth_error(e, ctx)  # always raises
    except Exception as e:
        if isinstance(e, NetworkError) and _is_linux_browser_dependency_error(e):
            raise_tool_error(
                LinuxBrowserDependencyError(
                    "Chromium could not start because required system libraries are missing on this Linux host. Install the needed browser dependencies or use the Docker setup instead."
                ),
                tool_name,
            )
        raise_tool_error(e, tool_name)  # NoReturn
