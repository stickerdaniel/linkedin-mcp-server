"""
Authentication logic for LinkedIn MCP Server.

Handles LinkedIn session management with file-based session persistence
and cookie-based authentication for Docker headless mode.
"""

import logging
import os
from pathlib import Path
from typing import Literal, Optional

from linkedin_mcp_server.drivers.browser import (
    DEFAULT_SESSION_PATH,
    session_exists,
)
from linkedin_mcp_server.exceptions import CredentialsNotFoundError

logger = logging.getLogger(__name__)

AuthSource = Literal["session", "cookie"]


def get_linkedin_cookie() -> Optional[str]:
    """Get LinkedIn cookie from environment variable."""
    return os.environ.get("LINKEDIN_COOKIE")


def get_authentication_source() -> AuthSource:
    """
    Check available authentication methods in priority order.

    Priority:
    1. Session file (most reliable)
    2. LINKEDIN_COOKIE env var (Docker headless)

    Returns:
        String indicating auth source: "session" or "cookie"

    Raises:
        CredentialsNotFoundError: If no authentication method available
    """
    # Priority 1: Session file
    if session_exists():
        logger.info(f"Using session from {DEFAULT_SESSION_PATH}")
        return "session"

    # Priority 2: Cookie from environment
    if get_linkedin_cookie():
        logger.info("Using LINKEDIN_COOKIE from environment")
        return "cookie"

    raise CredentialsNotFoundError(
        "No LinkedIn authentication found.\n\n"
        "Options:\n"
        "  1. Run with --get-session to create a session file (recommended)\n"
        "  2. Set LINKEDIN_COOKIE environment variable with your li_at cookie\n"
        "  3. Run with --no-headless to login interactively\n\n"
        "For Docker users:\n"
        "  docker run -it -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \\\n"
        "    stickerdaniel/linkedin-mcp-server:latest --get-session"
    )


def clear_session(session_path: Optional[Path] = None) -> bool:
    """
    Clear stored session file.

    Args:
        session_path: Path to session file

    Returns:
        True if clearing was successful
    """
    if session_path is None:
        session_path = DEFAULT_SESSION_PATH

    if session_path.exists():
        try:
            session_path.unlink()
            logger.info(f"Session cleared from {session_path}")
            return True
        except OSError as e:
            logger.warning(f"Could not clear session: {e}")
            return False
    return True
