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
    CredentialsNotFoundError,
    LinkedInMCPError,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)


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
        raise ToolError(
            "Authentication not found. Run with --login to create a browser profile."
        ) from exception

    elif isinstance(exception, SessionExpiredError):
        logger.warning("Session expired%s: %s", ctx, exception)
        raise ToolError(
            "Session expired. Run with --login to create a new browser profile."
        ) from exception

    elif isinstance(exception, AuthenticationError):
        logger.warning("Authentication failed%s: %s", ctx, exception)
        raise ToolError(
            "Authentication failed. Run with --login to re-authenticate."
        ) from exception

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
        raise ToolError(
            "Element not found. LinkedIn page structure may have changed."
        ) from exception

    elif isinstance(exception, NetworkError):
        logger.warning("Network error%s: %s", ctx, exception)
        raise ToolError(
            "Network error. Check your connection and try again."
        ) from exception

    elif isinstance(exception, ScrapingError):
        logger.warning("Scraping error%s: %s", ctx, exception)
        raise ToolError(
            "Scraping failed. LinkedIn page structure may have changed."
        ) from exception

    elif isinstance(exception, (LinkedInScraperException, LinkedInMCPError)):
        logger.warning("LinkedIn error%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    else:
        logger.error("Unexpected error%s: %s", ctx, exception, exc_info=True)
        raise exception
