"""
Storage module for outreach tracking with SQLite persistence.

Provides database management, data models, and repository classes for
tracking outreach actions, daily statistics, and search result caching.
"""

from .database import close_db, get_db, reset_db_for_testing
from .models import (
    ActionStatus,
    ActionType,
    DailyStats,
    OutreachAction,
    SearchResult,
)
from .repository import (
    ActionRepository,
    OutreachStateRepository,
    SearchCacheRepository,
)

__all__ = [
    # Database
    "get_db",
    "close_db",
    "reset_db_for_testing",
    # Models
    "ActionType",
    "ActionStatus",
    "OutreachAction",
    "DailyStats",
    "SearchResult",
    # Repositories
    "ActionRepository",
    "SearchCacheRepository",
    "OutreachStateRepository",
]
