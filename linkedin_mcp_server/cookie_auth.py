"""Non-interactive LinkedIn session seeding from a raw cookie value.

Mirrors the ``browser_import.orchestrate`` stage -> validate -> persist sequence,
but sources the ``li_at`` cookie from the ``--cookie`` argument (or the
``LINKEDIN_COOKIE`` env fallback) instead of a locally logged-in browser's
keychain. This is the headless / remote-server auth path: no browser window and
no host keychain are needed, so the server can authenticate on a box with no
display. The validated cookies are written to the same portable ``cookies.json``
the Docker bridge already consumes, so the rest of the auth flow is unchanged.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from linkedin_mcp_server.common_utils import harden_linkedin_tree, secure_write_text
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

logger = logging.getLogger(__name__)

_PRIVATE_FILE_MODE = 0o600
_LINKEDIN_DOMAIN = ".linkedin.com"


def parse_linkedin_cookie(value: str) -> list[dict[str, object]]:
    """Parse a ``--cookie`` value into Playwright cookie dicts.

    Accepts either a bare ``li_at`` value or a full ``name=value; name2=value2``
    cookie header. The header form is detected by the literal substring
    ``li_at=`` (a raw ``li_at`` value never contains it) or a ``;`` separator
    (a multi-cookie header). Neither trips on the ``==`` base64 padding a bare
    value can end with, and a multi-cookie header that omits ``li_at`` raises a
    clear error instead of being mistaken for a value.

    Raises:
        ValueError: If the value is empty or yields no usable ``li_at`` cookie.
    """
    text = value.strip()
    if not text:
        raise ValueError("Cookie value is empty")

    if "li_at=" in text or ";" in text:
        pairs: list[tuple[str, str]] = []
        for chunk in text.split(";"):
            name, sep, raw = chunk.strip().partition("=")
            if not sep:
                continue
            name = name.strip()
            if name:
                pairs.append((name, raw.strip().strip('"')))
    else:
        pairs = [("li_at", text)]

    cookies: list[dict[str, object]] = [
        {
            "name": name,
            "value": raw,
            "domain": _LINKEDIN_DOMAIN,
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }
        for name, raw in pairs
    ]

    if not any(c["name"] == "li_at" and c["value"] for c in cookies):
        raise ValueError(
            "No li_at cookie found in the provided value. Pass the LinkedIn "
            "'li_at' cookie value, or a 'li_at=...; JSESSIONID=...' cookie string."
        )
    return cookies


async def seed_session_from_cookie(cookie_value: str, user_data_dir: Path) -> bool:
    """Stage, validate and persist a LinkedIn session from a raw cookie value.

    Writes the parsed cookies to the portable ``cookies.json``, validates them
    against ``/feed/`` headless via the shared validator, and on success writes
    the source-session metadata so the normal same-runtime / Docker-bridge flow
    reuses them. The cookie value is never logged. On failure the partial
    artifacts are removed so a stale ``cookies.json`` cannot mask the next
    attempt.

    Returns:
        True on a validated, persisted session; False when the cookie parsed but
        LinkedIn rejected it (expired / revoked).

    Raises:
        ValueError: If the cookie value cannot be parsed into an ``li_at``.
    """
    # Lazy import: drivers.browser pulls in patchright. Keep this module cheap to
    # import from config-time code paths. Mirrors orchestrate.py's lazy import.
    from linkedin_mcp_server.drivers.browser import validate_imported_cookies

    cookies = parse_linkedin_cookie(cookie_value)
    cookie_path = portable_cookie_path(user_data_dir)

    secure_write_text(
        cookie_path, json.dumps(cookies, indent=2), mode=_PRIVATE_FILE_MODE
    )
    harden_linkedin_tree(cookie_path.parent)
    logger.info("Validating %d cookie(s) from --cookie against /feed/", len(cookies))

    if await validate_imported_cookies(cookie_path, user_data_dir):
        write_source_state(user_data_dir)
        logger.info("Seeded LinkedIn session from --cookie into %s", user_data_dir)
        return True

    # Syntactically valid but LinkedIn rejected it (expired / revoked). Drop the
    # partial artifacts so they don't shadow a later, fresher cookie.
    cookie_path.unlink(missing_ok=True)
    shutil.rmtree(user_data_dir, ignore_errors=True)
    return False
