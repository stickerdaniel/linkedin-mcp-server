"""SQLite-backed persistent cache for MCP tool outputs and comment dedup."""

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path("~/.linkedin-mcp/cache.db").expanduser()

_DDL = """
CREATE TABLE IF NOT EXISTS tool_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name   TEXT NOT NULL,
    args_hash   TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    UNIQUE(tool_name, args_hash)
);
CREATE INDEX IF NOT EXISTS idx_cache_lookup
    ON tool_cache(tool_name, args_hash, expires_at);

CREATE TABLE IF NOT EXISTS seen_comments (
    permalink   TEXT PRIMARY KEY,
    post_url    TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class SQLiteCache:
    """Persistent SQLite cache for tool outputs and comment dedup.

    Two responsibilities:
    - Tool result caching: get_tool / set_tool (TTL-based, per tool+args)
    - Comment dedup: is_seen_comment / mark_seen_comments (persistent across runs)

    Separate from ScrapingCache (in-memory, URL-keyed). Both coexist.
    """

    def __init__(self, db_path: Path | str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_DDL)

    def _args_hash(self, tool_name: str, args: dict[str, Any]) -> str:
        key = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        return hashlib.sha256(key.encode()).hexdigest()

    def get_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Return cached result if present and not expired, else None."""
        h = self._args_hash(tool_name, args)
        row = self._conn.execute(
            "SELECT result_json FROM tool_cache"
            " WHERE tool_name=? AND args_hash=? AND expires_at > datetime('now')",
            (tool_name, h),
        ).fetchone()
        if row is None:
            # Lazy delete any expired entry for this key
            try:
                self._conn.execute(
                    "DELETE FROM tool_cache WHERE tool_name=? AND args_hash=?"
                    " AND expires_at <= datetime('now')",
                    (tool_name, h),
                )
            except Exception:
                pass
            return None
        logger.debug("Cache hit: %s %s", tool_name, str(args)[:60])
        return json.loads(row[0])

    def set_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
        ttl: int,
    ) -> None:
        """Store a tool result with TTL in seconds."""
        h = self._args_hash(tool_name, args)
        self._conn.execute(
            "INSERT OR REPLACE INTO tool_cache"
            " (tool_name, args_hash, args_json, result_json, expires_at)"
            " VALUES (?, ?, ?, ?, datetime('now', ?))",
            (
                tool_name,
                h,
                json.dumps(args, sort_keys=True),
                json.dumps(result),
                f"+{ttl} seconds",
            ),
        )

    def is_seen_comment(self, permalink: str) -> bool:
        """Return True if this permalink was seen in a previous run."""
        if not permalink:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM seen_comments WHERE permalink=?", (permalink,)
        ).fetchone()
        return row is not None

    def mark_seen_comments(self, items: list[dict[str, Any]]) -> None:
        """Record comment permalinks as seen. Items without permalink are skipped."""
        for item in items:
            permalink = item.get("comment_permalink") or ""
            if not permalink:
                continue
            post_url = item.get("post_url") or ""
            self._conn.execute(
                "INSERT INTO seen_comments (permalink, post_url, last_seen)"
                " VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(permalink) DO UPDATE SET last_seen=excluded.last_seen",
                (permalink, post_url),
            )

    def cleanup(self) -> int:
        """Delete all expired tool_cache entries. Returns count deleted."""
        deleted = self._conn.execute(
            "DELETE FROM tool_cache WHERE expires_at <= datetime('now')"
        ).rowcount
        if deleted:
            logger.info("SQLiteCache cleanup: removed %d expired entries", deleted)
        return deleted


# Module-level singleton — path resolved at import time
sqlite_cache = SQLiteCache()
