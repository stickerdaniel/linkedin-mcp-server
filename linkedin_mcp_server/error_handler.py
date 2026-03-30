"""Map known LinkedIn exceptions to FastMCP ToolError."""

import logging
from typing import NoReturn

from fastmcp.exceptions import ToolError

from linkedin_mcp_server.exceptions import (
    AuthenticationError,
    CredentialsNotFoundError,
    LinkedInMCPError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)


def raise_tool_error(exception: Exception, context: str = "") -> NoReturn:
    """Raise ToolError for known exceptions; re-raise unknown ones."""
    ctx = f" in {context}" if context else ""

    if isinstance(exception, CredentialsNotFoundError):
        logger.warning("Credentials not found%s: %s", ctx, exception)
        raise ToolError(
            "Authentication not found. Run with --login to create a browser profile."
        ) from exception

    if isinstance(exception, SessionExpiredError):
        logger.warning("Session expired%s: %s", ctx, exception)
        raise ToolError(
            "Session expired. Run with --login to create a new browser profile."
        ) from exception

    if isinstance(exception, AuthenticationError):
        logger.warning("Authentication failed%s: %s", ctx, exception)
        raise ToolError(
            "Authentication failed. Run with --login to re-authenticate."
        ) from exception

    if isinstance(exception, RateLimitError):
        wait_time = getattr(exception, "suggested_wait_time", 300)
        logger.warning("Rate limit%s: %s (wait=%ds)", ctx, exception, wait_time)
        raise ToolError(
            f"Rate limit detected. Wait {wait_time} seconds before trying again."
        ) from exception

    if isinstance(exception, ProfileNotFoundError):
        logger.warning("Profile not found%s: %s", ctx, exception)
        raise ToolError("Profile not found. Check the profile URL is correct.") from exception

    if isinstance(exception, NetworkError):
        logger.warning("Network error%s: %s", ctx, exception)
        raise ToolError("Network error. Check your connection and try again.") from exception

    if isinstance(exception, ScrapingError):
        logger.warning("Scraping error%s: %s", ctx, exception)
        raise ToolError("Scraping failed. LinkedIn page structure may have changed.") from exception

    if isinstance(exception, (LinkedInScraperException, LinkedInMCPError)):
        logger.warning("LinkedIn error%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    logger.error("Unexpected error%s: %s", ctx, exception, exc_info=True)
    raise exception
