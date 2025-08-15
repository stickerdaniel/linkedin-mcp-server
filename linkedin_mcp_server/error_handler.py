# src/linkedin_mcp_server/error_handler.py
"""
Centralized error handling for LinkedIn MCP Server with structured responses.

Provides DRY approach to error handling across all tools with consistent MCP response
format, specific LinkedIn error categorization, and proper logging integration.
Eliminates code duplication while ensuring user-friendly error messages.
"""

import logging
from typing import Any, Dict, List

try:
    from linkedin_scraper.exceptions import (
        CaptchaRequiredError,
        InvalidCredentialsError,
        LoginTimeoutError,
        RateLimitError,
        SecurityChallengeError,
        TwoFactorAuthError,
    )
except ImportError:
    # Fallback if linkedin_scraper is not available
    class CaptchaRequiredError(Exception):
        pass

    class InvalidCredentialsError(Exception):
        pass

    class LoginTimeoutError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class SecurityChallengeError(Exception):
        pass

    class TwoFactorAuthError(Exception):
        pass


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

        # Check for common fast-linkedin-scraper errors by error message
        error_message = str(exception).lower()

        if "cookie" in error_message and (
            "invalid" in error_message or "expired" in error_message
        ):
            return {
                "error": "invalid_cookie",
                "message": str(exception),
                "resolution": "LinkedIn cookie has expired or is invalid. Please obtain a fresh cookie.",
            }
        elif "authentication" in error_message or "login" in error_message:
            return {
                "error": "authentication_failed",
                "message": str(exception),
                "resolution": "Authentication failed. Check your LinkedIn credentials or cookie.",
            }
        elif "rate" in error_message or "limit" in error_message:
            return {
                "error": "rate_limited",
                "message": str(exception),
                "resolution": "Rate limited by LinkedIn. Wait before making more requests.",
            }
        elif "captcha" in error_message:
            return {
                "error": "captcha_required",
                "message": str(exception),
                "resolution": "Captcha required. Use --no-headless mode to solve manually.",
            }

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
    Only used for legacy linkedin-scraper. Fast-linkedin-scraper handles its own browser management.

    Returns:
        Driver instance

    Raises:
        LinkedInMCPError: If driver initialization fails
    """
    from linkedin_mcp_server.config import get_config

    config = get_config()

    # Only create Chrome driver for legacy scraper
    if config.linkedin.scraper_type == "linkedin-scraper":
        from linkedin_mcp_server.authentication import ensure_authentication
        from linkedin_mcp_server.drivers.chrome import get_or_create_driver

        # Get authentication first
        authentication = ensure_authentication()

        # Create driver with authentication
        driver = get_or_create_driver(authentication)

        return driver
    else:
        # For fast-linkedin-scraper, we don't use Chrome driver
        raise LinkedInMCPError(
            "safe_get_driver() should not be called when using fast-linkedin-scraper"
        )
