# src/linkedin_mcp_server/config/secrets.py
import logging
from typing import Dict, Optional

import inquirer  # type: ignore

from linkedin_mcp_server.config import get_config

from .providers import (
    get_credentials_from_keyring,
    get_keyring_name,
    save_credentials_to_keyring,
)

logger = logging.getLogger(__name__)


def get_credentials() -> Optional[Dict[str, str]]:
    """Get LinkedIn credentials from config, keyring, or prompt."""
    config = get_config()

    # First, try configuration (includes environment variables)
    if config.linkedin.email and config.linkedin.password:
        print("Using LinkedIn credentials from configuration")
        return {"email": config.linkedin.email, "password": config.linkedin.password}

    # Second, try keyring if enabled
    if config.linkedin.use_keyring:
        credentials = get_credentials_from_keyring()
        if credentials["email"] and credentials["password"]:
            print(f"Using LinkedIn credentials from {get_keyring_name()}")
            return {"email": credentials["email"], "password": credentials["password"]}

    # If in non-interactive mode and no credentials found, return None
    if config.chrome.non_interactive:
        print("No credentials found in non-interactive mode")
        return None

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
        print(f"✅ Credentials stored securely in {get_keyring_name()}")
    else:
        print("⚠️ Warning: Could not store credentials in system keyring.")
        print("   Your credentials will only be used for this session.")

    return credentials
