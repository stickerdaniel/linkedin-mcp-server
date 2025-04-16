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
import stat
from open_linkedin_api import Linkedin
from requests.cookies import RequestsCookieJar

logger = logging.getLogger(__name__)

# Credentials file path
CREDENTIALS_FILE = Path.home() / ".linkedin_mcp_credentials.json"


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
            # Get cookies from environment variables
            li_at = os.environ.get("LINKEDIN_LI_AT")
            jsessionid = os.environ.get("LINKEDIN_JSESSIONID")

            # Check for stored credentials if environment variables not set
            if not li_at or not jsessionid:
                stored_cookies = cls._load_stored_cookies()
                if stored_cookies:
                    li_at = stored_cookies.get("li_at")
                    jsessionid = stored_cookies.get("jsessionid")

            if not li_at or not jsessionid:
                raise ValueError(
                    "LinkedIn cookies not available. Please set LINKEDIN_LI_AT and LINKEDIN_JSESSIONID environment variables."
                )

            # Validate cookie format
            if li_at and (len(li_at) < 10 or not li_at.isalnum()):
                logger.warning("LinkedIn li_at cookie appears to be invalid")

            if jsessionid and (len(jsessionid) < 10 or not jsessionid.isalnum()):
                logger.warning("LinkedIn JSESSIONID cookie appears to be invalid")

            # Create cookie jar with secure settings
            cookies = RequestsCookieJar()
            cookies.set(
                "li_at",
                li_at,
                domain=".linkedin.com",
                secure=True,
                rest={"HttpOnly": True},
            )
            cookies.set(
                "JSESSIONID",
                f"ajax:{jsessionid}",
                domain=".linkedin.com",
                secure=True,
                rest={"HttpOnly": True},
            )

            logger.info("Using cookies for authentication")

            # Create client with secure settings
            cls._instance = Linkedin(cookies=cookies, username=None, password=None)

        return cls._instance

    @classmethod
    def reset_client(cls) -> None:
        """Reset the LinkedIn client instance."""
        cls._instance = None

    @classmethod
    def _load_stored_cookies(cls) -> Dict[str, str]:
        """
        Load stored cookies from the credentials file.

        Returns:
            Dict[str, str]: Dictionary containing li_at and jsessionid cookies
        """
        if CREDENTIALS_FILE.exists():
            # Check file permissions (should be readable only by owner)
            file_mode = CREDENTIALS_FILE.stat().st_mode
            if file_mode & (stat.S_IRWXG | stat.S_IRWXO):
                logger.warning(
                    f"Credentials file {CREDENTIALS_FILE} has unsafe permissions. "
                    "It should be readable only by the owner."
                )
                return {}

            try:
                with open(CREDENTIALS_FILE, "r") as f:
                    credentials = json.load(f)
                    cookies = {}
                    if "li_at" in credentials:
                        cookies["li_at"] = credentials["li_at"]
                    if "jsessionid" in credentials:
                        cookies["jsessionid"] = credentials["jsessionid"]
                    return cookies
            except Exception as e:
                logger.error(f"Error loading cookies: {e}")

        return {}

    @classmethod
    def store_cookies(cls, li_at: str, jsessionid: str) -> bool:
        """
        Securely store cookies to the credentials file.

        Args:
            li_at: LinkedIn li_at cookie value
            jsessionid: LinkedIn JSESSIONID cookie value (without 'ajax:' prefix)

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            credentials = {"li_at": li_at, "jsessionid": jsessionid}

            # Write to file
            with open(CREDENTIALS_FILE, "w") as f:
                json.dump(credentials, f)

            # Set restrictive permissions (user read/write only)
            CREDENTIALS_FILE.chmod(0o600)

            logger.info(f"Cookies stored securely at {CREDENTIALS_FILE}")
            return True
        except Exception as e:
            logger.error(f"Failed to store cookies: {e}")
            return False
