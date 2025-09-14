# src/linkedin_mcp_server/config/providers.py
"""
Configuration providers for LinkedIn MCP Server.

This module provides secure credential storage and retrieval using the system keyring,
as well as utility functions for Chrome driver path detection. It abstracts the
complexity of different keyring backends across macOS, Windows, and Linux.

Key Functions:
- System keyring integration for LinkedIn credentials and cookies
- Chrome driver path detection across different operating systems
- Secure credential management with proper error handling
- Cross-platform compatibility with appropriate keyring backends
"""

import logging
import os
import platform
from typing import Dict, List, Optional

import keyring
from keyring.errors import KeyringError

# Constants
SERVICE_NAME = "linkedin_mcp_server"
EMAIL_KEY = "linkedin_email"
PASSWORD_KEY = "linkedin_password"
COOKIE_KEY = "linkedin_cookie"

logger = logging.getLogger(__name__)


def get_keyring_name() -> str:
    """Get the name of the current keyring backend."""
    system = platform.system()
    if system == "Darwin":
        return "macOS Keychain"
    elif system == "Windows":
        return "Windows Credential Locker"
    else:
        return keyring.get_keyring().__class__.__name__


def get_secret_from_keyring(key: str) -> Optional[str]:
    """Retrieve a secret from system keyring."""
    try:
        secret = keyring.get_password(SERVICE_NAME, key)
        return secret
    except KeyringError as e:
        logger.error(f"Error accessing keyring for {key}: {e}")
        return None


def set_secret_in_keyring(key: str, value: str) -> bool:
    """Store a secret in system keyring."""
    try:
        keyring.set_password(SERVICE_NAME, key, value)
        logger.debug(f"Secret '{key}' stored successfully in {get_keyring_name()}")
        return True
    except KeyringError as e:
        logger.error(f"Error storing secret '{key}': {e}")
        return False


def get_credentials_from_keyring() -> Dict[str, Optional[str]]:
    """Retrieve LinkedIn credentials from system keyring."""
    email = get_secret_from_keyring(EMAIL_KEY)
    password = get_secret_from_keyring(PASSWORD_KEY)

    return {"email": email, "password": password}


def save_credentials_to_keyring(email: str, password: str) -> bool:
    """Save LinkedIn credentials to system keyring."""
    email_saved = set_secret_in_keyring(EMAIL_KEY, email)
    password_saved = set_secret_in_keyring(PASSWORD_KEY, password)

    return email_saved and password_saved


def clear_credentials_from_keyring() -> bool:
    """Clear stored credentials from the keyring."""
    try:
        keyring.delete_password(SERVICE_NAME, EMAIL_KEY)
        keyring.delete_password(SERVICE_NAME, PASSWORD_KEY)
        logger.info(f"Credentials removed from {get_keyring_name()}")
        return True
    except KeyringError as e:
        logger.error(f"Error clearing credentials: {e}")
        return False


def get_cookie_from_keyring() -> Optional[str]:
    """Retrieve LinkedIn cookie from system keyring."""
    return get_secret_from_keyring(COOKIE_KEY)


def save_cookie_to_keyring(cookie: str) -> bool:
    """Save LinkedIn cookie to system keyring."""
    return set_secret_in_keyring(COOKIE_KEY, cookie)


def clear_cookie_from_keyring() -> bool:
    """Clear stored cookie from the keyring."""
    try:
        keyring.delete_password(SERVICE_NAME, COOKIE_KEY)
        logger.info(f"Cookie removed from {get_keyring_name()}")
        return True
    except KeyringError as e:
        logger.error(f"Error clearing cookie: {e}")
        return False


def check_keychain_data_exists() -> Dict[str, bool]:
    """Check what LinkedIn data exists in the keyring."""
    credentials = get_credentials_from_keyring()
    cookie = get_cookie_from_keyring()

    return {
        "has_email": credentials["email"] is not None,
        "has_password": credentials["password"] is not None,
        "has_cookie": cookie is not None,
        "has_credentials": credentials["email"] is not None
        or credentials["password"] is not None,
        "has_any": credentials["email"] is not None
        or credentials["password"] is not None
        or cookie is not None,
    }


def clear_existing_keychain_data() -> Dict[str, bool]:
    """Clear only existing LinkedIn data from the keyring."""
    existing = check_keychain_data_exists()
    results = {"credentials_cleared": False, "cookie_cleared": False}

    # Only try to clear credentials if they exist
    if existing["has_credentials"]:
        try:
            if existing["has_email"]:
                keyring.delete_password(SERVICE_NAME, EMAIL_KEY)
            if existing["has_password"]:
                keyring.delete_password(SERVICE_NAME, PASSWORD_KEY)
            results["credentials_cleared"] = True
            logger.info(f"Credentials removed from {get_keyring_name()}")
        except KeyringError as e:
            logger.error(f"Error clearing credentials: {e}")
    else:
        results["credentials_cleared"] = True  # Nothing to clear = success

    # Only try to clear cookie if it exists
    if existing["has_cookie"]:
        try:
            keyring.delete_password(SERVICE_NAME, COOKIE_KEY)
            results["cookie_cleared"] = True
            logger.info(f"Cookie removed from {get_keyring_name()}")
        except KeyringError as e:
            logger.error(f"Error clearing cookie: {e}")
    else:
        results["cookie_cleared"] = True  # Nothing to clear = success

    return results


def clear_all_keychain_data() -> bool:
    """Clear all stored LinkedIn data from the keyring (credentials + cookie)."""
    results = clear_existing_keychain_data()

    if results["credentials_cleared"] and results["cookie_cleared"]:
        logger.info(f"All LinkedIn data cleared from {get_keyring_name()}")
        return True
    else:
        logger.error("Failed to clear some LinkedIn data from keyring")
        return False


def get_chromedriver_paths() -> List[str]:
    """Get possible ChromeDriver paths based on the platform."""
    paths = [
        os.path.join(os.path.dirname(__file__), "../../../../drivers/chromedriver"),
        os.path.join(os.path.expanduser("~"), "chromedriver"),
        "/usr/local/bin/chromedriver",
        "/usr/bin/chromedriver",
        "/opt/homebrew/bin/chromedriver",
        "/Applications/chromedriver",
    ]

    if platform.system() == "Windows":
        paths.extend(
            [
                "C:\\Program Files\\chromedriver.exe",
                "C:\\Program Files (x86)\\chromedriver.exe",
            ]
        )

    return paths
