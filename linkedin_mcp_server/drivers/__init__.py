"""
Browser management package for LinkedIn scraping.

This package provides Playwright browser management using linkedin_scraper v3's
BrowserManager. It implements a singleton pattern for browser instances
to ensure session persistence across multiple tool calls while handling
authentication, session management, and proper resource cleanup.

Key Components:
- Playwright browser initialization via BrowserManager
- LinkedIn authentication with session persistence
- Singleton pattern for browser reuse across tools
- Automatic cleanup and resource management
"""

from linkedin_mcp_server.drivers.browser import (
    DEFAULT_SESSION_PATH,
    check_rate_limit,
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
    session_exists,
    set_headless,
    validate_session,
)

__all__ = [
    "DEFAULT_SESSION_PATH",
    "check_rate_limit",
    "close_browser",
    "ensure_authenticated",
    "get_or_create_browser",
    "session_exists",
    "set_headless",
    "validate_session",
]
