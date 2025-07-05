"""
Custom exceptions for LinkedIn MCP Server.

This module defines specific exception types for different error scenarios
to provide better error handling and reporting to MCP clients.
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
