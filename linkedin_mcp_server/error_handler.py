"""
Centralized error handling for LinkedIn MCP Server tools.

This module provides a DRY approach to error handling across all tools,
eliminating code duplication and ensuring consistent error responses.
"""

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


def handle_linkedin_errors(func):
    """
    Decorator to handle LinkedIn MCP errors consistently across all tools.

    This decorator wraps tool functions and converts exceptions into
    structured error responses that MCP clients can understand.

    Args:
        func: The tool function to wrap

    Returns:
        The decorated function that returns structured error responses
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return convert_exception_to_response(e, func.__name__)

    return wrapper


def handle_linkedin_errors_list(func):
    """
    Decorator to handle LinkedIn MCP errors for functions that return lists.

    Similar to handle_linkedin_errors but returns errors in list format.

    Args:
        func: The tool function to wrap

    Returns:
        The decorated function that returns structured error responses in list format
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return convert_exception_to_list_response(e, func.__name__)

    return wrapper


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
            "error": "credentials_not_found",
            "message": str(exception),
            "resolution": "Provide LinkedIn credentials via environment variables",
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
        # Generic error handling
        print(f"❌ Error in {context}: {exception}")
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
        Driver instance or None if initialization fails

    Raises:
        LinkedInMCPError: If driver initialization fails in non-interactive mode
    """
    from linkedin_mcp_server.drivers.chrome import get_or_create_driver

    driver = get_or_create_driver()
    if not driver:
        from linkedin_mcp_server.exceptions import DriverInitializationError

        raise DriverInitializationError("Failed to initialize Chrome driver")

    return driver
