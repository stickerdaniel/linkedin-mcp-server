"""Import a LinkedIn session from a locally logged-in Chromium-family browser.

The package exposes the discovery and extraction primitives eagerly. The
orchestrator entry point is imported lazily inside ``import_session_from_browser``
so that importing this package never pulls in ``drivers.browser`` (which imports
``config``). That avoids a config -> browser_import -> drivers.browser -> config
import cycle when ``config/schema.py`` references ``SUPPORTED_BROWSERS``.
"""

from __future__ import annotations

from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from .discovery import SUPPORTED_BROWSERS, BrowserProfile, discover_profiles
from .extract import LinkedInCookie, extract_linkedin_cookies

__all__ = [
    "SUPPORTED_BROWSERS",
    "BrowserProfile",
    "LinkedInCookie",
    "discover_profiles",
    "extract_linkedin_cookies",
    "import_session_from_browser",
]


def import_session_from_browser(
    browser: str | None,
    *,
    user_data_dir: Path,
) -> Coroutine[Any, Any, bool]:
    """Lazy entry point; avoids importing drivers.browser at package import time."""
    from .orchestrate import import_session_from_browser as _impl

    return _impl(browser, user_data_dir=user_data_dir)
