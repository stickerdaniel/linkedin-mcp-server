"""
Authentication logic for LinkedIn MCP Server.

Handles LinkedIn session management with persistent browser profile.
"""

import logging
import shutil
from pathlib import Path

from linkedin_mcp_server.session_state import (
    clear_auth_state as clear_all_auth_state,
    get_source_profile_dir,
    portable_cookie_path,
    profile_exists,
    source_state_path,
    load_source_state,
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
    profile_dir = get_source_profile_dir()
    cookies_path = portable_cookie_path(profile_dir)
    source_state = load_source_state(profile_dir)
    if profile_exists(profile_dir) and cookies_path.exists() and source_state:
        logger.info("Using source profile from %s", profile_dir)
        return True

    if profile_exists(profile_dir) or cookies_path.exists():
        raise CredentialsNotFoundError(
            "LinkedIn source session metadata is missing or incomplete.\n\n"
            f"Expected source metadata: {source_state_path(profile_dir)}\n"
            f"Expected portable cookies: {cookies_path}\n\n"
            "Run with --login to create a fresh source session generation."
        )

    raise CredentialsNotFoundError(
        "No LinkedIn source session found.\n\n"
        "Options:\n"
        "  1. Run with --login to create a source browser profile (recommended)\n"
        "  2. Run with --no-headless to login interactively\n\n"
        "For Docker users:\n"
        "  Create profile on host first: uvx linkedin-scraper-mcp --login\n"
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
        profile_dir = get_source_profile_dir()

    if profile_dir.exists():
        try:
            shutil.rmtree(profile_dir)
            logger.info(f"Profile cleared from {profile_dir}")
            return True
        except OSError as e:
            logger.warning(f"Could not clear profile: {e}")
            return False
    return True


def clear_auth_state(profile_dir: Path | None = None) -> bool:
    """Clear source session artifacts and all derived runtime sessions."""
    return clear_all_auth_state(profile_dir or get_source_profile_dir())
