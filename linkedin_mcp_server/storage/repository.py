"""
Data access repository for outreach tracking.

Provides high-level data access methods for outreach actions, statistics,
and search cache operations.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from .database import get_db
from .models import ActionStatus, ActionType, DailyStats, OutreachAction, SearchResult

logger = logging.getLogger(__name__)


class ActionRepository:
    """Repository for managing outreach actions and statistics."""

    async def create_action(self, action: OutreachAction) -> OutreachAction:
        """
        Create a new outreach action record.

        Args:
            action: The outreach action to create

        Returns:
            The created action with ID populated
        """
        db = await get_db()
        cursor = await db.execute(
            """
            INSERT INTO outreach_actions
            (action_type, target_url, target_name, message, status, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action.action_type.value,
                action.target_url,
                action.target_name,
                action.message,
                action.status.value,
                action.error_message,
                action.created_at.isoformat(),
            ),
        )
        await db.commit()
        action.id = cursor.lastrowid
        return action

    async def update_action_status(
        self,
        action_id: int,
        status: ActionStatus,
        error_message: str | None = None,
    ) -> None:
        """
        Update the status of an existing action.

        Args:
            action_id: ID of the action to update
            status: New status value
            error_message: Optional error message if status is failed
        """
        db = await get_db()
        await db.execute(
            """
            UPDATE outreach_actions
            SET status = ?, error_message = ?, updated_at = ?
            WHERE id = ?
            """,
            (status.value, error_message, datetime.now().isoformat(), action_id),
        )
        await db.commit()

    async def get_action_by_target_url(
        self, target_url: str, action_type: ActionType | None = None
    ) -> OutreachAction | None:
        """
        Check if an action already exists for a target URL.

        Args:
            target_url: The LinkedIn profile/company URL
            action_type: Optional filter by action type

        Returns:
            The existing action or None
        """
        db = await get_db()
        if action_type:
            cursor = await db.execute(
                """
                SELECT * FROM outreach_actions
                WHERE target_url = ? AND action_type = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (target_url, action_type.value),
            )
        else:
            cursor = await db.execute(
                """
                SELECT * FROM outreach_actions
                WHERE target_url = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (target_url,),
            )
        row = await cursor.fetchone()
        if row:
            return self._row_to_action(row)
        return None

    async def get_actions(
        self,
        action_type: ActionType | None = None,
        status: ActionStatus | None = None,
        since: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutreachAction]:
        """
        Get outreach actions with optional filters.

        Args:
            action_type: Filter by action type
            status: Filter by status
            since: Filter to actions created after this time
            limit: Maximum number of results
            offset: Number of results to skip

        Returns:
            List of matching outreach actions
        """
        db = await get_db()
        conditions = []
        params: list[Any] = []

        if action_type:
            conditions.append("action_type = ?")
            params.append(action_type.value)
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if since:
            conditions.append("created_at >= ?")
            params.append(since.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit, offset])

        cursor = await db.execute(
            f"""
            SELECT * FROM outreach_actions
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_action(row) for row in rows]

    async def get_today_stats(self) -> DailyStats:
        """
        Get today's statistics for rate limiting.

        Returns:
            DailyStats for today (creates if not exists)
        """
        today = datetime.now().strftime("%Y-%m-%d")
        return await self.get_stats_for_date(today)

    async def get_stats_for_date(self, date: str) -> DailyStats:
        """
        Get statistics for a specific date.

        Args:
            date: Date in YYYY-MM-DD format

        Returns:
            DailyStats for the date (creates if not exists)
        """
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM daily_stats WHERE date = ?",
            (date,),
        )
        row = await cursor.fetchone()
        if row:
            return DailyStats(
                date=row["date"],
                connection_requests=row["connection_requests"],
                follows=row["follows"],
                messages=row["messages"],
                successful_connections=row["successful_connections"],
                successful_follows=row["successful_follows"],
                failed_actions=row["failed_actions"],
            )
        # Create new stats entry for the date
        await db.execute(
            "INSERT INTO daily_stats (date) VALUES (?)",
            (date,),
        )
        await db.commit()
        return DailyStats(date=date)

    async def increment_daily_stat(
        self,
        stat_name: str,
        date: str | None = None,
    ) -> None:
        """
        Increment a daily statistic counter.

        Args:
            stat_name: Name of the stat column to increment
            date: Date in YYYY-MM-DD format (defaults to today)
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        db = await get_db()
        # Ensure the row exists
        await self.get_stats_for_date(date)
        # Increment the counter
        await db.execute(
            f"UPDATE daily_stats SET {stat_name} = {stat_name} + 1 WHERE date = ?",
            (date,),
        )
        await db.commit()

    async def get_stats_range(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> list[DailyStats]:
        """
        Get statistics for a date range.

        Args:
            start_date: Start of the range
            end_date: End of the range

        Returns:
            List of DailyStats for each day in the range
        """
        db = await get_db()
        cursor = await db.execute(
            """
            SELECT * FROM daily_stats
            WHERE date >= ? AND date <= ?
            ORDER BY date DESC
            """,
            (
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            ),
        )
        rows = await cursor.fetchall()
        return [
            DailyStats(
                date=row["date"],
                connection_requests=row["connection_requests"],
                follows=row["follows"],
                messages=row["messages"],
                successful_connections=row["successful_connections"],
                successful_follows=row["successful_follows"],
                failed_actions=row["failed_actions"],
            )
            for row in rows
        ]

    async def get_weekly_stats(self) -> dict[str, Any]:
        """Get aggregated statistics for the last 7 days."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)
        stats_list = await self.get_stats_range(start_date, end_date)

        totals = {
            "period": "week",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "connection_requests": sum(s.connection_requests for s in stats_list),
            "follows": sum(s.follows for s in stats_list),
            "messages": sum(s.messages for s in stats_list),
            "successful_connections": sum(s.successful_connections for s in stats_list),
            "successful_follows": sum(s.successful_follows for s in stats_list),
            "failed_actions": sum(s.failed_actions for s in stats_list),
            "daily_breakdown": [s.to_dict() for s in stats_list],
        }
        return totals

    async def get_monthly_stats(self) -> dict[str, Any]:
        """Get aggregated statistics for the last 30 days."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        stats_list = await self.get_stats_range(start_date, end_date)

        totals = {
            "period": "month",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "connection_requests": sum(s.connection_requests for s in stats_list),
            "follows": sum(s.follows for s in stats_list),
            "messages": sum(s.messages for s in stats_list),
            "successful_connections": sum(s.successful_connections for s in stats_list),
            "successful_follows": sum(s.successful_follows for s in stats_list),
            "failed_actions": sum(s.failed_actions for s in stats_list),
            "daily_breakdown": [s.to_dict() for s in stats_list],
        }
        return totals

    def _row_to_action(self, row: Any) -> OutreachAction:
        """Convert a database row to an OutreachAction."""
        return OutreachAction(
            id=row["id"],
            action_type=ActionType(row["action_type"]),
            target_url=row["target_url"],
            target_name=row["target_name"],
            message=row["message"],
            status=ActionStatus(row["status"]),
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else datetime.now(),
            updated_at=datetime.fromisoformat(row["updated_at"])
            if row["updated_at"]
            else None,
        )


class SearchCacheRepository:
    """Repository for managing search result cache."""

    async def cache_result(self, result: SearchResult) -> SearchResult:
        """
        Cache a search result.

        Args:
            result: The search result to cache

        Returns:
            The cached result with ID populated
        """
        db = await get_db()
        try:
            cursor = await db.execute(
                """
                INSERT INTO search_cache
                (url, name, title, location, search_query, result_type, extra_data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.url,
                    result.name,
                    result.title,
                    result.location,
                    result.search_query,
                    result.result_type,
                    result.extra_data,
                    result.created_at.isoformat(),
                ),
            )
            await db.commit()
            result.id = cursor.lastrowid
        except Exception:
            # URL already exists, update instead
            await db.execute(
                """
                UPDATE search_cache
                SET name = ?, title = ?, location = ?, search_query = ?, extra_data = ?
                WHERE url = ?
                """,
                (
                    result.name,
                    result.title,
                    result.location,
                    result.search_query,
                    result.extra_data,
                    result.url,
                ),
            )
            await db.commit()
        return result

    async def get_by_url(self, url: str) -> SearchResult | None:
        """
        Get a cached search result by URL.

        Args:
            url: The LinkedIn URL to look up

        Returns:
            Cached result or None
        """
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM search_cache WHERE url = ?",
            (url,),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_result(row)
        return None

    async def get_by_query(
        self,
        query: str,
        result_type: str | None = None,
        limit: int = 100,
    ) -> list[SearchResult]:
        """
        Get cached results for a search query.

        Args:
            query: The search query
            result_type: Filter by "person" or "company"
            limit: Maximum results to return

        Returns:
            List of cached search results
        """
        db = await get_db()
        if result_type:
            cursor = await db.execute(
                """
                SELECT * FROM search_cache
                WHERE search_query = ? AND result_type = ?
                LIMIT ?
                """,
                (query, result_type, limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT * FROM search_cache
                WHERE search_query = ?
                LIMIT ?
                """,
                (query, limit),
            )
        rows = await cursor.fetchall()
        return [self._row_to_result(row) for row in rows]

    def _row_to_result(self, row: Any) -> SearchResult:
        """Convert a database row to a SearchResult."""
        return SearchResult(
            id=row["id"],
            url=row["url"],
            name=row["name"],
            title=row["title"],
            location=row["location"],
            search_query=row["search_query"],
            result_type=row["result_type"],
            extra_data=row["extra_data"],
            created_at=datetime.fromisoformat(row["created_at"])
            if row["created_at"]
            else datetime.now(),
        )


class OutreachStateRepository:
    """Repository for managing outreach pause/resume state."""

    PAUSE_KEY = "outreach_paused"

    async def is_paused(self) -> bool:
        """Check if outreach is currently paused."""
        db = await get_db()
        cursor = await db.execute(
            "SELECT value FROM outreach_state WHERE key = ?",
            (self.PAUSE_KEY,),
        )
        row = await cursor.fetchone()
        if row:
            return row["value"] == "true"
        return False

    async def set_paused(self, paused: bool) -> None:
        """Set the outreach paused state."""
        db = await get_db()
        value = "true" if paused else "false"
        await db.execute(
            """
            INSERT INTO outreach_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = ?
            """,
            (
                self.PAUSE_KEY,
                value,
                datetime.now().isoformat(),
                value,
                datetime.now().isoformat(),
            ),
        )
        await db.commit()

    async def get_pause_info(self) -> dict[str, Any]:
        """Get detailed pause state information."""
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM outreach_state WHERE key = ?",
            (self.PAUSE_KEY,),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "paused": row["value"] == "true",
                "updated_at": row["updated_at"],
            }
        return {"paused": False, "updated_at": None}
