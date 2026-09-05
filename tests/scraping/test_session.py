"""Tests for the shared scraping page adapter."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.scraping import session as session_module
from linkedin_mcp_server.scraping.session import ScrapingSession


def test_page_binding_is_frozen(mock_page):
    session = ScrapingSession(mock_page)

    with pytest.raises(FrozenInstanceError):
        setattr(session, "page", mock_page)


async def test_clock_and_delay_use_the_session_boundaries(mock_page, monkeypatch):
    session = ScrapingSession(mock_page)
    sleep = AsyncMock()

    monkeypatch.setattr(session_module.time, "monotonic", lambda: 17.5)
    monkeypatch.setattr(session_module.asyncio, "sleep", sleep)

    assert session.monotonic() == 17.5
    await session.delay(0.25)

    sleep.assert_awaited_once_with(0.25)


async def test_modal_and_rate_limit_helpers_receive_the_bound_page(
    mock_page, monkeypatch
):
    session = ScrapingSession(mock_page)
    rate_limit = AsyncMock()
    modal = AsyncMock(return_value=True)

    monkeypatch.setattr(session_module, "detect_rate_limit", rate_limit)
    monkeypatch.setattr(session_module, "handle_modal_close", modal)

    await session.check_rate_limit()
    assert await session.dismiss_modal() is True

    rate_limit.assert_awaited_once_with(mock_page)
    modal.assert_awaited_once_with(mock_page)


async def test_scroll_body_delegates_the_utility_defaults(mock_page, monkeypatch):
    session = ScrapingSession(mock_page)
    scroll = AsyncMock()
    monkeypatch.setattr(session_module, "scroll_to_bottom", scroll)

    await session.scroll_body()

    scroll.assert_awaited_once_with(mock_page, pause_time=1.0, max_scrolls=10)


async def test_scroll_sidebar_delegates_every_utility_default(mock_page, monkeypatch):
    session = ScrapingSession(mock_page)
    scroll = AsyncMock(return_value=True)
    monkeypatch.setattr(session_module, "scroll_job_sidebar", scroll)

    assert await session.scroll_job_sidebar() is True

    scroll.assert_awaited_once_with(
        mock_page,
        settle_timeout=3.0,
        poll_interval=0.15,
        min_budget=0.4,
        max_scrolls=10,
        deadline=12.0,
    )
