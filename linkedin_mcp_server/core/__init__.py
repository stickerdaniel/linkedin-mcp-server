"""Core browser management, authentication, and scraping utilities."""

from .auth import (
    detect_auth_barrier,
    detect_auth_barrier_quick,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
    warm_up_browser,
)
from .browser import BrowserManager
from .exceptions import (
    AuthenticationError,
    ElementNotFoundError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
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
    "RateLimitError",
    "ScrapingError",
    "detect_rate_limit",
    "handle_modal_close",
    "is_logged_in",
    "resolve_remember_me_prompt",
    "scroll_to_bottom",
    "wait_for_manual_login",
    "warm_up_browser",
]
