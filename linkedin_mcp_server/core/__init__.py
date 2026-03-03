"""Core browser management, authentication, and scraping utilities."""

from .auth import is_logged_in, wait_for_manual_login, warm_up_browser
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
from .utils import (
    detect_rate_limit,
    handle_modal_close,
    humanized_delay,
    rate_limit_state,
    scroll_to_bottom,
    wait_for_cooldown,
)

__all__ = [
    "AuthenticationError",
    "BrowserManager",
    "ElementNotFoundError",
    "LinkedInScraperException",
    "NetworkError",
    "ProfileNotFoundError",
    "RateLimitError",
    "ScrapingError",
    "detect_rate_limit",
    "handle_modal_close",
    "humanized_delay",
    "is_logged_in",
    "rate_limit_state",
    "scroll_to_bottom",
    "wait_for_cooldown",
    "wait_for_manual_login",
    "warm_up_browser",
]
