# src/linkedin_mcp_server/credentials.py
"""
Credential management for LinkedIn MCP server.

This module handles the secure storage and retrieval of LinkedIn credentials.
"""

from typing import Dict, Optional
import os
import json
from pathlib import Path
import logging
import inquirer

logger = logging.getLogger(__name__)


def get_credentials(non_interactive: bool = False) -> Optional[Dict[str, str]]:
    """
    Get LinkedIn credentials from environment variables, stored file, or prompt.

    Args:
        non_interactive: If True, only get credentials from environment or stored file,
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

    # Second, try stored credentials file
    credentials_file = Path.home() / ".linkedin_mcp_credentials.json"
    if credentials_file.exists():
        try:
            with open(credentials_file, "r") as f:
                credentials = json.load(f)
                if "email" in credentials and "password" in credentials:
                    logger.info("Using LinkedIn credentials from stored file")
                    return credentials
        except Exception as e:
            logger.error(f"Error reading credentials file: {e}")

    # If in non-interactive mode and we haven't found credentials yet, return None
    if non_interactive:
        logger.warning("No credentials found in non-interactive mode")
        return None

    # Otherwise, prompt for credentials
    return prompt_for_credentials()


def prompt_for_credentials() -> Dict[str, str]:
    """
    Prompt user for LinkedIn credentials and store them.

    Returns:
        Dict[str, str]: Dictionary containing email and password
    """
    print("🔑 LinkedIn credentials required")
    questions = [
        inquirer.Text("email", message="LinkedIn Email"),
        inquirer.Password("password", message="LinkedIn Password"),
    ]
    credentials = inquirer.prompt(questions)

    # Store credentials securely
    try:
        credentials_file = Path.home() / ".linkedin_mcp_credentials.json"
        with open(credentials_file, "w") as f:
            json.dump(credentials, f)

        # Set permissions to user-only read/write
        os.chmod(credentials_file, 0o600)
        print(f"✅ Credentials stored with user-only read/write at {credentials_file}")
    except Exception as e:
        logger.warning(f"Could not store credentials: {e}")
        print(f"⚠️ Warning: Could not store credentials: {e}")

    return credentials
