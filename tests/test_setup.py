from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.setup import interactive_login


class _BrowserContextManager:
    def __init__(self, browser):
        self.browser = browser

    async def __aenter__(self):
        return self.browser

    async def __aexit__(self, exc_type, exc, tb):
        return None


def _make_browser(*, export_cookies: bool) -> MagicMock:
    browser = MagicMock()
    browser.page = MagicMock()
    browser.page.goto = AsyncMock()
    browser.context = MagicMock()
    browser.context.cookies = AsyncMock(
        return_value=[{"name": "li_at", "domain": ".linkedin.com"}]
    )
    browser.export_cookies = AsyncMock(return_value=export_cookies)
    return browser


@pytest.mark.asyncio
async def test_interactive_login_writes_source_state_when_cookie_export_succeeds(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=True)
    write_source_state = MagicMock(
        return_value=SimpleNamespace(login_generation="gen-123")
    )

    monkeypatch.setattr(
        "linkedin_mcp_server.setup.BrowserManager",
        lambda **kwargs: _BrowserContextManager(browser),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.warm_up_browser", AsyncMock())
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.resolve_remember_me_prompt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.wait_for_manual_login",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.write_source_state", write_source_state
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.asyncio.sleep", AsyncMock())

    assert await interactive_login(tmp_path / "profile") is True

    write_source_state.assert_called_once_with(tmp_path / "profile")
    captured = capsys.readouterr()
    assert "cookies exported for docker portability" in captured.out.lower()
    assert "source session generation: gen-123" in captured.out.lower()


@pytest.mark.asyncio
async def test_interactive_login_skips_source_state_when_cookie_export_fails(
    monkeypatch, tmp_path, capsys
):
    browser = _make_browser(export_cookies=False)
    write_source_state = MagicMock()

    monkeypatch.setattr(
        "linkedin_mcp_server.setup.BrowserManager",
        lambda **kwargs: _BrowserContextManager(browser),
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.warm_up_browser", AsyncMock())
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.resolve_remember_me_prompt",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.wait_for_manual_login",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.setup.write_source_state", write_source_state
    )
    monkeypatch.setattr("linkedin_mcp_server.setup.asyncio.sleep", AsyncMock())

    assert await interactive_login(tmp_path / "profile") is True

    write_source_state.assert_not_called()
    captured = capsys.readouterr()
    assert "warning: cookie export failed" in captured.out.lower()
