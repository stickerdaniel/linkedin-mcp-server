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
)

logger = logging.getLogger(__name__)


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
