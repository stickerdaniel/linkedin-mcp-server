"""Core browser management, authentication, and scraping utilities."""

from .auth import (
    AuthBarrier,
    AuthBarrierKind,
    detect_auth_barrier,
    detect_auth_barrier_quick,
    detect_empty_profile_barrier,
    is_logged_in,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)
from .browser import BrowserManager
from .exceptions import (
    AuthenticationError,
    BlockError,
    ChallengeError,
    ElementNotFoundError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
)
from .utils import detect_rate_limit, handle_modal_close, scroll_to_bottom

__all__ = [
    "AuthBarrier",
    "AuthBarrierKind",
    "AuthenticationError",
    "BlockError",
    "BrowserManager",
    "ChallengeError",
    "detect_auth_barrier",
    "detect_auth_barrier_quick",
    "detect_empty_profile_barrier",
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
]
