"""Tests for the ScrapingCache TTL cache."""

import time
from unittest.mock import patch

from linkedin_mcp_server.scraping.cache import ScrapingCache


class TestScrapingCache:
    def test_get_returns_none_for_missing_key(self):
        cache = ScrapingCache()
        assert cache.get("missing") is None

    def test_put_and_get(self):
        cache = ScrapingCache()
        cache.put("url", "content")
        assert cache.get("url") == "content"

    def test_ttl_expiry(self):
        cache = ScrapingCache(default_ttl=0.1)
        cache.put("url", "content")
        assert cache.get("url") == "content"
        with patch("linkedin_mcp_server.scraping.cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 1.0
            assert cache.get("url") is None

    def test_custom_ttl_per_entry(self):
        cache = ScrapingCache(default_ttl=300.0)
        cache.put("short", "data", ttl=0.1)
        assert cache.get("short") == "data"
        with patch("linkedin_mcp_server.scraping.cache.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 1.0
            assert cache.get("short") is None

    def test_invalidate(self):
        cache = ScrapingCache()
        cache.put("url", "content")
        cache.invalidate("url")
        assert cache.get("url") is None

    def test_invalidate_missing_key_no_error(self):
        cache = ScrapingCache()
        cache.invalidate("missing")  # Should not raise

    def test_clear(self):
        cache = ScrapingCache()
        cache.put("a", "1")
        cache.put("b", "2")
        cache.clear()
        assert cache.get("a") is None
        assert cache.get("b") is None

    def test_stores_any_type(self):
        cache = ScrapingCache()
        cache.put("list", [{"a": 1}, {"b": 2}])
        result = cache.get("list")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_overwrite_existing_key(self):
        cache = ScrapingCache()
        cache.put("url", "old")
        cache.put("url", "new")
        assert cache.get("url") == "new"
