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
from .stealth_profile import (
    DEFAULT_STEALTH_PROFILE_NAME,
    STEALTH_PROFILE_NAMES,
    DelayConfig,
    NavigationMode,
    SimulationLevel,
    StealthProfile,
    get_stealth_profile,
)
from .utils import detect_rate_limit, handle_modal_close, scroll_to_bottom

__all__ = [
    "AuthBarrier",
    "AuthBarrierKind",
    "AuthenticationError",
    "BlockError",
    "BrowserManager",
    "ChallengeError",
    "DEFAULT_STEALTH_PROFILE_NAME",
    "DelayConfig",
    "detect_auth_barrier",
    "detect_auth_barrier_quick",
    "detect_empty_profile_barrier",
    "ElementNotFoundError",
    "get_stealth_profile",
    "LinkedInScraperException",
    "NavigationMode",
    "NetworkError",
    "ProfileNotFoundError",
    "RateLimitError",
    "ScrapingError",
    "SimulationLevel",
    "STEALTH_PROFILE_NAMES",
    "StealthProfile",
    "detect_rate_limit",
    "handle_modal_close",
    "is_logged_in",
    "resolve_remember_me_prompt",
    "scroll_to_bottom",
    "wait_for_manual_login",
]
