"""
Centralized error handling for LinkedIn MCP Server using FastMCP ToolError.

Provides raise_tool_error() which maps known LinkedIn exceptions to user-friendly
ToolError messages. Unknown exceptions are re-raised as-is for mask_error_details
to handle.
"""

import logging
from typing import NoReturn

from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    ElementNotFoundError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
)

from linkedin_mcp_server.exceptions import (
    AuthenticationBootstrapFailedError,
    AuthenticationInProgressError,
    AuthenticationStartedError,
    BrowserSetupFailedError,
    BrowserSetupInProgressError,
    CredentialsNotFoundError,
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
    LinkedInMCPError,
    SessionExpiredError,
)
from linkedin_mcp_server.error_diagnostics import (
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)

logger = logging.getLogger(__name__)


def _raise_tool_error_with_diagnostics(
    exception: Exception,
    message: str,
    *,
    context: str,
) -> NoReturn:
    try:
        diagnostics = build_issue_diagnostics(exception, context=context)
    except Exception:
        logger.debug("Could not build issue diagnostics", exc_info=True)
        diagnostics = None

    if diagnostics is not None:
        message = format_tool_error_with_diagnostics(message, diagnostics)
    raise ToolError(message) from exception


def raise_tool_error(exception: Exception, context: str = "") -> NoReturn:
    """
    Raise a ToolError for known LinkedIn exceptions, or re-raise unknown ones.

    Known exceptions are mapped to user-friendly messages via ToolError.
    Unknown exceptions are re-raised as-is so mask_error_details can mask them.

    Args:
        exception: The exception that occurred
        context: Optional context about which tool failed (for log correlation)

    Raises:
        ToolError: For known LinkedIn exception types
        Exception: Re-raises unknown exceptions as-is
    """
    ctx = f" in {context}" if context else ""

    if isinstance(exception, CredentialsNotFoundError):
        logger.warning("Credentials not found%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Authentication not found. Run with --login to create a browser profile.",
            context=context,
        )

    elif isinstance(exception, BrowserSetupInProgressError):
        logger.info("Browser setup in progress%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, BrowserSetupFailedError):
        logger.warning("Browser setup failed%s: %s", ctx, exception)
        raise ToolError(
            "LinkedIn browser setup was not ready. A fresh setup attempt has started in the background. Retry this tool in a few minutes."
        ) from exception

    elif isinstance(exception, AuthenticationStartedError):
        logger.info("Authentication started%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, AuthenticationInProgressError):
        logger.info("Authentication in progress%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, AuthenticationBootstrapFailedError):
        logger.warning("Authentication bootstrap failed%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, DockerHostLoginRequiredError):
        logger.warning("Docker host login required%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, LinuxBrowserDependencyError):
        logger.warning("Linux browser dependency missing%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, SessionExpiredError):
        logger.warning("Session expired%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Session expired. Run with --login to create a new browser profile.",
            context=context,
        )

    elif isinstance(exception, AuthenticationError):
        logger.warning("Authentication failed%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Authentication failed. Run with --login to re-authenticate.",
            context=context,
        )

    elif isinstance(exception, RateLimitError):
        wait_time = getattr(exception, "suggested_wait_time", 300)
        logger.warning("Rate limit%s: %s (wait=%ds)", ctx, exception, wait_time)
        raise ToolError(
            f"Rate limit detected. Wait {wait_time} seconds before trying again."
        ) from exception

    elif isinstance(exception, ProfileNotFoundError):
        logger.warning("Profile not found%s: %s", ctx, exception)
        raise ToolError(
            "Profile not found. Check the profile URL is correct."
        ) from exception

    elif isinstance(exception, ElementNotFoundError):
        logger.warning("Element not found%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Element not found. LinkedIn page structure may have changed.",
            context=context,
        )

    elif isinstance(exception, NetworkError):
        logger.warning("Network error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Network error. Check your connection and try again.",
            context=context,
        )

    elif isinstance(exception, ScrapingError):
        logger.warning("Scraping error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Scraping failed. LinkedIn page structure may have changed.",
            context=context,
        )

    elif isinstance(exception, (LinkedInScraperException, LinkedInMCPError)):
        # Catch-all for base exception types and any future subclasses
        # without a dedicated handler above. Passes through str(exception).
        logger.warning("LinkedIn error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            str(exception),
            context=context,
        )

    else:
        logger.error("Unexpected error%s: %s", ctx, exception, exc_info=True)
        raise exception
