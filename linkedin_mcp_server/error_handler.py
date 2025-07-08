# src/linkedin_mcp_server/error_handler.py
"""
Centralized error handling for LinkedIn MCP Server with structured responses.

Provides DRY approach to error handling across all tools with consistent MCP response
format, specific LinkedIn error categorization, and proper logging integration.
Eliminates code duplication while ensuring user-friendly error messages.
"""

import logging
from typing import Any, Dict, List

from linkedin_scraper.exceptions import (
    CaptchaRequiredError,
    InvalidCredentialsError,
    LoginTimeoutError,
    RateLimitError,
    SecurityChallengeError,
    TwoFactorAuthError,
)

from linkedin_mcp_server.exceptions import (
    CredentialsNotFoundError,
    LinkedInMCPError,
)


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


def handle_tool_error_list(
    exception: Exception, context: str = ""
) -> List[Dict[str, Any]]:
    """
    Handle errors from tool functions that return lists.

    Args:
        exception: The exception that occurred
        context: Context about which tool failed

    Returns:
        List containing structured error response dictionary
    """
    return convert_exception_to_list_response(exception, context)


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
            "resolution": "Provide LinkedIn cookie via LINKEDIN_COOKIE environment variable or run setup",
        }

    elif isinstance(exception, InvalidCredentialsError):
        return {
            "error": "invalid_credentials",
            "message": str(exception),
            "resolution": "Check your LinkedIn email and password",
        }

    elif isinstance(exception, CaptchaRequiredError):
        return {
            "error": "captcha_required",
            "message": str(exception),
            "captcha_url": exception.captcha_url,
            "resolution": "Complete the captcha challenge manually",
        }

    elif isinstance(exception, SecurityChallengeError):
        return {
            "error": "security_challenge_required",
            "message": str(exception),
            "challenge_url": getattr(exception, "challenge_url", None),
            "resolution": "Complete the security challenge manually",
        }

    elif isinstance(exception, TwoFactorAuthError):
        return {
            "error": "two_factor_auth_required",
            "message": str(exception),
            "resolution": "Complete 2FA verification",
        }

    elif isinstance(exception, RateLimitError):
        return {
            "error": "rate_limit",
            "message": str(exception),
            "resolution": "Wait before attempting to login again",
        }

    elif isinstance(exception, LoginTimeoutError):
        return {
            "error": "login_timeout",
            "message": str(exception),
            "resolution": "Check network connection and try again",
        }

    elif isinstance(exception, LinkedInMCPError):
        return {"error": "linkedin_error", "message": str(exception)}

    else:
        # Generic error handling with structured logging
        logger = logging.getLogger(__name__)
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


def convert_exception_to_list_response(
    exception: Exception, context: str = ""
) -> List[Dict[str, Any]]:
    """
    Convert an exception to a list-formatted structured MCP response.

    Some tools return lists, so this provides the same error handling
    but wrapped in a list format.

    Args:
        exception: The exception to convert
        context: Additional context about where the error occurred

    Returns:
        List containing single structured error response dictionary
    """
    return [convert_exception_to_response(exception, context)]


def safe_get_driver():
    """
    Safely get or create a driver with proper error handling.

    Returns:
        Driver instance

    Raises:
        LinkedInMCPError: If driver initialization fails
    """
    from linkedin_mcp_server.authentication import ensure_authentication
    from linkedin_mcp_server.drivers.chrome import get_or_create_driver

    # Get authentication first
    authentication = ensure_authentication()

    # Create driver with authentication
    driver = get_or_create_driver(authentication)

    return driver
