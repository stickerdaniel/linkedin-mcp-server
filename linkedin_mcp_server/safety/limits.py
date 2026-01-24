"""
Rate limit configuration for outreach safety.

Defines default limits and delay ranges to prevent LinkedIn account restrictions.
These conservative defaults help protect accounts from automation detection.
"""

from dataclasses import dataclass


@dataclass
class RateLimits:
    """Configuration for outreach rate limits."""

    # Daily action limits
    daily_connection_limit: int = 30  # Max connection requests per day
    daily_follow_limit: int = 50  # Max company follows per day
    daily_message_limit: int = 50  # Max messages per day

    # Delay configuration (in seconds)
    min_action_delay: int = 30  # Minimum delay between actions
    max_action_delay: int = 120  # Maximum delay between actions

    # Batch configuration
    batch_size: int = 10  # Actions per batch before pause
    min_batch_pause: int = 300  # 5 minutes minimum pause
    max_batch_pause: int = 900  # 15 minutes maximum pause

    # Backoff configuration
    initial_backoff: int = 60  # Initial backoff on rate limit detection
    max_backoff: int = 3600  # Maximum backoff (1 hour)
    backoff_multiplier: float = 2.0  # Exponential backoff multiplier


# Default rate limits instance
DEFAULT_LIMITS = RateLimits()
