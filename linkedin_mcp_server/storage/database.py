"""
SQLite database management for outreach tracking.

Provides async database operations using aiosqlite for persistent storage
of outreach actions, daily statistics, and search cache.
"""

import logging
from pathlib import Path
from typing import cast

import aiosqlite

logger = logging.getLogger(__name__)

# Default database location
DEFAULT_DB_PATH = Path.home() / ".linkedin-mcp" / "outreach.db"

# Global database connection (singleton)
_db: aiosqlite.Connection | None = None


# SQL schema for creating tables
CREATE_TABLES_SQL = """
-- Outreach actions table
CREATE TABLE IF NOT EXISTS outreach_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    target_url TEXT NOT NULL,
    target_name TEXT,
    message TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

-- Index for querying by date and type
CREATE INDEX IF NOT EXISTS idx_actions_created_at ON outreach_actions(created_at);
CREATE INDEX IF NOT EXISTS idx_actions_type ON outreach_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_actions_target_url ON outreach_actions(target_url);

-- Daily stats table for rate limiting
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    connection_requests INTEGER DEFAULT 0,
    follows INTEGER DEFAULT 0,
    messages INTEGER DEFAULT 0,
    successful_connections INTEGER DEFAULT 0,
    successful_follows INTEGER DEFAULT 0,
    failed_actions INTEGER DEFAULT 0
);

-- Search cache table
CREATE TABLE IF NOT EXISTS search_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    title TEXT,
    location TEXT,
    search_query TEXT,
    result_type TEXT NOT NULL,
    extra_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_search_url ON search_cache(url);
CREATE INDEX IF NOT EXISTS idx_search_query ON search_cache(search_query);

-- Outreach pause state
CREATE TABLE IF NOT EXISTS outreach_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def get_db(db_path: Path | None = None) -> aiosqlite.Connection:
    """
    Get existing database connection or create a new one.

    Uses a singleton pattern to reuse the connection across calls.

    Args:
        db_path: Path to database file. Defaults to ~/.linkedin-mcp/outreach.db

    Returns:
        Active aiosqlite connection
    """
    global _db

    if db_path is None:
        db_path = DEFAULT_DB_PATH

    if _db is not None:
        return cast(aiosqlite.Connection, _db)

    # Ensure directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Opening database at {db_path}")
    _db = await aiosqlite.connect(db_path)
    _db.row_factory = aiosqlite.Row

    # Initialize schema
    await _db.executescript(CREATE_TABLES_SQL)
    await _db.commit()

    logger.info("Database initialized successfully")
    return _db


async def close_db() -> None:
    """Close the database connection."""
    global _db

    if _db is not None:
        logger.info("Closing database connection")
        await _db.close()
        _db = None


async def reset_db_for_testing() -> None:
    """Reset global database state for test isolation."""
    global _db
    if _db is not None:
        await _db.close()
    _db = None
