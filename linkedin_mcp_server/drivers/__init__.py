"""
Browser management package for LinkedIn scraping.

This package provides Playwright browser management using persistent browser context.
It implements a singleton pattern for browser instances to ensure session persistence
across multiple tool calls while handling authentication, session management, and
proper resource cleanup.

Key Components:
- Playwright persistent browser context for automatic session persistence
- LinkedIn authentication with browser profile storage
- Singleton pattern for browser reuse across tools
- Automatic cleanup and resource management
"""

from linkedin_mcp_server.drivers.browser import (
    DEFAULT_USER_DATA_DIR,
    LEGACY_SESSION_PATH,
    check_rate_limit,
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
    migrate_from_legacy_session,
    needs_migration,
    profile_exists,
    reset_browser_for_testing,
    session_exists,
    set_headless,
    validate_session,
)

__all__ = [
    "DEFAULT_USER_DATA_DIR",
    "LEGACY_SESSION_PATH",
    "check_rate_limit",
    "close_browser",
    "ensure_authenticated",
    "get_or_create_browser",
    "migrate_from_legacy_session",
    "needs_migration",
    "profile_exists",
    "reset_browser_for_testing",
    "session_exists",
    "set_headless",
    "validate_session",
]
