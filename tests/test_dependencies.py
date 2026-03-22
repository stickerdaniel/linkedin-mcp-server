"""Tests for the get_extractor dependency injection context manager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetExtractor:
    async def test_triggers_rotation_when_should_rotate(self):
        """hard_reset_browser is called BEFORE get_or_create_browser when should_rotate()."""
        call_order = []

        async def mock_hard_reset():
            call_order.append("hard_reset")

        async def mock_get_browser():
            call_order.append("get_browser")
            browser = MagicMock()
            browser.page = MagicMock()
            return browser

        async def mock_ensure_auth():
            pass

        with (
            patch(
                "linkedin_mcp_server.dependencies.should_rotate",
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.dependencies.hard_reset_browser",
                side_effect=mock_hard_reset,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                side_effect=mock_get_browser,
            ),
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                side_effect=mock_ensure_auth,
            ),
            patch("linkedin_mcp_server.dependencies.record_scrape"),
        ):
            from linkedin_mcp_server.dependencies import get_extractor

            async with get_extractor():
                pass

        assert call_order == ["hard_reset", "get_browser"]

    async def test_no_rotation_below_threshold(self):
        """hard_reset_browser is NOT called when should_rotate() is False."""
        hard_reset_mock = AsyncMock()
        browser = MagicMock()
        browser.page = MagicMock()

        with (
            patch(
                "linkedin_mcp_server.dependencies.should_rotate",
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.dependencies.hard_reset_browser",
                hard_reset_mock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=browser,
            ),
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
            ),
            patch("linkedin_mcp_server.dependencies.record_scrape"),
        ):
            from linkedin_mcp_server.dependencies import get_extractor

            async with get_extractor():
                pass

        hard_reset_mock.assert_not_awaited()

    async def test_record_scrape_called_in_finally_on_success(self):
        """record_scrape is called in the finally block after a successful yield."""
        record_mock = MagicMock()
        browser = MagicMock()
        browser.page = MagicMock()

        with (
            patch(
                "linkedin_mcp_server.dependencies.should_rotate",
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.dependencies.hard_reset_browser",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=browser,
            ),
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.record_scrape",
                record_mock,
            ),
        ):
            from linkedin_mcp_server.dependencies import get_extractor

            async with get_extractor():
                pass

        record_mock.assert_called_once()

    async def test_record_scrape_called_in_finally_on_exception(self):
        """record_scrape is called even when the consumer raises an exception."""
        record_mock = MagicMock()
        browser = MagicMock()
        browser.page = MagicMock()

        with (
            patch(
                "linkedin_mcp_server.dependencies.should_rotate",
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.dependencies.hard_reset_browser",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.get_or_create_browser",
                new_callable=AsyncMock,
                return_value=browser,
            ),
            patch(
                "linkedin_mcp_server.dependencies.ensure_authenticated",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.dependencies.record_scrape",
                record_mock,
            ),
        ):
            from linkedin_mcp_server.dependencies import get_extractor

            with pytest.raises(RuntimeError):
                async with get_extractor():
                    raise RuntimeError("consumer error")

        record_mock.assert_called_once()
