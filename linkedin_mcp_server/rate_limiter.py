"""Proactive rate limiting for LinkedIn write actions.

Tracks daily action counts, enforces configurable limits, and adds
randomized delays between actions to avoid bot-like patterns.

State is persisted to a JSON file so limits carry across server restarts.
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Default Limits (override via env vars) ──────────────────────────

_DEFAULTS = {
    # Daily caps
    "LINKEDIN_DAILY_CONNECTION_LIMIT": 40,       # safe for active recruiters (~100/week max)
    "LINKEDIN_DAILY_MESSAGE_LIMIT": 80,          # messages to connections
    "LINKEDIN_DAILY_SEARCH_MESSAGE_LIMIT": 20,   # messages to non-connections (InMail-like)
    # Delays (seconds) — random value between min and max
    "LINKEDIN_MIN_ACTION_DELAY": 3,
    "LINKEDIN_MAX_ACTION_DELAY": 8,
    "LINKEDIN_MIN_WRITE_DELAY": 5,               # longer delay after write actions
    "LINKEDIN_MAX_WRITE_DELAY": 15,
    # Backoff
    "LINKEDIN_COOLDOWN_MINUTES": 30,             # pause after hitting a limit
    "LINKEDIN_RATE_LIMIT_BACKOFF_MINUTES": 240,  # pause if LinkedIn rate-limits us
}


def _get_config(key: str) -> int:
    """Read config from env var or fall back to default."""
    return int(os.environ.get(key, _DEFAULTS[key]))


# ── State File ──────────────────────────────────────────────────────

_STATE_DIR = Path.home() / ".linkedin-mcp"
_STATE_FILE = _STATE_DIR / "rate_limiter_state.json"


@dataclass
class DailyState:
    """Tracks action counts for the current day."""

    date: str = ""
    connections_sent: int = 0
    messages_sent: int = 0
    search_messages_sent: int = 0
    last_action_at: float = 0.0
    last_write_at: float = 0.0
    cooldown_until: float = 0.0
    actions_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "connections_sent": self.connections_sent,
            "messages_sent": self.messages_sent,
            "search_messages_sent": self.search_messages_sent,
            "last_action_at": self.last_action_at,
            "last_write_at": self.last_write_at,
            "cooldown_until": self.cooldown_until,
            "actions_log": self.actions_log[-100:],  # keep last 100
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyState":
        return cls(
            date=data.get("date", ""),
            connections_sent=data.get("connections_sent", 0),
            messages_sent=data.get("messages_sent", 0),
            search_messages_sent=data.get("search_messages_sent", 0),
            last_action_at=data.get("last_action_at", 0.0),
            last_write_at=data.get("last_write_at", 0.0),
            cooldown_until=data.get("cooldown_until", 0.0),
            actions_log=data.get("actions_log", []),
        )


class RateLimiter:
    """Proactive rate limiter for LinkedIn write actions."""

    def __init__(self) -> None:
        self._state = self._load_state()
        self._lock = asyncio.Lock()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_state(self) -> DailyState:
        try:
            if _STATE_FILE.exists():
                data = json.loads(_STATE_FILE.read_text())
                state = DailyState.from_dict(data)
                # Reset if it's a new day
                if state.date != self._today():
                    logger.info(
                        "New day — resetting rate limiter counters "
                        "(yesterday: %d connections, %d messages)",
                        state.connections_sent,
                        state.messages_sent,
                    )
                    return DailyState(date=self._today())
                return state
        except Exception as e:
            logger.warning("Failed to load rate limiter state: %s", e)
        return DailyState(date=self._today())

    def _save_state(self) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(
                json.dumps(self._state.to_dict(), indent=2)
            )
        except Exception as e:
            logger.warning("Failed to save rate limiter state: %s", e)

    def _check_new_day(self) -> None:
        if self._state.date != self._today():
            self._state = DailyState(date=self._today())

    async def check_and_wait(
        self,
        action_type: str,
    ) -> None:
        """Check limits and wait appropriate delay before an action.

        Args:
            action_type: One of 'connection', 'message', 'search_message', 'read'

        Raises:
            RateLimitExceeded: If daily limit would be exceeded.
        """
        async with self._lock:
            self._check_new_day()
            now = time.time()

            # Check cooldown
            if now < self._state.cooldown_until:
                remaining = int(self._state.cooldown_until - now)
                raise RateLimitExceeded(
                    f"Rate limiter cooldown active. "
                    f"Try again in {remaining // 60} min {remaining % 60}s."
                )

            # Check daily limits for write actions
            if action_type == "connection":
                limit = _get_config("LINKEDIN_DAILY_CONNECTION_LIMIT")
                if self._state.connections_sent >= limit:
                    self._activate_cooldown("daily_connection_limit")
                    raise RateLimitExceeded(
                        f"Daily connection request limit reached "
                        f"({self._state.connections_sent}/{limit}). "
                        f"Resets tomorrow."
                    )

            elif action_type == "message":
                limit = _get_config("LINKEDIN_DAILY_MESSAGE_LIMIT")
                if self._state.messages_sent >= limit:
                    self._activate_cooldown("daily_message_limit")
                    raise RateLimitExceeded(
                        f"Daily message limit reached "
                        f"({self._state.messages_sent}/{limit}). "
                        f"Resets tomorrow."
                    )

            elif action_type == "search_message":
                limit = _get_config("LINKEDIN_DAILY_SEARCH_MESSAGE_LIMIT")
                if self._state.search_messages_sent >= limit:
                    self._activate_cooldown("daily_search_message_limit")
                    raise RateLimitExceeded(
                        f"Daily search message limit reached "
                        f"({self._state.search_messages_sent}/{limit}). "
                        f"Resets tomorrow."
                    )

            # Add randomized delay
            if action_type in ("connection", "message", "search_message"):
                # Write action: longer delay
                min_delay = _get_config("LINKEDIN_MIN_WRITE_DELAY")
                max_delay = _get_config("LINKEDIN_MAX_WRITE_DELAY")
                since_last_write = now - self._state.last_write_at
            else:
                # Read action: shorter delay
                min_delay = _get_config("LINKEDIN_MIN_ACTION_DELAY")
                max_delay = _get_config("LINKEDIN_MAX_ACTION_DELAY")
                since_last_write = now - self._state.last_action_at

            delay = random.uniform(min_delay, max_delay)
            remaining_delay = delay - since_last_write

            if remaining_delay > 0:
                logger.debug(
                    "Rate limiter: waiting %.1fs before %s action",
                    remaining_delay,
                    action_type,
                )
                await asyncio.sleep(remaining_delay)

    async def record_action(
        self,
        action_type: str,
        target: str = "",
        success: bool = True,
    ) -> None:
        """Record a completed action for tracking.

        Args:
            action_type: One of 'connection', 'message', 'search_message', 'read'
            target: Username or description of the target
            success: Whether the action succeeded
        """
        async with self._lock:
            self._check_new_day()
            now = time.time()

            if action_type == "connection" and success:
                self._state.connections_sent += 1
            elif action_type == "message" and success:
                self._state.messages_sent += 1
            elif action_type == "search_message" and success:
                self._state.search_messages_sent += 1

            self._state.last_action_at = now
            if action_type in ("connection", "message", "search_message"):
                self._state.last_write_at = now

            self._state.actions_log.append({
                "type": action_type,
                "target": target,
                "success": success,
                "at": datetime.now(timezone.utc).isoformat(),
            })

            self._save_state()

            logger.info(
                "Rate limiter: %s %s (today: %d connections, %d messages)",
                action_type,
                "✓" if success else "✗",
                self._state.connections_sent,
                self._state.messages_sent,
            )

    async def on_linkedin_rate_limit(self) -> None:
        """Called when LinkedIn itself rate-limits us. Triggers extended backoff."""
        async with self._lock:
            backoff_min = _get_config("LINKEDIN_RATE_LIMIT_BACKOFF_MINUTES")
            self._state.cooldown_until = time.time() + (backoff_min * 60)
            self._save_state()
            logger.warning(
                "LinkedIn rate limit detected! "
                "Backing off for %d minutes.",
                backoff_min,
            )

    def _activate_cooldown(self, reason: str) -> None:
        cooldown_min = _get_config("LINKEDIN_COOLDOWN_MINUTES")
        self._state.cooldown_until = time.time() + (cooldown_min * 60)
        self._save_state()
        logger.warning(
            "Rate limiter cooldown activated (%s). "
            "Pausing for %d minutes.",
            reason,
            cooldown_min,
        )

    def get_status(self) -> dict[str, Any]:
        """Return current rate limiter status for diagnostics."""
        self._check_new_day()
        now = time.time()
        return {
            "date": self._state.date,
            "connections": {
                "sent": self._state.connections_sent,
                "limit": _get_config("LINKEDIN_DAILY_CONNECTION_LIMIT"),
                "remaining": max(
                    0,
                    _get_config("LINKEDIN_DAILY_CONNECTION_LIMIT")
                    - self._state.connections_sent,
                ),
            },
            "messages": {
                "sent": self._state.messages_sent,
                "limit": _get_config("LINKEDIN_DAILY_MESSAGE_LIMIT"),
                "remaining": max(
                    0,
                    _get_config("LINKEDIN_DAILY_MESSAGE_LIMIT")
                    - self._state.messages_sent,
                ),
            },
            "cooldown_active": now < self._state.cooldown_until,
            "cooldown_remaining_seconds": max(
                0, int(self._state.cooldown_until - now)
            ),
        }


class RateLimitExceeded(Exception):
    """Raised when a daily limit has been reached."""

    pass


# ── Singleton ────────────────────────────────────────────────────────

_instance: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _instance
    if _instance is None:
        _instance = RateLimiter()
    return _instance
