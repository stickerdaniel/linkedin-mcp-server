# linkedin_mcp_server/authentication.py
"""
Pure authentication module for LinkedIn MCP Server.

This module handles authentication without any driver dependencies.
"""

import logging

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.providers import (
    clear_cookie_from_keyring,
    get_cookie_from_keyring,
    save_cookie_to_keyring,
)
from linkedin_mcp_server.exceptions import CredentialsNotFoundError

# Constants for cookie validation
MIN_RAW_COOKIE_LENGTH = 110
MIN_COOKIE_LENGTH = MIN_RAW_COOKIE_LENGTH + len("li_at=")

logger = logging.getLogger(__name__)


def has_authentication() -> bool:
    """
    Check if authentication is available without triggering setup.

    Returns:
        bool: True if authentication (cookie) is available, False otherwise
    """
    config = get_config()

    # Check environment variable
    if config.linkedin.cookie:
        return True

    # Check keyring if enabled
    if config.linkedin.use_keyring:
        cookie = get_cookie_from_keyring()
        if cookie:
            return True

    return False


def get_authentication() -> str:
    """
    Get LinkedIn cookie from available sources.

    Returns:
        str: LinkedIn session cookie

    Raises:
        CredentialsNotFoundError: If no authentication is available
    """
    config = get_config()

    # First, try environment variable
    if config.linkedin.cookie:
        logger.info("Using LinkedIn cookie from environment")
        return config.linkedin.cookie

    # Second, try keyring if enabled
    if config.linkedin.use_keyring:
        cookie = get_cookie_from_keyring()
        if cookie:
            logger.info("Using LinkedIn cookie from keyring")
            return cookie

    # No authentication available
    raise CredentialsNotFoundError("No LinkedIn cookie found")


def store_authentication(cookie: str) -> bool:
    """
    Store LinkedIn cookie securely.

    Args:
        cookie: LinkedIn session cookie to store

    Returns:
        bool: True if storage was successful, False otherwise
    """
    config = get_config()

    if config.linkedin.use_keyring:
        success = save_cookie_to_keyring(cookie)
        if success:
            logger.info("Cookie stored securely in keyring")
        else:
            logger.warning("Could not store cookie in system keyring")
        return success
    else:
        logger.info("Keyring disabled, cookie not stored")
        return False


def clear_authentication() -> bool:
    """
    Clear stored authentication.

    Returns:
        bool: True if clearing was successful, False otherwise
    """
    config = get_config()

    if config.linkedin.use_keyring:
        success = clear_cookie_from_keyring()
        if success:
            logger.info("Authentication cleared from keyring")
        else:
            logger.warning("Could not clear authentication from keyring")
        return success
    else:
        logger.info("Keyring disabled, nothing to clear")
        return True


def validate_cookie_format(cookie: str) -> bool:
    """
    Validate that the cookie has the expected format.

    Args:
        cookie: Cookie string to validate

    Returns:
        bool: True if cookie format is valid, False otherwise
    """
    if not cookie:
        return False

    # LinkedIn session cookies typically start with "li_at="
    if cookie.startswith("li_at=") and len(cookie) > MIN_COOKIE_LENGTH:
        return True

    # Also accept raw cookie values (without li_at= prefix)
    if (
        not cookie.startswith("li_at=")
        and len(cookie) > MIN_RAW_COOKIE_LENGTH
        and "=" not in cookie
    ):
        return True

    return False


def ensure_authentication() -> str:
    """
    Ensure authentication is available, raising clear error if not.

    Returns:
        str: LinkedIn session cookie

    Raises:
        CredentialsNotFoundError: If no authentication is available with clear instructions
    """
    try:
        return get_authentication()
    except CredentialsNotFoundError:
        config = get_config()

        if config.chrome.non_interactive:
            raise CredentialsNotFoundError(
                "No LinkedIn cookie found. Please provide cookie via "
                "environment variable (LINKEDIN_COOKIE) or run with --get-cookie to obtain one."
            )
        else:
            raise CredentialsNotFoundError(
                "No LinkedIn authentication found. Please run setup to configure authentication."
            )
