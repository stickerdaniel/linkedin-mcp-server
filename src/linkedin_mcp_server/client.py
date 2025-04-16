# src/linkedin_mcp_server/client.py
"""
LinkedIn API client management for MCP server.

This module manages the LinkedIn API client connection and authentication.
"""

from typing import Dict, Optional
import os
import logging
import json
from pathlib import Path
from open_linkedin_api import Linkedin
from requests.cookies import RequestsCookieJar

logger = logging.getLogger(__name__)


class LinkedInClientManager:
    """Manages LinkedIn API client connections and authentication."""

    _instance: Optional[Linkedin] = None

    @classmethod
    def get_client(cls) -> Linkedin:
        """
        Get the LinkedIn client, creating it if it doesn't exist.

        Returns:
            Linkedin: An authenticated LinkedIn API client
        """
        if cls._instance is None:
            # Check environment variables first
            email = os.environ.get("LINKEDIN_EMAIL")
            password = os.environ.get("LINKEDIN_PASSWORD")

            # Check for stored credentials if environment variables not set
            if not email or not password:
                credentials = cls._load_stored_credentials()
                if credentials:
                    email = credentials.get("email")
                    password = credentials.get("password")

            # Check for stored cookies
            cookies = cls._load_stored_cookies(email) if email else None

            if cookies:
                logger.info("Using stored cookies for authentication")
                cls._instance = Linkedin(
                    username=email, cookies=cookies, authenticate=True
                )
            elif email and password:
                logger.info("Authenticating with email and password")
                cls._instance = Linkedin(email, password)
            else:
                raise ValueError(
                    "LinkedIn credentials not available. Please set LINKEDIN_EMAIL and LINKEDIN_PASSWORD environment variables or store credentials using the credentials manager."
                )

        return cls._instance

    @classmethod
    def reset_client(cls) -> None:
        """Reset the LinkedIn client instance."""
        cls._instance = None

    @classmethod
    def _load_stored_credentials(cls) -> Dict[str, str]:
        """
        Load stored credentials from the credentials file.

        Returns:
            Dict[str, str]: Dictionary containing email and password
        """
        credentials_file = Path.home() / ".linkedin_mcp_credentials.json"
        if credentials_file.exists():
            try:
                with open(credentials_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading credentials: {e}")

        return {}

    @classmethod
    def _load_stored_cookies(cls, username: str) -> Optional[RequestsCookieJar]:
        """
        Load stored cookies for the given username.

        Args:
            username: The LinkedIn username

        Returns:
            Optional[RequestsCookieJar]: The cookies if available, None otherwise
        """
        # The Open LinkedIn API already handles cookie loading internally
        # This is just a stub in case we want to add custom cookie handling
        return None
