# src/linkedin_mcp_server/secrets.py
"""
Secure secrets management for LinkedIn MCP server.

This module provides secure storage and retrieval of sensitive credentials
using the system's native keychain/credential manager.
"""

from typing import Dict, Optional
import os
import platform
import logging
import keyring
from keyring.errors import KeyringError
import inquirer  # type: ignore

# Service name for the keyring
SERVICE_NAME = "linkedin_mcp_server"

# Secret keys
EMAIL_KEY = "linkedin_email"
PASSWORD_KEY = "linkedin_password"

logger = logging.getLogger(__name__)


def get_keyring_name() -> str:
    """
    Get the name of the current keyring backend.

    Returns:
        str: Human-readable name of the keyring backend based on platform
    """
    system = platform.system()
    if system == "Darwin":
        return "macOS Keychain"
    elif system == "Windows":
        return "Windows Credential Locker"
    else:
        return keyring.get_keyring().__class__.__name__


def get_secret(key: str) -> Optional[str]:
    """
    Retrieve a secret from system keyring.

    Args:
        key: The key identifier for the secret

    Returns:
        Optional[str]: The secret value if found, None otherwise
    """
    try:
        secret = keyring.get_password(SERVICE_NAME, key)
        return secret
    except KeyringError as e:
        logger.error(f"Error accessing keyring for {key}: {e}")
        return None


def set_secret(key: str, value: str) -> bool:
    """
    Store a secret in system keyring.

    Args:
        key: The key identifier for the secret
        value: The secret value to store

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        keyring.set_password(SERVICE_NAME, key, value)
        logger.debug(f"Secret '{key}' stored successfully in {get_keyring_name()}")
        return True
    except KeyringError as e:
        logger.error(f"Error storing secret '{key}': {e}")
        return False


def get_credentials(non_interactive: bool = False) -> Optional[Dict[str, str]]:
    """
    Get LinkedIn credentials from environment variables, keyring, or prompt.

    Args:
        non_interactive: If True, only get credentials from environment or keyring,
                         without prompting the user.

    Returns:
        Optional[Dict[str, str]]: Dictionary containing email and password, or None if
                                  not available in non-interactive mode.
    """
    # First, try environment variables
    email = os.environ.get("LINKEDIN_EMAIL")
    password = os.environ.get("LINKEDIN_PASSWORD")

    if email and password:
        logger.info("Using LinkedIn credentials from environment variables")
        return {"email": email, "password": password}

    # Second, try keyring
    email = get_secret(EMAIL_KEY)
    password = get_secret(PASSWORD_KEY)

    if email and password:
        logger.info(f"Using LinkedIn credentials from {get_keyring_name()}")
        return {"email": email, "password": password}

    # If in non-interactive mode and we haven't found credentials yet, return None
    if non_interactive:
        logger.error("No credentials found in non-interactive mode")
        return None

    # Otherwise, prompt for credentials
    return prompt_for_credentials()


def prompt_for_credentials() -> Dict[str, str]:
    """
    Prompt user for LinkedIn credentials and store them securely.

    Returns:
        Dict[str, str]: Dictionary containing email and password
    """
    print(f"🔑 LinkedIn credentials required (will be stored in {get_keyring_name()})")
    questions = [
        inquirer.Text("email", message="LinkedIn Email"),
        inquirer.Password("password", message="LinkedIn Password"),
    ]
    credentials = inquirer.prompt(questions)

    if not credentials:
        raise KeyboardInterrupt("Credential input was cancelled")

    # Store credentials securely in keyring
    if set_secret(EMAIL_KEY, credentials["email"]) and set_secret(
        PASSWORD_KEY, credentials["password"]
    ):
        print(f"✅ Credentials stored securely in {get_keyring_name()}")
    else:
        print("⚠️ Warning: Could not store credentials in system keyring.")
        print("   Your credentials will only be used for this session.")

    return credentials


def clear_credentials() -> bool:
    """
    Clear stored credentials from the keyring.

    Returns:
        bool: True if successful, False otherwise
    """
    success = True
    try:
        # Delete both keys
        keyring.delete_password(SERVICE_NAME, EMAIL_KEY)
        keyring.delete_password(SERVICE_NAME, PASSWORD_KEY)
        print(f"✅ Credentials removed from {get_keyring_name()}")
    except KeyringError as e:
        success = False
        logger.error(f"Error clearing credentials: {e}")
        print(f"❌ Error clearing credentials: {e}")

    return success
