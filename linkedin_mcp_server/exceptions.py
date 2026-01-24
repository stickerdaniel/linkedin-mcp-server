# src/linkedin_mcp_server/exceptions.py
"""
Custom exceptions for LinkedIn MCP Server with specific error categorization.

Defines hierarchical exception types for different error scenarios including
authentication failures and MCP client reporting.
"""


class LinkedInMCPError(Exception):
    """Base exception for LinkedIn MCP Server."""

    pass


class CredentialsNotFoundError(LinkedInMCPError):
    """No credentials available in non-interactive mode."""

    pass


class SessionExpiredError(LinkedInMCPError):
    """Session has expired and needs to be refreshed."""

    def __init__(self, message: str | None = None):
        default_msg = (
            "LinkedIn session has expired.\n\n"
            "To fix this:\n"
            "  1. Run with --get-session to create a new session\n"
            "  2. Or set a fresh LINKEDIN_COOKIE environment variable"
        )
        super().__init__(message or default_msg)


class CookieAuthenticationError(LinkedInMCPError):
    """Cookie-based authentication failed."""

    def __init__(self, message: str | None = None):
        default_msg = (
            "Cookie authentication failed. The cookie may be:\n"
            "  - Expired (cookies typically last 1-7 days)\n"
            "  - Invalid (check the format)\n"
            "  - From a different account"
        )
        super().__init__(message or default_msg)


# Outreach-related exceptions


class OutreachError(LinkedInMCPError):
    """Base exception for outreach operations."""

    pass


class RateLimitExceededError(OutreachError):
    """Daily rate limit has been exceeded."""

    def __init__(
        self,
        action_type: str,
        current: int,
        limit: int,
        message: str | None = None,
    ):
        self.action_type = action_type
        self.current = current
        self.limit = limit
        default_msg = (
            f"Daily {action_type} limit exceeded: {current}/{limit}. "
            f"Try again tomorrow."
        )
        super().__init__(message or default_msg)


class OutreachPausedError(OutreachError):
    """Outreach automation has been paused."""

    def __init__(self, message: str | None = None):
        default_msg = "Outreach is currently paused. Use resume_outreach to continue."
        super().__init__(message or default_msg)


class ConnectionRequestError(OutreachError):
    """Failed to send connection request."""

    pass


class CompanyFollowError(OutreachError):
    """Failed to follow company."""

    pass


class AlreadyConnectedError(ConnectionRequestError):
    """Already connected to this person."""

    pass


class PendingConnectionError(ConnectionRequestError):
    """Connection request already pending."""

    pass


class AlreadyFollowingError(CompanyFollowError):
    """Already following this company."""

    pass


# Automation-related exceptions


class AutomationError(LinkedInMCPError):
    """Base exception for browser automation errors."""

    pass


class ElementNotFoundError(AutomationError):
    """Could not find expected element on page."""

    pass


class NavigationError(AutomationError):
    """Failed to navigate to a page."""

    pass
