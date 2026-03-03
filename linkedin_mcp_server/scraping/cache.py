"""Simple TTL cache for scraped page content."""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ScrapingCache:
    """In-memory TTL cache keyed by URL or arbitrary string key."""

    def __init__(self, default_ttl: float = 300.0):
        self._store: dict[str, tuple[Any, float]] = {}
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        """Return cached value if present and not expired, else None."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        logger.debug("Cache hit: %s", key[:80])
        return value

    def put(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Store a value with TTL (defaults to instance default_ttl)."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        self._store[key] = (value, time.monotonic() + effective_ttl)

    def invalidate(self, key: str) -> None:
        """Remove a specific key from the cache."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Remove all cached entries."""
        self._store.clear()
        logger.debug("Cache cleared")


# Module-level singleton
scraping_cache = ScrapingCache()
