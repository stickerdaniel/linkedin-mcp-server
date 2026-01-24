"""
Rate limiter for outreach safety.

Provides rate limiting enforcement and tracking to prevent LinkedIn account
restrictions from excessive automation.
"""

import asyncio
import logging
import random
from datetime import datetime
from typing import Any

from linkedin_mcp_server.storage import (
    ActionRepository,
    ActionType,
    OutreachStateRepository,
)

from .limits import DEFAULT_LIMITS, RateLimits

logger = logging.getLogger(__name__)


class RateLimitExceededError(Exception):
    """Raised when a rate limit has been exceeded."""

    def __init__(
        self,
        action_type: ActionType,
        current: int,
        limit: int,
        message: str | None = None,
    ):
        self.action_type = action_type
        self.current = current
        self.limit = limit
        default_msg = (
            f"Daily {action_type.value} limit exceeded: {current}/{limit}. "
            f"Try again tomorrow."
        )
        super().__init__(message or default_msg)


class OutreachPausedError(Exception):
    """Raised when outreach has been paused."""

    def __init__(self, message: str | None = None):
        default_msg = "Outreach is currently paused. Use resume_outreach to continue."
        super().__init__(message or default_msg)


class RateLimiter:
    """
    Rate limiter for outreach actions.

    Tracks and enforces daily limits for different action types,
    manages delays between actions, and handles pause/resume state.
    """

    def __init__(self, limits: RateLimits | None = None):
        """
        Initialize the rate limiter.

        Args:
            limits: Custom rate limits. Defaults to conservative defaults.
        """
        self.limits = limits or DEFAULT_LIMITS
        self._action_repo = ActionRepository()
        self._state_repo = OutreachStateRepository()
        self._current_backoff = self.limits.initial_backoff
        self._batch_count = 0

    async def check_limit(self, action_type: ActionType) -> None:
        """
        Check if an action is allowed under current rate limits.

        Args:
            action_type: Type of action to check

        Raises:
            OutreachPausedError: If outreach is paused
            RateLimitExceededError: If daily limit exceeded
        """
        # Check pause state first
        if await self._state_repo.is_paused():
            raise OutreachPausedError()

        # Get today's stats
        stats = await self._action_repo.get_today_stats()

        # Check appropriate limit
        if action_type == ActionType.CONNECTION_REQUEST:
            current = stats.connection_requests
            limit = self.limits.daily_connection_limit
        elif action_type == ActionType.FOLLOW_COMPANY:
            current = stats.follows
            limit = self.limits.daily_follow_limit
        elif action_type == ActionType.MESSAGE_SENT:
            current = stats.messages
            limit = self.limits.daily_message_limit
        else:
            return  # Unknown type, allow it

        if current >= limit:
            raise RateLimitExceededError(action_type, current, limit)

    async def record_action(self, action_type: ActionType, success: bool) -> None:
        """
        Record an action for rate limiting and increment batch counter.

        Args:
            action_type: Type of action performed
            success: Whether the action succeeded
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # Increment appropriate counter
        if action_type == ActionType.CONNECTION_REQUEST:
            await self._action_repo.increment_daily_stat("connection_requests", today)
            if success:
                await self._action_repo.increment_daily_stat(
                    "successful_connections", today
                )
        elif action_type == ActionType.FOLLOW_COMPANY:
            await self._action_repo.increment_daily_stat("follows", today)
            if success:
                await self._action_repo.increment_daily_stat(
                    "successful_follows", today
                )
        elif action_type == ActionType.MESSAGE_SENT:
            await self._action_repo.increment_daily_stat("messages", today)

        if not success:
            await self._action_repo.increment_daily_stat("failed_actions", today)

        self._batch_count += 1

    async def wait_between_actions(self) -> float:
        """
        Wait a random delay between actions.

        Uses human-like random delays to avoid detection.

        Returns:
            The actual delay in seconds
        """
        delay = random.uniform(
            self.limits.min_action_delay,
            self.limits.max_action_delay,
        )
        logger.debug(f"Waiting {delay:.1f}s between actions")
        await asyncio.sleep(delay)
        return delay

    async def wait_for_batch_pause(self) -> float | None:
        """
        Check if batch pause is needed and wait if so.

        Returns:
            The pause duration if paused, None otherwise
        """
        if self._batch_count >= self.limits.batch_size:
            pause = random.uniform(
                self.limits.min_batch_pause,
                self.limits.max_batch_pause,
            )
            logger.info(
                f"Batch of {self._batch_count} actions complete, "
                f"pausing for {pause:.0f}s"
            )
            await asyncio.sleep(pause)
            self._batch_count = 0
            return pause
        return None

    async def apply_backoff(self) -> float:
        """
        Apply exponential backoff after rate limit detection.

        Returns:
            The backoff duration in seconds
        """
        backoff = min(self._current_backoff, self.limits.max_backoff)
        logger.warning(f"Rate limit detected, backing off for {backoff}s")
        await asyncio.sleep(backoff)
        self._current_backoff = int(
            self._current_backoff * self.limits.backoff_multiplier
        )
        return backoff

    def reset_backoff(self) -> None:
        """Reset backoff to initial value after successful action."""
        self._current_backoff = self.limits.initial_backoff

    async def get_status(self) -> dict[str, Any]:
        """
        Get current rate limit status.

        Returns:
            Dictionary with current limits and remaining actions
        """
        stats = await self._action_repo.get_today_stats()
        is_paused = await self._state_repo.is_paused()

        return {
            "paused": is_paused,
            "date": stats.date,
            "connection_requests": {
                "used": stats.connection_requests,
                "limit": self.limits.daily_connection_limit,
                "remaining": max(
                    0, self.limits.daily_connection_limit - stats.connection_requests
                ),
            },
            "follows": {
                "used": stats.follows,
                "limit": self.limits.daily_follow_limit,
                "remaining": max(0, self.limits.daily_follow_limit - stats.follows),
            },
            "messages": {
                "used": stats.messages,
                "limit": self.limits.daily_message_limit,
                "remaining": max(0, self.limits.daily_message_limit - stats.messages),
            },
            "success_rate": {
                "connections": (
                    f"{stats.successful_connections}/{stats.connection_requests}"
                    if stats.connection_requests > 0
                    else "N/A"
                ),
                "follows": (
                    f"{stats.successful_follows}/{stats.follows}"
                    if stats.follows > 0
                    else "N/A"
                ),
            },
            "batch_progress": f"{self._batch_count}/{self.limits.batch_size}",
            "current_backoff": self._current_backoff,
        }

    async def pause(self) -> None:
        """Pause all outreach automation."""
        await self._state_repo.set_paused(True)
        logger.info("Outreach automation paused")

    async def resume(self) -> None:
        """Resume outreach automation."""
        await self._state_repo.set_paused(False)
        self.reset_backoff()
        logger.info("Outreach automation resumed")


# Global rate limiter instance
_rate_limiter: RateLimiter | None = None


def get_rate_limiter(limits: RateLimits | None = None) -> RateLimiter:
    """
    Get the global rate limiter instance.

    Args:
        limits: Optional custom rate limits (only used on first call)

    Returns:
        The global RateLimiter instance
    """
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(limits)
    rate_limiter = _rate_limiter
    assert rate_limiter is not None
    return rate_limiter


def reset_rate_limiter_for_testing() -> None:
    """Reset global rate limiter state for test isolation."""
    global _rate_limiter
    _rate_limiter = None
