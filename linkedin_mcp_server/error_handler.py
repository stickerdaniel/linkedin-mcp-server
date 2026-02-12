"""
Centralized error handling for LinkedIn MCP Server with structured responses.

Provides DRY approach to error handling across all tools with consistent MCP response
format, specific LinkedIn error categorization, and proper logging integration.
"""

import logging
from typing import Any, Dict

from linkedin_scraper.core.exceptions import (
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


def handle_tool_error(exception: Exception, context: str = "") -> Dict[str, Any]:
    """
    Handle errors from tool functions and return structured responses.

    Args:
        exception: The exception that occurred
        context: Context about which tool failed

    Returns:
        Structured error response dictionary
    """
    return convert_exception_to_response(exception, context)


def convert_exception_to_response(
    exception: Exception, context: str = ""
) -> Dict[str, Any]:
    """
    Convert an exception to a structured MCP response.

    Args:
        exception: The exception to convert
        context: Additional context about where the error occurred

    Returns:
        Structured error response dictionary
    """
    if isinstance(exception, CredentialsNotFoundError):
        return {
            "error": "authentication_not_found",
            "message": str(exception),
            "resolution": "Run with --get-session to create a session file",
        }

    elif isinstance(exception, SessionExpiredError):
        return {
            "error": "session_expired",
            "message": str(exception),
            "resolution": "Run with --get-session to create a new session",
        }

    elif isinstance(exception, AuthenticationError):
        return {
            "error": "authentication_failed",
            "message": str(exception),
            "resolution": "Run with --get-session to re-authenticate.",
        }

    elif isinstance(exception, RateLimitError):
        wait_time = getattr(exception, "suggested_wait_time", 300)
        return {
            "error": "rate_limit",
            "message": str(exception),
            "suggested_wait_seconds": wait_time,
            "resolution": f"LinkedIn rate limit detected. Wait {wait_time} seconds before trying again.",
        }

    elif isinstance(exception, ProfileNotFoundError):
        return {
            "error": "profile_not_found",
            "message": str(exception),
            "resolution": "Check the profile URL is correct and the profile exists.",
        }

    elif isinstance(exception, ElementNotFoundError):
        return {
            "error": "element_not_found",
            "message": str(exception),
            "resolution": "LinkedIn page structure may have changed. Please report this issue.",
        }

    elif isinstance(exception, NetworkError):
        return {
            "error": "network_error",
            "message": str(exception),
            "resolution": "Check your network connection and try again.",
        }

    elif isinstance(exception, ScrapingError):
        return {
            "error": "scraping_error",
            "message": str(exception),
            "resolution": "Failed to extract data from LinkedIn. The page structure may have changed.",
        }

    elif isinstance(exception, LinkedInScraperException):
        return {
            "error": "linkedin_scraper_error",
            "message": str(exception),
        }

    elif isinstance(exception, LinkedInMCPError):
        return {
            "error": "linkedin_mcp_error",
            "message": str(exception),
        }

    else:
        # Generic error handling with structured logging
        logger.error(
            f"Error in {context}: {exception}",
            extra={
                "context": context,
                "exception_type": type(exception).__name__,
                "exception_message": str(exception),
            },
        )
        return {
            "error": "unknown_error",
            "message": f"Failed to execute {context}: {str(exception)}",
        }
