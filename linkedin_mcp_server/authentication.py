# linkedin_mcp_server/authentication.py
"""
Pure authentication logic for LinkedIn MCP Server.

Handles LinkedIn session cookie management with secure storage and retrieval.
Provides layered authentication resolution from configuration, keyring, and user input.
Implements proper error handling with context-aware messaging.
"""

import logging

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.messages import ErrorMessages, InfoMessages
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


def get_authentication() -> str:
    """
    Get LinkedIn cookie from available sources.

    Returns:
        str: LinkedIn session cookie

    Raises:
        CredentialsNotFoundError: If no authentication is available
    """
    config = get_config()

    # First, try environment variable or command line
    if config.linkedin.cookie:
        logger.info(InfoMessages.using_cookie_from("configuration"))
        return config.linkedin.cookie

    # Second, try keyring
    cookie = get_cookie_from_keyring()
    if cookie:
        logger.info(InfoMessages.using_cookie_from("keyring"))
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
    success = save_cookie_to_keyring(cookie)
    if success:
        logger.info(InfoMessages.cookie_stored_securely())
    else:
        logger.warning(InfoMessages.keyring_storage_failed())
    return success


def clear_authentication() -> bool:
    """
    Clear stored authentication.

    Returns:
        bool: True if clearing was successful, False otherwise
    """
    success = clear_cookie_from_keyring()
    if success:
        logger.info("Authentication cleared from keyring")
    else:
        logger.warning("Could not clear authentication from keyring")
    return success


def ensure_authentication() -> str:
    """
    Ensure authentication is available with clear error messages.

    Returns:
        str: Valid LinkedIn session cookie

    Raises:
        CredentialsNotFoundError: If no authentication is available with clear instructions
    """
    try:
        return get_authentication()
    except CredentialsNotFoundError:
        config = get_config()

        raise CredentialsNotFoundError(
            ErrorMessages.no_cookie_found(config.is_interactive)
        )
