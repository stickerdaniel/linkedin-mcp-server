"""Unit tests for SQLiteCache (sqlite-backed persistent cache)."""

import sqlite3
import time

import pytest

from linkedin_mcp_server.scraping.sqlite_cache import SQLiteCache


@pytest.fixture
def db(tmp_path):
    """Fresh SQLiteCache backed by a temp file."""
    return SQLiteCache(db_path=tmp_path / "test_cache.db")


class TestToolCache:
    def test_miss_returns_none(self, db):
        assert db.get_tool("my_tool", {"arg": 1}) is None

    def test_hit_returns_data(self, db):
        payload = {"posts": [{"url": "https://example.com"}]}
        db.set_tool("my_tool", {"arg": 1}, payload, ttl=3600)
        result = db.get_tool("my_tool", {"arg": 1})
        assert result == payload

    def test_different_args_different_keys(self, db):
        db.set_tool("my_tool", {"limit": 10}, {"posts": ["A"]}, ttl=3600)
        db.set_tool("my_tool", {"limit": 20}, {"posts": ["B"]}, ttl=3600)
        assert db.get_tool("my_tool", {"limit": 10}) == {"posts": ["A"]}
        assert db.get_tool("my_tool", {"limit": 20}) == {"posts": ["B"]}

    def test_overwrite_same_key(self, db):
        db.set_tool("my_tool", {}, {"v": 1}, ttl=3600)
        db.set_tool("my_tool", {}, {"v": 2}, ttl=3600)
        assert db.get_tool("my_tool", {}) == {"v": 2}

    def test_expired_returns_none(self, db):
        db.set_tool("my_tool", {}, {"v": 1}, ttl=0)
        time.sleep(0.05)
        assert db.get_tool("my_tool", {}) is None

    def test_lazy_delete_on_expired_miss(self, db):
        db.set_tool("my_tool", {}, {"v": 1}, ttl=0)
        time.sleep(0.05)
        db.get_tool("my_tool", {})  # triggers lazy delete
        # Verify row was removed from DB
        conn = sqlite3.connect(str(db._db_path))
        rows = conn.execute("SELECT COUNT(*) FROM tool_cache").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_cleanup_removes_expired_keeps_valid(self, db):
        db.set_tool("expired", {}, {"v": 1}, ttl=0)
        db.set_tool("valid", {"k": 1}, {"v": 2}, ttl=3600)
        time.sleep(0.05)
        db.cleanup()
        assert db.get_tool("expired", {}) is None
        assert db.get_tool("valid", {"k": 1}) == {"v": 2}

    def test_different_tools_same_args_different_entries(self, db):
        db.set_tool("tool_a", {"x": 1}, {"data": "a"}, ttl=3600)
        db.set_tool("tool_b", {"x": 1}, {"data": "b"}, ttl=3600)
        assert db.get_tool("tool_a", {"x": 1}) == {"data": "a"}
        assert db.get_tool("tool_b", {"x": 1}) == {"data": "b"}

    def test_valid_entry_not_deleted_on_different_key_miss(self, db):
        """A true cache miss (unknown key) does not delete valid entries for other keys."""
        db.set_tool("my_tool", {"limit": 10}, {"posts": ["A"]}, ttl=3600)
        db.get_tool("my_tool", {"limit": 99})  # miss — different key
        assert db.get_tool("my_tool", {"limit": 10}) == {"posts": ["A"]}


class TestSeenComments:
    def test_not_seen_by_default(self, db):
        assert not db.is_seen_comment("https://linkedin.com/comments/123")

    def test_seen_after_mark(self, db):
        db.mark_seen_comments([
            {
                "comment_permalink": "https://linkedin.com/comments/123",
                "post_url": "https://linkedin.com/posts/x",
            }
        ])
        assert db.is_seen_comment("https://linkedin.com/comments/123")

    def test_mark_skips_none_permalink(self, db):
        db.mark_seen_comments([{"comment_permalink": None, "post_url": "https://p"}])
        # Should not raise
        assert not db.is_seen_comment("")

    def test_mark_skips_empty_permalink(self, db):
        db.mark_seen_comments([{"comment_permalink": "", "post_url": "https://p"}])
        # Should not raise, empty string not stored
        assert not db.is_seen_comment("")

    def test_mark_is_idempotent(self, db):
        item = {"comment_permalink": "https://linkedin.com/c/1", "post_url": "p"}
        db.mark_seen_comments([item])
        db.mark_seen_comments([item])  # second call must not raise
        assert db.is_seen_comment("https://linkedin.com/c/1")

    def test_mark_multiple(self, db):
        items = [
            {"comment_permalink": f"https://linkedin.com/c/{i}", "post_url": "p"}
            for i in range(5)
        ]
        db.mark_seen_comments(items)
        for i in range(5):
            assert db.is_seen_comment(f"https://linkedin.com/c/{i}")

    def test_unseen_permalink_not_affected_by_others(self, db):
        db.mark_seen_comments([
            {"comment_permalink": "https://linkedin.com/c/1", "post_url": "p"}
        ])
        assert not db.is_seen_comment("https://linkedin.com/c/2")
