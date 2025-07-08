# src/linkedin_mcp_server/exceptions.py
"""
Custom exceptions for LinkedIn MCP Server with specific error categorization.

Defines hierarchical exception types for different error scenarios including
authentication failures, driver initialization issues, and MCP client reporting.
Provides structured error handling for better debugging and user experience.
"""


class LinkedInMCPError(Exception):
    """Base exception for LinkedIn MCP Server."""

    pass


class CredentialsNotFoundError(LinkedInMCPError):
    """No credentials available in non-interactive mode."""

    pass


class DriverInitializationError(LinkedInMCPError):
    """Failed to initialize Chrome WebDriver."""

    pass
