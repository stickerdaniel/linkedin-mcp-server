"""Helpers used by MCP tools after bootstrap gating."""

import logging
from typing import NoReturn

from fastmcp import Context

from linkedin_mcp_server.bootstrap import (
    RuntimePolicy,
    ensure_tool_ready_or_raise,
    get_runtime_policy,
    invalidate_auth_and_trigger_relogin,
    invalidate_browser_setup,
)
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
from linkedin_mcp_server.core.ip_monitor import get_ip_drift_monitor
from linkedin_mcp_server.drivers.browser import (
    BrowserManager,
    _is_transport_failure,
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    BrowserBinaryMissingError,
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
)
from linkedin_mcp_server.scraping import LinkedInExtractor

logger = logging.getLogger(__name__)

# How often (in tool calls) to re-check outbound IP drift when a proxy is
# configured. A cheap fetch(), but no reason to pay it on every single call.
_IP_DRIFT_CHECK_EVERY_N_CALLS = 20
_tool_call_count = 0


def _reset_ip_drift_call_counter_for_testing() -> None:
    global _tool_call_count
    _tool_call_count = 0


async def _maybe_check_ip_drift(page: object) -> None:
    """Sample outbound-IP drift every N calls, only when a proxy is set."""
    global _tool_call_count
    if not get_config().browser.proxy_settings():
        return
    _tool_call_count += 1
    if _tool_call_count % _IP_DRIFT_CHECK_EVERY_N_CALLS != 0:
        return
    try:
        await get_ip_drift_monitor().check(page)
    except Exception:
        logger.debug("IP drift check failed (ignored)", exc_info=True)


async def _ensure_browser_alive(browser: BrowserManager) -> BrowserManager:
    """Probe the reused browser singleton's liveness; relaunch once on
    transport death.

    A cheap page.evaluate("1") proves the driver connection is actually
    alive before other code trusts the singleton. If it fails with a
    transport-failure signature (dead driver/process -- see
    drivers.browser._is_transport_failure), close and relaunch the
    singleton once before propagating. A semantic failure (real
    auth/timeout error) is NOT transport death and must not trigger a
    relaunch -- it's re-raised as-is for the normal auth-handling path.
    """
    try:
        await browser.page.evaluate("1")
        return browser
    except Exception as exc:
        if not _is_transport_failure(exc):
            raise
        logger.warning(
            "Browser liveness probe failed with a transport error; "
            "relaunching once: %s",
            exc,
        )
        await close_browser()
        return await get_or_create_browser()


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
        browser = await _ensure_browser_alive(browser)
        await ensure_authenticated()
        await _maybe_check_ip_drift(browser.page)
        return LinkedInExtractor(
            browser.page, engine=get_config().browser.browser_engine
        )
    except AuthenticationError as e:
        await handle_auth_error(e, ctx)  # always raises
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
