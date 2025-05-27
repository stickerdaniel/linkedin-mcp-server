# src/linkedin_mcp_server/config/providers.py
from typing import Dict, Optional, List
import os
import platform
import logging
import keyring
from keyring.errors import KeyringError

# Constants
SERVICE_NAME = "linkedin_mcp_server"
EMAIL_KEY = "linkedin_email"
PASSWORD_KEY = "linkedin_password"

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
