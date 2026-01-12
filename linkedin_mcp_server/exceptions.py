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
