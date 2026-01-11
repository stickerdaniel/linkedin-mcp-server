"""Utility functions for LinkedIn MCP Server."""

import os


def get_linkedin_cookie() -> str | None:
    """Get LinkedIn cookie from environment variable."""
    return os.environ.get("LINKEDIN_COOKIE")
