"""
Safety module for outreach rate limiting and protection.

Provides rate limiting enforcement to prevent LinkedIn account restrictions
from excessive automation.
"""

from .limits import DEFAULT_LIMITS, RateLimits
from .rate_limiter import (
    OutreachPausedError,
    RateLimitExceededError,
    RateLimiter,
    get_rate_limiter,
    reset_rate_limiter_for_testing,
)

__all__ = [
    # Limits
    "RateLimits",
    "DEFAULT_LIMITS",
    # Rate limiter
    "RateLimiter",
    "get_rate_limiter",
    "reset_rate_limiter_for_testing",
    # Exceptions
    "RateLimitExceededError",
    "OutreachPausedError",
]
