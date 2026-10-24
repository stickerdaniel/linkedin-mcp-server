"""What a client can learn about the session without a browser or LinkedIn.

Read from the auth root's files and this process's own state, and nothing else:
no Chromium, no setup, no login, no request. So the answer says what is stored,
never whether LinkedIn still accepts it.
"""

from typing import Any

from linkedin_mcp_server.bootstrap import _auth_ready, get_bootstrap_state
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import browser_activity
from linkedin_mcp_server.session_state import (
    get_runtime_id,
    get_source_profile_dir,
    load_source_state,
    portable_cookie_path,
    profile_exists,
)


def session_status() -> dict[str, Any]:
    """The facts ``get_session_status`` reports. No paths: they name the user."""
    profile_dir = get_source_profile_dir()
    source = load_source_state(profile_dir)
    profile = profile_exists(profile_dir)
    cookies = portable_cookie_path(profile_dir).exists()
    state = get_bootstrap_state()
    return {
        "session": {
            # What the readiness gate requires before a tool may run.
            "stored": _auth_ready(),
            "profile": profile,
            "cookies": cookies,
            "login_generation": source.login_generation if source else None,
            "created_at": source.created_at if source else None,
            "created_on": source.source_runtime_id if source else None,
        },
        "runtime": get_runtime_id(),
        "browser": {
            **browser_activity(),
            "idle_timeout_seconds": get_config().browser.browser_idle_timeout_seconds,
        },
        "setup": state.setup_state.value,
        "login": state.auth_state.value,
        "account_restricted": state.account_restricted,
    }
