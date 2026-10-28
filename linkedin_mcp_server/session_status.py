"""What a client can learn about the session without a browser or LinkedIn.

Read from the auth root's files and this process's own state, and nothing else:
no Chromium, no setup, no login, no request. So the answer says what is stored,
never whether LinkedIn still accepts it.
"""

from typing import Any

from linkedin_mcp_server.bootstrap import _auth_ready, bootstrap_observation
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import browser_activity
from linkedin_mcp_server.server_role import process_role
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    get_source_profile_dir,
    portable_cookie_path,
    profile_exists,
)


def session_status() -> dict[str, Any]:
    """The facts ``get_session_status`` reports. No paths: they name the user."""
    profile_dir = get_source_profile_dir()
    profile = profile_exists(profile_dir)
    cookies = portable_cookie_path(profile_dir).exists()
    try:
        stored = _auth_ready(profile_dir)
    except UnicodeDecodeError:
        stored = False
    return {
        "scope": "serving_process",
        "role": process_role().value,
        "session": {
            "stored": stored,
            "profile": profile,
            "cookies": cookies,
        },
        "runtime": get_runtime_id(),
        "browser": {
            **browser_activity(),
            "idle_timeout_seconds": get_config().browser.browser_idle_timeout_seconds,
        },
        **bootstrap_observation(),
    }
