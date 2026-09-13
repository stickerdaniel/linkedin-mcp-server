"""Tests for the shared scraping page adapter."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.scraping import session as session_module
from linkedin_mcp_server.scraping.rate_limit import (
    RATE_LIMIT_RETRY_DELAY,
    RateLimitBudget,
)
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


async def test_pace_jitters_through_the_session_boundary(mock_page, monkeypatch):
    session = ScrapingSession(mock_page)
    sleep = AsyncMock()
    monkeypatch.setattr(session_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(session_module, "jitter", lambda base, *a, **kw: base * 1.25)

    await session.pace(2.0)

    sleep.assert_awaited_once_with(2.5)


async def test_pace_is_jittered_by_default(mock_page, monkeypatch):
    """Unpatched, no two pauses of the same base are equal.

    The point of `pace` over `delay` is the missing fixed period. Two hundred
    draws that all landed on the base would be the constant delay this exists
    to remove.
    """
    session = ScrapingSession(mock_page)
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(session_module.asyncio, "sleep", record)

    for _ in range(200):
        await session.pace(2.0)

    assert all(1.0 <= s <= 3.0 for s in slept)
    assert len(set(slept)) > 1


def test_each_session_carries_its_own_rate_limit_budget(mock_page):
    first = ScrapingSession(mock_page)
    second = ScrapingSession(mock_page)

    assert isinstance(first.rate_limit, RateLimitBudget)
    assert first.rate_limit is not second.rate_limit
    first.rate_limit.rate_limit_hits += 1
    assert second.rate_limit.rate_limit_hits == 0


async def test_claim_soft_retry_paces_through_the_session(mock_page, monkeypatch):
    session = ScrapingSession(mock_page)
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(session_module.asyncio, "sleep", record)
    monkeypatch.setattr(session_module, "jitter", lambda base, *a, **kw: base)

    assert await session.claim_soft_retry("https://www.linkedin.com/in/x/") is True
    assert await session.claim_soft_retry("https://www.linkedin.com/in/x/") is True
    assert await session.claim_soft_retry("https://www.linkedin.com/in/x/") is False

    assert slept == [RATE_LIMIT_RETRY_DELAY, RATE_LIMIT_RETRY_DELAY * 2]
    assert session.rate_limit.soft_retries_used == 2
