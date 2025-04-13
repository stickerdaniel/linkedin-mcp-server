# src/linkedin_mcp_server/credentials.py
"""
Credential management for LinkedIn MCP server.

This module handles the secure storage and retrieval of LinkedIn credentials.
"""

from typing import Dict
import os
import json
from pathlib import Path
import inquirer


def setup_credentials() -> Dict[str, str]:
    """
    Ask for LinkedIn credentials during setup and store them securely.

    Returns:
        Dict[str, str]: Dictionary containing email and password
    """
    credentials_file = Path.home() / ".linkedin_mcp_credentials.json"

    if credentials_file.exists():
        try:
            with open(credentials_file, "r") as f:
                credentials = json.load(f)
                if "email" in credentials and "password" in credentials:
                    return credentials
        except Exception as e:
            print(f"Error reading credentials file: {e}")

    questions = [
        inquirer.Text("email", message="LinkedIn Email"),
        inquirer.Password("password", message="LinkedIn Password"),
    ]
    credentials = inquirer.prompt(questions)

    # Store credentials securely
    try:
        with open(credentials_file, "w") as f:
            json.dump(credentials, f)

        # Set permissions to user-only read/write
        os.chmod(credentials_file, 0o600)
        print(f"✅ Credentials stored with user-only read/write at {credentials_file}")
    except Exception as e:
        print(f"⚠️ Warning: Could not store credentials: {e}")

    return credentials
