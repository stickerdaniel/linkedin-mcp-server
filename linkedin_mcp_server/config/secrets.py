# src/linkedin_mcp_server/config/secrets.py
"""
Interactive credential prompting and secure storage for LinkedIn MCP Server.

This module handles interactive credential collection from users and securely stores
them in the system keyring. It provides a user-friendly interface for credential
input while ensuring security through proper keyring integration.

Key Functions:
- Interactive credential prompting with secure password input
- Automatic storage of credentials in system keyring
- User-friendly error handling and feedback
- Integration with the keyring providers for secure storage
"""

import logging
from typing import Dict

import inquirer  # type: ignore


from .providers import (
    get_keyring_name,
    save_credentials_to_keyring,
)

logger = logging.getLogger(__name__)


def prompt_for_credentials() -> Dict[str, str]:
    """Prompt user for LinkedIn credentials and store them securely."""
    print(f"🔑 LinkedIn credentials required (will be stored in {get_keyring_name()})")
    questions = [
        inquirer.Text("email", message="LinkedIn Email"),
        inquirer.Password("password", message="LinkedIn Password"),
    ]
    credentials: Dict[str, str] = inquirer.prompt(questions)

    if not credentials:
        raise KeyboardInterrupt("Credential input was cancelled")

    # Store credentials securely in keyring
    if save_credentials_to_keyring(credentials["email"], credentials["password"]):
        logger.info("Credentials stored securely in keyring")
    else:
        logger.warning("Could not store credentials in system keyring")

    return credentials
