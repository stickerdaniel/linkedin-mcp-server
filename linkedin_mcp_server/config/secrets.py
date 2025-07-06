# src/linkedin_mcp_server/config/secrets.py
import logging
from typing import Dict

import inquirer  # type: ignore

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.exceptions import CredentialsNotFoundError

from .providers import (
    get_credentials_from_keyring,
    get_keyring_name,
    save_credentials_to_keyring,
    get_cookie_from_keyring,
    save_cookie_to_keyring,
)

logger = logging.getLogger(__name__)


def has_authentication() -> bool:
    """Check if authentication is available without triggering interactive setup."""
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
    """Get LinkedIn cookie from keyring, environment, or interactive setup."""
    config = get_config()

    # First, try environment variable
    if config.linkedin.cookie:
        logger.info("Using LinkedIn cookie from environment")
        return config.linkedin.cookie

    # Second, try keyring if enabled
    if config.linkedin.use_keyring:
        cookie = get_cookie_from_keyring()
        if cookie:
            logger.info(f"Using LinkedIn cookie from {get_keyring_name()}")
            return cookie

    # If in non-interactive mode and no cookie found, raise error
    if config.chrome.non_interactive:
        raise CredentialsNotFoundError(
            "No LinkedIn cookie found. Please provide cookie via "
            "environment variable (LINKEDIN_COOKIE) or run with --get-cookie to obtain one."
        )

    # Otherwise, prompt for cookie or setup
    return prompt_for_authentication()


def get_credentials() -> Dict[str, str]:
    """Get LinkedIn credentials from config, keyring, or prompt (legacy for --get-cookie)."""
    config = get_config()

    # First, try configuration (includes environment variables)
    if config.linkedin.email and config.linkedin.password:
        logger.info("Using LinkedIn credentials from configuration")
        return {"email": config.linkedin.email, "password": config.linkedin.password}

    # Second, try keyring if enabled
    if config.linkedin.use_keyring:
        credentials = get_credentials_from_keyring()
        if credentials["email"] and credentials["password"]:
            logger.info(f"Using LinkedIn credentials from {get_keyring_name()}")
            return {"email": credentials["email"], "password": credentials["password"]}

    # If in non-interactive mode and no credentials found, raise error
    if config.chrome.non_interactive:
        raise CredentialsNotFoundError(
            "No LinkedIn credentials found. Please provide credentials via "
            "environment variables (LINKEDIN_EMAIL, LINKEDIN_PASSWORD) or keyring."
        )

    # Otherwise, prompt for credentials
    return prompt_for_credentials()


def prompt_for_authentication() -> str:
    """Prompt user for LinkedIn cookie or setup via login."""
    print("🔗 LinkedIn MCP Server Setup")

    # Ask if user has a cookie
    has_cookie = inquirer.confirm("Do you have a LinkedIn cookie?", default=False)

    if has_cookie:
        cookie = inquirer.text("LinkedIn Cookie", validate=lambda _, x: len(x) > 10)
        if save_cookie_to_keyring(cookie):
            logger.info(f"Cookie stored securely in {get_keyring_name()}")
        else:
            logger.warning("Could not store cookie in system keyring.")
            logger.info("Your cookie will only be used for this session.")
        return cookie
    else:
        # Login flow to get cookie
        return setup_cookie_from_login()


def setup_cookie_from_login() -> str:
    """Login with credentials and capture cookie."""
    from linkedin_mcp_server.setup import capture_cookie_from_credentials

    print("🔑 LinkedIn login required to obtain cookie")
    credentials = prompt_for_credentials()

    # Use existing cookie capture functionality
    cookie = capture_cookie_from_credentials(
        credentials["email"], credentials["password"]
    )

    if cookie:
        if save_cookie_to_keyring(cookie):
            logger.info(f"Cookie stored securely in {get_keyring_name()}")
        else:
            logger.warning("Could not store cookie in system keyring.")
            logger.info("Your cookie will only be used for this session.")
        return cookie
    else:
        raise CredentialsNotFoundError("Failed to obtain LinkedIn cookie")


def prompt_for_credentials() -> Dict[str, str]:
    """Prompt user for LinkedIn credentials and store them securely."""
    print(f"🔑 LinkedIn credentials required (will be stored in {get_keyring_name()})")
    questions = [
        inquirer.Text("email", message="LinkedIn Email"),
        inquirer.Password("password", message="LinkedIn Password"),
    ]
    credentials = inquirer.prompt(questions)

    if not credentials:
        raise KeyboardInterrupt("Credential input was cancelled")

    # Store credentials securely in keyring
    if save_credentials_to_keyring(credentials["email"], credentials["password"]):
        logger.info(f"Credentials stored securely in {get_keyring_name()}")
    else:
        logger.warning("Could not store credentials in system keyring.")
        logger.info("Your credentials will only be used for this session.")

    return credentials
