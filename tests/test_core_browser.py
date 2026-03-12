"""Tests for BrowserManager.import_cookies domain filtering and auth-cookie validation."""

import json
from unittest.mock import AsyncMock, MagicMock

from linkedin_mcp_server.core.browser import BrowserManager


def _make_browser(tmp_path) -> BrowserManager:
    """Create a BrowserManager with a mock context pointed at tmp_path."""
    bm = BrowserManager(user_data_dir=tmp_path / "profile")
    bm._context = MagicMock()
    bm._context.clear_cookies = AsyncMock()
    bm._context.add_cookies = AsyncMock()
    return bm


def _cookie(name: str, domain: str = ".linkedin.com", value: str = "v") -> dict:
    return {"name": name, "domain": domain, "value": value, "path": "/"}


async def test_import_only_linkedin_cookies_returns_true(tmp_path):
    """Cookie file with only LinkedIn cookies (including li_at) → True, all imported."""
    bm = _make_browser(tmp_path)
    cookies = [
        _cookie("li_at"),
        _cookie("JSESSIONID"),
        _cookie("bcookie"),
    ]
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps(cookies))

    result = await bm.import_cookies(cookie_path)

    assert result is True
    bm._context.clear_cookies.assert_awaited_once()
    bm._context.add_cookies.assert_awaited_once()
    imported = bm._context.add_cookies.call_args[0][0]
    assert len(imported) == 3


async def test_import_mixed_domains_filters_non_linkedin(tmp_path):
    """Mixed-domain cookies → only LinkedIn cookies are imported."""
    bm = _make_browser(tmp_path)
    cookies = [
        _cookie("li_at", ".linkedin.com"),
        _cookie("session_id", ".google.com"),
        _cookie("bcookie", ".linkedin.com"),
        _cookie("_ga", ".example.com"),
    ]
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps(cookies))

    result = await bm.import_cookies(cookie_path)

    assert result is True
    imported = bm._context.add_cookies.call_args[0][0]
    assert len(imported) == 2
    imported_names = {c["name"] for c in imported}
    assert imported_names == {"li_at", "bcookie"}


async def test_import_linkedin_cookies_without_auth_returns_false(tmp_path):
    """LinkedIn cookies but neither li_at nor li_rm → False."""
    bm = _make_browser(tmp_path)
    cookies = [
        _cookie("JSESSIONID", ".linkedin.com"),
        _cookie("bcookie", ".linkedin.com"),
    ]
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps(cookies))

    result = await bm.import_cookies(cookie_path)

    assert result is False
    bm._context.add_cookies.assert_not_awaited()


async def test_import_empty_cookie_file_returns_false(tmp_path):
    """Empty cookie file → False."""
    bm = _make_browser(tmp_path)
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([]))

    result = await bm.import_cookies(cookie_path)

    assert result is False
    bm._context.add_cookies.assert_not_awaited()


async def test_import_missing_cookie_file_returns_false(tmp_path):
    """Missing cookie file → False."""
    bm = _make_browser(tmp_path)
    cookie_path = tmp_path / "nonexistent.json"

    result = await bm.import_cookies(cookie_path)

    assert result is False


async def test_import_no_context_returns_false(tmp_path):
    """No browser context → False."""
    bm = BrowserManager(user_data_dir=tmp_path / "profile")
    bm._context = None
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps([_cookie("li_at")]))

    result = await bm.import_cookies(cookie_path)

    assert result is False


async def test_import_normalizes_www_domain(tmp_path):
    """Cookies with .www.linkedin.com domain are normalized to .linkedin.com."""
    bm = _make_browser(tmp_path)
    cookies = [
        _cookie("li_at", ".www.linkedin.com"),
        _cookie("bcookie", "www.linkedin.com"),
    ]
    cookie_path = tmp_path / "cookies.json"
    cookie_path.write_text(json.dumps(cookies))

    result = await bm.import_cookies(cookie_path)

    assert result is True
    imported = bm._context.add_cookies.call_args[0][0]
    assert all(c["domain"] == ".linkedin.com" for c in imported)
