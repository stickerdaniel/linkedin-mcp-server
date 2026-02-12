"""
Browser management package for LinkedIn scraping.

This package provides Patchright browser management using linkedin_scraper v3's
BrowserManager with persistent context. It implements a singleton pattern for
browser instances to ensure profile persistence across multiple tool calls
while handling authentication and proper resource cleanup.

Key Components:
- Patchright browser initialization via BrowserManager with persistent profile
- LinkedIn authentication with automatic profile persistence
- Singleton pattern for browser reuse across tools
- Automatic cleanup and resource management
"""

from linkedin_mcp_server.drivers.browser import (
    DEFAULT_PROFILE_DIR,
    check_rate_limit,
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
    get_profile_dir,
    profile_exists,
    reset_browser_for_testing,
    set_headless,
    validate_session,
)

__all__ = [
    "DEFAULT_PROFILE_DIR",
    "check_rate_limit",
    "close_browser",
    "ensure_authenticated",
    "get_or_create_browser",
    "get_profile_dir",
    "profile_exists",
    "reset_browser_for_testing",
    "set_headless",
    "validate_session",
]
