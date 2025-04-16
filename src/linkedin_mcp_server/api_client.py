# src/linkedin_mcp_server/api_client.py
from typing import Optional
import os
import logging
from open_linkedin_api import Linkedin

logger = logging.getLogger(__name__)


class LinkedInClient:
    """LinkedIn API client singleton with error handling."""

    _instance: Optional[Linkedin] = None

    @classmethod
    def get_instance(cls) -> Linkedin:
        """Get or create the LinkedIn client instance."""
        if cls._instance is None:
            # Get credentials from environment or prompt user
            email = os.environ.get("LINKEDIN_EMAIL")
            password = os.environ.get("LINKEDIN_PASSWORD")

            if not email or not password:
                from .credentials import prompt_for_credentials

                creds = prompt_for_credentials()
                email = creds["email"]
                password = creds["password"]

            cls._instance = Linkedin(email, password)
            logger.info("LinkedIn API client initialized")

        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the client instance."""
        cls._instance = None
