"""
Authentication logic for LinkedIn MCP Server.

Handles LinkedIn session management with persistent browser profile.
"""

import logging
import shutil
from pathlib import Path

from linkedin_mcp_server.drivers.browser import (
    DEFAULT_PROFILE_DIR,
    profile_exists,
)
from linkedin_mcp_server.exceptions import CredentialsNotFoundError

logger = logging.getLogger(__name__)


def get_authentication_source() -> bool:
    """
    Check if authentication is available via persistent profile.

    Returns:
        True if profile exists

    Raises:
        CredentialsNotFoundError: If no authentication method available
    """
    if profile_exists():
        logger.info(f"Using persistent profile from {DEFAULT_PROFILE_DIR}")
        return True

    raise CredentialsNotFoundError(
        "No LinkedIn authentication found.\n\n"
        "Options:\n"
        "  1. Run with --get-session to create a browser profile (recommended)\n"
        "  2. Run with --no-headless to login interactively\n\n"
        "For Docker users:\n"
        "  Create profile on host first: uvx linkedin-scraper-mcp --get-session\n"
        "  Then mount into Docker: -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp"
    )


def clear_profile(profile_dir: Path | None = None) -> bool:
    """
    Clear stored browser profile directory.

    Args:
        profile_dir: Path to profile directory

    Returns:
        True if clearing was successful
    """
    if profile_dir is None:
        profile_dir = DEFAULT_PROFILE_DIR

    if profile_dir.exists():
        try:
            shutil.rmtree(profile_dir)
            logger.info(f"Profile cleared from {profile_dir}")
            return True
        except OSError as e:
            logger.warning(f"Could not clear profile: {e}")
            return False
    return True
