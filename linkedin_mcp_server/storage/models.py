"""
Data models for outreach tracking and storage.

Defines dataclasses for outreach actions, daily statistics, and search cache
used by the SQLite database for persistence.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    """Types of outreach actions that can be tracked."""

    CONNECTION_REQUEST = "connection_request"
    FOLLOW_COMPANY = "follow_company"
    MESSAGE_SENT = "message_sent"


class ActionStatus(str, Enum):
    """Status of an outreach action."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    SKIPPED = "skipped"


@dataclass
class OutreachAction:
    """Represents a single outreach action (connection request, follow, etc.)."""

    action_type: ActionType
    target_url: str
    status: ActionStatus
    created_at: datetime = field(default_factory=datetime.now)
    id: int | None = None
    target_name: str | None = None
    message: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "action_type": self.action_type.value,
            "target_url": self.target_url,
            "target_name": self.target_name,
            "message": self.message,
            "status": self.status.value,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class DailyStats:
    """Aggregated daily statistics for rate limiting."""

    date: str  # YYYY-MM-DD format
    connection_requests: int = 0
    follows: int = 0
    messages: int = 0
    successful_connections: int = 0
    successful_follows: int = 0
    failed_actions: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "date": self.date,
            "connection_requests": self.connection_requests,
            "follows": self.follows,
            "messages": self.messages,
            "successful_connections": self.successful_connections,
            "successful_follows": self.successful_follows,
            "failed_actions": self.failed_actions,
        }


@dataclass
class SearchResult:
    """Cached search result to avoid duplicate outreach."""

    url: str
    name: str
    search_query: str
    result_type: str  # "person" or "company"
    created_at: datetime = field(default_factory=datetime.now)
    id: int | None = None
    title: str | None = None
    location: str | None = None
    extra_data: str | None = None  # JSON string for additional data

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "url": self.url,
            "name": self.name,
            "title": self.title,
            "location": self.location,
            "search_query": self.search_query,
            "result_type": self.result_type,
            "extra_data": self.extra_data,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
