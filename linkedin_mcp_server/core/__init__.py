"""Core browser management, authentication, and scraping utilities."""

from .auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)
from .browser import BrowserManager
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

__all__ = [
    "AuthenticationError",
    "BrowserManager",
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
