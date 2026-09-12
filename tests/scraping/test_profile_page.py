"""Tests for the profile-identity reader."""

from __future__ import annotations

from unittest.mock import AsyncMock

from linkedin_mcp_server.scraping import message_sender as message_sender_module
from linkedin_mcp_server.scraping.message_sender import MessageSender
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.profile_page import ProfilePageReader
from linkedin_mcp_server.scraping.session import ScrapingSession


def _reader(page) -> ProfilePageReader:
    """Wire the reader the way the facade does."""
    session = ScrapingSession(page)
    sender = MessageSender(session, PageNavigator(session))
    return ProfilePageReader(session, sender._read_profile_message_target)


class TestExtractProfileUrn:
    async def test_returns_urn_from_atomic_top_card_snapshot(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/testuser/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB&"
                    "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
                ],
            }
        )

        result = await _reader(mock_page)._extract_profile_urn()

        assert result == "ACoAAB"
        mock_page.evaluate.assert_awaited_once_with(
            message_sender_module._PROFILE_MESSAGE_TARGET_JS
        )

    async def test_returns_none_for_ambiguous_top_card_links(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/testuser/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB",
                    "/messaging/compose/?recipient=OTHER",
                ],
            }
        )

        result = await _reader(mock_page)._extract_profile_urn()

        assert result is None


class TestReadProfileDisplayName:
    """The name read the conversation lookup matches threads against.

    Nothing else in the suite drives this read: every caller stubs it, so
    without these the program could return the wrong line and stay green.
    """

    async def test_the_heading_is_preferred_over_the_body(self, mock_page):
        mock_page.evaluate = AsyncMock(return_value="  Ada   Lovelace ")

        assert await _reader(mock_page)._read_profile_display_name() == (
            "Ada   Lovelace"
        )

    async def test_a_blank_answer_is_reported_as_no_name(self, mock_page):
        mock_page.evaluate = AsyncMock(return_value="   ")

        assert await _reader(mock_page)._read_profile_display_name() is None

    async def test_a_non_string_answer_is_reported_as_no_name(self, mock_page):
        # What a page whose <main> is missing answers through a driver that
        # returns `null` rather than the empty string the program intends.
        mock_page.evaluate = AsyncMock(return_value=None)

        assert await _reader(mock_page)._read_profile_display_name() is None
