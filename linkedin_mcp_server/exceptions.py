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
            "  Run with --login to create a new session"
        )
        super().__init__(message or default_msg)


class BrowserSetupInProgressError(LinkedInMCPError):
    """Patchright Chromium browser setup is still running."""


class BrowserSetupFailedError(LinkedInMCPError):
    """Patchright Chromium browser setup failed."""


class AuthenticationStartedError(LinkedInMCPError):
    """Interactive LinkedIn login has been started."""


class AuthenticationInProgressError(LinkedInMCPError):
    """Interactive LinkedIn login is already running."""


class AuthenticationBootstrapFailedError(LinkedInMCPError):
    """Interactive LinkedIn login could not be completed."""


class DockerHostLoginRequiredError(LinkedInMCPError):
    """Docker runtime requires host-side login creation."""


class LinuxBrowserDependencyError(LinkedInMCPError):
    """Linux host dependencies required for Chromium are missing."""
