"""Core browser management, authentication, and scraping utilities."""

from typing import TYPE_CHECKING

from .auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)
from .exceptions import (
    AuthenticationError,
    ElementNotFoundError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    ProxyConnectionError,
    RateLimitError,
    ScrapingError,
)
from .proxy_errors import (
    as_proxy_error,
    goto_reporting_proxy_errors,
    is_proxy_error,
    proxy_hint,
    raise_if_proxy_configured,
    raise_if_proxy_error,
    redact_proxy_credentials,
    redacted_copy,
)
from .utils import detect_rate_limit, handle_modal_close, scroll_to_bottom

if TYPE_CHECKING:
    from .browser import BrowserManager, await_deferring_cancels

_LAZY_BROWSER_EXPORTS = frozenset({"BrowserManager", "await_deferring_cancels"})


def __getattr__(name: str) -> object:
    """Resolve browser lifecycle exports without loading them for leaf imports."""
    if name not in _LAZY_BROWSER_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    browser = import_module(".browser", __name__)
    value = getattr(browser, name)
    globals()[name] = value
    return value


__all__ = [
    "AuthenticationError",
    "BrowserManager",
    "await_deferring_cancels",
    "detect_auth_barrier",
    "detect_auth_barrier_quick",
    "ElementNotFoundError",
    "LinkedInScraperException",
    "NetworkError",
    "ProfileNotFoundError",
    "ProxyConnectionError",
    "RateLimitError",
    "ScrapingError",
    "as_proxy_error",
    "goto_reporting_proxy_errors",
    "is_proxy_error",
    "proxy_hint",
    "raise_if_proxy_configured",
    "raise_if_proxy_error",
    "redact_proxy_credentials",
    "redacted_copy",
    "detect_rate_limit",
    "handle_modal_close",
    "is_logged_in",
    "resolve_remember_me_prompt",
    "scroll_to_bottom",
    "wait_for_manual_login",
]
