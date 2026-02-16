"""Core utilities inlined from linkedin_scraper."""

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
from .utils import detect_rate_limit, handle_modal_close, scroll_to_bottom

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
    "is_logged_in",
    "scroll_to_bottom",
    "wait_for_manual_login",
    "warm_up_browser",
]
