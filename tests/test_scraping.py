"""Tests for the LinkedInExtractor scraping engine."""

from contextlib import ExitStack
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import asyncio
import logging

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.connection import (
    ActionSignals,
    detect_connection_state,
)
from linkedin_mcp_server.scraping import extractor as extractor_module
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    LinkedInExtractor,
    _MESSAGE_COMPOSER_OWNER_JS,
    _MESSAGE_CONFIRMATION_DISPOSE_JS,
    _MESSAGE_CONFIRMATION_PREPARE_JS,
    _MESSAGE_CONFIRMATION_READY_JS,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestDetectConnectionState:
    """Tests for locale-independent connection-state detection.

    Every state is decided purely from the structural ActionSignals; no
    profile text is read for any state, including incoming_request (whose
    Accept/Ignore action row is fingerprinted by ``has_incoming_action_row``).
    """

    @staticmethod
    def _signals(
        invite: bool = False,
        compose_in_root: bool = False,
        edit: bool = False,
        labeled_action: bool = False,
        labeled_anchor: bool = False,
        incoming_row: bool = False,
    ) -> ActionSignals:
        return ActionSignals(
            has_invite_anchor=invite,
            has_compose_anchor_in_action_root=compose_in_root,
            has_edit_intro_anchor=edit,
            has_labeled_action_button=labeled_action,
            has_labeled_action_anchor=labeled_anchor,
            has_incoming_action_row=incoming_row,
        )

    def test_self_profile(self):
        assert detect_connection_state(self._signals(edit=True)) == "self_profile"

    def test_connectable(self):
        assert detect_connection_state(self._signals(invite=True)) == "connectable"

    def test_already_connected(self):
        # 1st-degree: Message anchor in action root, but no Follow/Connect/Pending
        # button (no aria-label on any action-root button).
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_action=False)
            )
            == "already_connected"
        )

    def test_follow_only(self):
        # No invite anchor anywhere, but a primary action <button> (Follow
        # / Save in Sales Navigator) is present alongside the Message
        # anchor.
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_action=True)
            )
            == "follow_only"
        )

    def test_pending_via_labeled_anchor(self):
        # Pending is rendered as <a aria-label="Pending, click to ..."> in
        # the action root — distinct from Follow's <button aria-label=...>.
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_anchor=True)
            )
            == "pending"
        )

    def test_pending_takes_priority_over_already_connected(self):
        # If the labeled anchor is present alongside compose-in-root with
        # no labeled button, pending wins over the already_connected
        # fallthrough that would otherwise apply.
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_anchor=True)
            )
            == "pending"
        )

    def test_incoming_request_via_structural_row(self):
        assert (
            detect_connection_state(self._signals(incoming_row=True))
            == "incoming_request"
        )

    def test_incoming_structural_beats_pending_misclassification(self):
        # Regression for the sidebar mis-anchor: on incoming profiles the
        # compose-anchor action-root walk lands on sidebar cards and
        # produces garbage signals (compose, labeled button, labeled
        # anchor all True). The structural incoming signal must win over
        # the pending check those garbage signals would trigger.
        assert (
            detect_connection_state(
                self._signals(
                    incoming_row=True,
                    compose_in_root=True,
                    labeled_action=True,
                    labeled_anchor=True,
                )
            )
            == "incoming_request"
        )

    def test_connectable_takes_priority_over_incoming_row(self):
        assert (
            detect_connection_state(self._signals(invite=True, incoming_row=True))
            == "connectable"
        )

    def test_self_profile_takes_priority_over_incoming_row(self):
        assert (
            detect_connection_state(self._signals(edit=True, incoming_row=True))
            == "self_profile"
        )

    def test_unavailable_when_no_signals(self):
        assert detect_connection_state(self._signals()) == "unavailable"

    def test_unavailable_when_compose_missing(self):
        # Restricted profile: no compose anchor, no labels, no invite.
        assert (
            detect_connection_state(self._signals(labeled_action=True)) == "unavailable"
        )


class TestSingleSectionRateLimits:
    """The single-page tools report the reason too, not just an empty result.

    Without these, three of the nine repaired call sites would be unbound: the
    branch could be deleted and the suite would stay green, because the older
    tests only assert the sentinel does not reach ``sections``.
    """

    @pytest.mark.parametrize(
        ("method", "args", "section"),
        [
            ("get_company_employees", ("testcorp",), "employees"),
            ("search_people", ("python",), "search_results"),
            ("search_companies", ("fintech",), "search_results"),
        ],
    )
    async def test_the_reason_is_reported(self, mock_page, method, args, section):
        extractor = LinkedInExtractor(mock_page)
        # Patched on the capture owner rather than on the facade: one of the
        # three methods reads through its own collaborator now, and a facade
        # patch would intercept nothing for it while still passing.
        with patch.object(
            SectionCapture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await getattr(extractor, method)(*args)

        assert result["sections"] == {}
        assert result["section_errors"][section]["error_type"] == "rate_limit"


class TestMessageTargetUrls:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
                "ACoAAB",
            ),
            (
                "https://de.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                "ACoAAB",
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=ACoAAB",
                "ACoAAB",
            ),
            ("http://www.linkedin.com/messaging/compose/?recipient=ACoAAB", None),
            ("https://evil.example/messaging/compose/?recipient=ACoAAB", None),
            ("//evil.example/messaging/compose/?recipient=ACoAAB", None),
            ("https://user@www.linkedin.com/messaging/compose/?recipient=ACoAAB", None),
            ("https://www.linkedin.com:444/messaging/compose/?recipient=ACoAAB", None),
            ("https://www.linkedin.com/jobs/?recipient=ACoAAB", None),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB#draft",
                None,
            ),
            ("https://www.linkedin.com/messaging/compose/?recipient=ACoAAB\n", None),
            ("https://www.linkedin.com/messaging/compose/?recipient=", None),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER",
                None,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER",
                None,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?profileUrn=malformed%3Aurn",
                None,
            ),
        ],
    )
    def test_compose_url_requires_one_linkedin_recipient(self, url, expected):
        assert extractor_module._profile_urn_from_compose_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.linkedin.com/in/testuser/", "/in/testuser/"),
            ("https://de.linkedin.com/in/testuser/", "/in/testuser/"),
            ("http://www.linkedin.com/in/testuser/", None),
            ("https://evil.example/in/testuser/", None),
            ("https://user@www.linkedin.com/in/testuser/", None),
            ("https://www.linkedin.com:444/in/testuser/", None),
            ("https://www.linkedin.com/in/testuser/edit/intro/", None),
            ("https://www.linkedin.com/in/testuser%2Fedit/", None),
            ("https://www.linkedin.com/in/testuser/?trk=profile", None),
            ("https://www.linkedin.com/in/testuser/#details", None),
        ],
    )
    def test_profile_url_requires_exact_linkedin_profile(self, url, expected):
        assert extractor_module._profile_path_from_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.linkedin.com/messaging/compose/", True),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
                True,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=ACoAAB&"
                "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                True,
            ),
            ("https://de.linkedin.com/messaging/thread/2-abc/", True),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                True,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?profileUrn=",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/"
                "?recipient=ACoAAB&recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/?profileUrn=",
                False,
            ),
            ("http://www.linkedin.com/messaging/compose/", False),
            ("https://evil.example/messaging/compose/", False),
            ("https://user@www.linkedin.com/messaging/thread/2-abc/", False),
            ("https://www.linkedin.com:444/messaging/compose/", False),
            ("https://www.linkedin.com/messaging/compose/#draft", False),
            ("https://www.linkedin.com/messaging/thread/2-abc%2Fother/", False),
            # Measured live: LinkedIn redirects an existing conversation to a
            # padded base64url id, and the padding reaches the path unescaped.
            (
                "https://www.linkedin.com/messaging/thread/"
                "2-ZDBkMjZiY2UtNjQwYi00NzczLWIxYWYtNTczZTZhZDkzMzQ4XzEwMA==/",
                True,
            ),
            ("https://www.linkedin.com/feed/", False),
        ],
    )
    def test_final_url_requires_safe_messaging_path(self, url, expected):
        assert extractor_module._message_page_url_is_safe(url, "ACoAAB") is expected


class TestReadProfileMessageTarget:
    async def test_accepts_safe_final_vanity_redirect(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/canonical-user/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB&"
                    "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
                ],
            }
        )

        resolution = await LinkedInExtractor(mock_page)._read_profile_message_target()

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_path == "/in/canonical-user/"
        assert resolution.target.profile_urn == "ACoAAB"


class TestGetInbox:
    async def test_returns_inbox_section(self, mock_page):
        """get_inbox returns sections with inbox key."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_wait_for_main_text",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_scroll_main_scrollable_region",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={
                    "text": "Conversation A\nConversation B",
                    "references": [],
                },
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Conversation A\nConversation B",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.get_inbox(limit=10)

        assert "sections" in result
        assert "inbox" in result["sections"]
        assert "Conversation A" in result["sections"]["inbox"]

    async def test_empty_inbox(self, mock_page):
        """get_inbox returns empty sections when page has no content."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="",
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.get_inbox(limit=5)

        assert result["sections"] == {}

    async def test_includes_conversation_thread_refs(self, mock_page):
        """get_inbox prepends conversation thread references from click extraction."""
        extractor = LinkedInExtractor(mock_page)
        thread_refs = [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-abc123/",
                "text": "Tony Chan",
                "context": "inbox",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-def456/",
                "text": "Paul Jasper",
                "context": "inbox",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={
                    "text": "Tony Chan\nPaul Jasper",
                    "references": [],
                },
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Tony Chan\nPaul Jasper",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            result = await extractor.get_inbox(limit=10)

        assert "references" in result
        refs = result["references"]["inbox"]
        assert len(refs) == 2
        assert refs[0]["kind"] == "conversation"
        assert refs[0]["url"] == "/messaging/thread/2-abc123/"
        assert refs[0]["text"] == "Tony Chan"


class TestGetConversation:
    async def test_returns_conversation_by_thread_id(self, mock_page):
        """get_conversation with thread_id navigates directly to thread URL."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Hello!\nHi there!", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Hello!\nHi there!",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            result = await extractor.get_conversation(thread_id="abc123")

        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/thread/abc123/"
        )
        assert result["sections"]["conversation"] == "Hello!\nHi there!"

    async def test_strips_conversation_page_chrome(self, mock_page):
        """get_conversation trims sidebar and composer chrome from the thread."""
        raw = (
            "Ada: Preview belonging to a different conversation\n"
            "Open the options list in your conversation with Ada and Grace\n"
            "Hello!\n"
            "Maximize compose field\n"
            "Open send options"
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": raw, "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            result = await extractor.get_conversation(thread_id="abc123")

        assert result["sections"]["conversation"] == "Hello!"

    async def test_raises_when_no_identifier(self, mock_page):
        """get_conversation raises LinkedInScraperException with no args."""
        extractor = LinkedInExtractor(mock_page)
        with pytest.raises(LinkedInScraperException):
            await extractor.get_conversation()

    async def test_by_username_default_index_picks_first_thread(self, mock_page):
        """get_conversation by username opens the 0th matching thread by default."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-newer/",
                    "https://www.linkedin.com/messaging/thread/2-older/",
                ],
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "msg", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="msg",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            await extractor.get_conversation(linkedin_username="jacki-old")

        target_calls = [
            c.args[0]
            for c in nav_mock.call_args_list
            if c.args and "/messaging/thread/" in c.args[0]
        ]
        assert target_calls == ["https://www.linkedin.com/messaging/thread/2-newer/"]

    async def test_by_username_index_picks_specified_thread(self, mock_page):
        """get_conversation by username + index opens the i-th matching thread."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-newer/",
                    "https://www.linkedin.com/messaging/thread/2-older/",
                ],
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "msg", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="msg",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            await extractor.get_conversation(linkedin_username="jacki-old", index=1)

        target_calls = [
            c.args[0]
            for c in nav_mock.call_args_list
            if c.args and "/messaging/thread/" in c.args[0]
        ]
        assert target_calls == ["https://www.linkedin.com/messaging/thread/2-older/"]

    async def test_by_username_index_out_of_range_raises(self, mock_page):
        """get_conversation raises when index exceeds the number of threads."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-only/",
                ],
            ),
        ):
            with pytest.raises(LinkedInScraperException, match="out of range"):
                await extractor.get_conversation(linkedin_username="jacki-old", index=5)

    async def test_by_username_no_threads_raises_could_not_find(self, mock_page):
        """get_conversation raises 'Could not find a conversation' when none exist."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            with pytest.raises(
                LinkedInScraperException, match="Could not find a conversation"
            ):
                await extractor.get_conversation(linkedin_username="jacki-old")


class TestStripSelectConversationPrefix:
    def test_strips_en_us_prefix(self):
        """Best-effort strip removes the en-US 'Select conversation with ' prefix."""
        assert (
            LinkedInExtractor._strip_select_conversation_prefix(
                "Select conversation with Jacki McMahan"
            )
            == "Jacki McMahan"
        )

    def test_case_insensitive(self):
        assert (
            LinkedInExtractor._strip_select_conversation_prefix(
                "select conversation with jacki mcmahan"
            )
            == "jacki mcmahan"
        )

    def test_returns_full_aria_when_prefix_absent(self):
        """In a non-en-US locale the verb prefix won't match; return as-is so
        downstream matching can endsWith / endswith on the participant name."""
        assert (
            LinkedInExtractor._strip_select_conversation_prefix(
                "Konversation auswählen mit Jacki McMahan"
            )
            == "Konversation auswählen mit Jacki McMahan"
        )

    def test_empty_input(self):
        assert LinkedInExtractor._strip_select_conversation_prefix("") == ""


class TestResolveConversationThreadUrls:
    async def test_inbox_enumeration_and_exact_aria_match(self, mock_page):
        """_resolve_conversation_thread_urls enumerates the plain inbox and
        matches participant by exact aria-label rather than substring."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        thread_refs = [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "text": "Jacki McMahan",  # exact match
                "context": "search",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-bbb/",
                "text": "Jacki McMahan-Group",  # extra suffix → not exact
                "context": "search",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-ccc/",
                "text": "Jacki McMahan",  # second exact match (multi-thread case)
                "context": "search",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            urls = await extractor._resolve_conversation_thread_urls("Jacki McMahan")

        nav_mock.assert_awaited_once_with("https://www.linkedin.com/messaging/")
        assert urls == [
            "https://www.linkedin.com/messaging/thread/2-aaa/",
            "https://www.linkedin.com/messaging/thread/2-ccc/",
        ]

    async def test_resolver_passes_name_filter_to_enumerator(self, mock_page):
        """_resolve_conversation_thread_urls scopes the click side effect by
        forwarding name_filter so only the participant's row is clicked."""
        extractor = LinkedInExtractor(mock_page)
        refs_mock = AsyncMock(
            return_value=[
                {
                    "kind": "conversation",
                    "url": "/messaging/thread/2-aaa/",
                    "text": "Jacki McMahan",
                    "context": "inbox",
                },
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(extractor, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await extractor._resolve_conversation_thread_urls("Jacki McMahan")

        refs_mock.assert_awaited_once_with(
            limit=ANY, context="inbox", name_filter="Jacki McMahan"
        )
        assert urls == ["https://www.linkedin.com/messaging/thread/2-aaa/"]

    async def test_resolver_falls_back_to_search_when_inbox_empty(self, mock_page):
        """When the inbox scan finds no match, resolution falls back to the
        messaging search for threads buried below the inbox window."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        # First call (inbox) finds nothing; second call (search) finds the thread.
        refs_mock = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "kind": "conversation",
                        "url": "/messaging/thread/2-ddd/",
                        "text": "Jacki McMahan",
                        "context": "search",
                    },
                ],
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(extractor, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await extractor._resolve_conversation_thread_urls("Jacki McMahan")

        assert nav_mock.await_args_list[0].args == (
            "https://www.linkedin.com/messaging/",
        )
        assert nav_mock.await_args_list[1].args == (
            "https://www.linkedin.com/messaging/?searchTerm=Jacki+McMahan",
        )
        assert refs_mock.await_count == 2
        assert urls == ["https://www.linkedin.com/messaging/thread/2-ddd/"]

    async def test_extract_refs_threads_name_filter_into_evaluate(self, mock_page):
        """_extract_conversation_thread_refs forwards name_filter into the
        in-browser click loop so non-matching rows are never clicked."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_selector = AsyncMock()
        captured: dict[str, object] = {}

        async def fake_evaluate(_js: str, arg: dict | None = None) -> list:
            captured["arg"] = arg
            return []

        mock_page.evaluate = fake_evaluate

        await extractor._extract_conversation_thread_refs(
            limit=50, context="inbox", name_filter="Jacki McMahan"
        )

        assert captured["arg"] == {"limit": 50, "nameFilter": "Jacki McMahan"}


class TestSearchConversations:
    async def test_returns_search_results(self, mock_page):
        """search_conversations returns search_results section."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()

        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Result 1\nResult 2", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Result 1\nResult 2",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.search_conversations("hello world")

        assert "search_results" in result["sections"]
        assert "Result 1" in result["sections"]["search_results"]
        # Search must be driven by the searchTerm URL parameter, not by typing
        # into the searchbox -- the URL form is reliable across SPA mounts and
        # preserves the search filter across click-to-capture navigations.
        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/?searchTerm=hello+world"
        )

    async def test_includes_conversation_thread_refs(self, mock_page):
        """search_conversations exposes per-result thread URLs as references."""
        extractor = LinkedInExtractor(mock_page)
        thread_refs = [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-abc/",
                "text": "Jacki McMahan",
                "context": "search_results",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-def/",
                "text": "Jacki McMahan",
                "context": "search_results",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Jacki McMahan\nJacki McMahan", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Jacki McMahan\nJacki McMahan",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ) as mock_refs,
        ):
            result = await extractor.search_conversations("Jacki")

        mock_refs.assert_awaited_once_with(limit=20, context="search_results")
        refs = result["references"]["search_results"]
        assert len(refs) == 2
        assert {ref["url"] for ref in refs} == {
            "/messaging/thread/2-abc/",
            "/messaging/thread/2-def/",
        }


class TestSendMessage:
    @pytest.mark.parametrize("message", ["", " \t\n"], ids=["empty", "whitespace"])
    async def test_blank_message_is_rejected_before_browser_interaction(
        self, mock_page, message
    ):
        extractor = LinkedInExtractor(mock_page)
        keyboard = MagicMock()
        mock_page.keyboard = keyboard

        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate:
            result = await extractor.send_message(
                "testuser", message, confirm_send=True
            )

        # Not `message_unavailable`: that status is about the recipient and
        # tells a caller to move on, while this one is about their own input.
        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "status": "invalid_message",
            "message": "Message must contain non-whitespace characters.",
            "recipient_selected": False,
            "sent": False,
            "retry_safe": True,
        }
        navigate.assert_not_awaited()
        mock_page.evaluate.assert_not_awaited()
        keyboard.type.assert_not_called()
        keyboard.press.assert_not_called()

    @pytest.mark.parametrize(
        "message",
        ["First\nSecond", "First\rSecond", "First\tSecond", "First\x7fSecond"],
        ids=["newline", "carriage-return", "tab", "del"],
    )
    async def test_control_message_is_rejected_before_browser_interaction(
        self, mock_page, message
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())

        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate:
            result = await extractor.send_message(
                "testuser", message, confirm_send=True
            )

        assert result["status"] == "invalid_message"
        assert result["message"] == (
            "Message must not contain control characters or line breaks."
        )
        assert result["retry_safe"] is True
        navigate.assert_not_awaited()
        mock_page.evaluate.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_unavailable_message_action_returns_connection_handoff(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())

        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution(
                    "unavailable"
                ),
            ),
            patch.object(
                extractor, "_wait_for_message_surface", new_callable=AsyncMock
            ) as surface,
            patch.object(
                extractor, "_read_message_composer_state", new_callable=AsyncMock
            ) as state,
            patch.object(
                extractor,
                "_focus_verified_message_editor",
                new_callable=AsyncMock,
            ) as focus,
            patch.object(
                extractor, "_submit_verified_message", new_callable=AsyncMock
            ) as submit,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "status": "message_unavailable",
            "message": (
                "LinkedIn did not expose a normal Message action for this profile. "
                "Use connect_with_person first, then retry only after the connection "
                "request is accepted."
            ),
            "recipient_selected": False,
            "sent": False,
            "retry_safe": True,
        }
        navigate.assert_awaited_once_with("https://www.linkedin.com/in/testuser/")
        surface.assert_not_awaited()
        state.assert_not_awaited()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_unresolved_profile_target_is_not_connection_handoff(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution("failed"),
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert "connect_with_person" not in result["message"]
        assert result["retry_safe"] is True

    @staticmethod
    def _target():
        return extractor_module._ProfileMessageTarget(
            profile_path="/in/testuser/",
            profile_urn="ACoAAB",
            compose_url=(
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
            ),
            display_name="Test User",
        )

    @staticmethod
    def _patch_to_composer(
        extractor,
        mock_page,
        *,
        states=None,
        submission="clicked",
        write_result="written",
    ):
        target = TestSendMessage._target()
        mock_page.url = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())
        owner = MagicMock()
        owner.as_element.return_value = owner
        owner.evaluate = AsyncMock(return_value="ready")
        owner.dispose = AsyncMock()
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        # An empty composer is the ordinary precondition for sending, so a
        # state that says nothing about it means empty. A case about a draft
        # still standing in the editor says `"empty": False` and gets it.
        def with_empty(state):
            if not isinstance(state, dict):
                return state
            return {
                "empty": True,
                "submitCount": 1,
                "submitUsable": True,
                **state,
            }

        if callable(states):
            inner = states

            async def states(*args, **kwargs):
                return with_empty(await inner(*args, **kwargs))
        elif states is not None:
            states = [with_empty(state) for state in states]
        return (
            target,
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution(
                    "resolved", target
                ),
            ),
            patch.object(
                extractor,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                extractor,
                "_read_message_composer_state",
                new_callable=AsyncMock,
                side_effect=states or None,
                return_value={
                    "status": "valid",
                    "active": False,
                    "empty": True,
                    "submitCount": 1,
                    "submitUsable": True,
                },
            ),
            patch.object(
                extractor,
                "_write_verified_message",
                new_callable=AsyncMock,
                return_value=write_result,
            ),
            patch.object(
                extractor,
                "_submit_verified_message",
                new_callable=AsyncMock,
                return_value=submission,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value="confirmation-token",
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        )

    async def test_dry_run_returns_before_focus_or_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=False
            )

        assert result["status"] == "confirmation_required"
        assert result["recipient_selected"] is True
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_supplied_urn_before_compose_navigation(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target = self._target()
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution(
                    "resolved", target
                ),
            ),
        ):
            result = await extractor.send_message(
                "testuser",
                "Hello!",
                confirm_send=True,
                profile_urn="OTHER",
            )

        assert result["status"] == "recipient_resolution_failed"
        navigate.assert_awaited_once_with("https://www.linkedin.com/in/testuser/")

    async def test_rejects_foreign_url_recipient_after_navigation(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        mock_page.url = "https://www.linkedin.com/messaging/compose/?recipient=OTHER"
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4] as surface,
            patches[5] as state,
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        surface.assert_not_awaited()
        state.assert_not_awaited()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_contradictory_url_before_focus(self, mock_page):
        extractor = LinkedInExtractor(mock_page)

        async def change_url_after_initial_state(_target):
            mock_page.url = (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER"
            )
            return {"status": "valid", "active": False}

        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=change_url_after_initial_state,
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5] as state,
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        state.assert_awaited_once()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_foreign_url_recipient_before_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        state_calls = 0

        async def change_url_during_prefocus_state(_target):
            nonlocal state_calls
            state_calls += 1
            if state_calls == 2:
                mock_page.url = (
                    "https://www.linkedin.com/messaging/compose/?recipient=OTHER"
                )
            return {"status": "valid", "active": state_calls > 1}

        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=change_url_during_prefocus_state,
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_queryless_route_switch_during_surface_wait_fails_closed(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        mock_page.url = alice_route

        async def switch_route(_target):
            mock_page.url = bob_route
            return "composer"

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4] as surface,
            patches[5] as state,
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            surface.side_effect = switch_route
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert result["retry_safe"] is True
        assert result["url"] == bob_route
        state.assert_not_awaited()
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_queryless_route_is_captured_before_owner_resolution(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        mock_page.url = alice_route

        async def switch_route(target, *, expected_route):
            assert target == self._target()
            assert expected_route == alice_route
            mock_page.url = bob_route
            return None

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
            patch.object(
                extractor,
                "_resolve_message_owner",
                new_callable=AsyncMock,
                side_effect=switch_route,
            ) as resolve_owner,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert result["retry_safe"] is True
        resolve_owner.assert_awaited_once_with(
            self._target(), expected_route=alice_route
        )
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_rejects_contradictory_url_before_submission(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        async def change_url_during_write(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            mock_page.url = (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER"
            )
            return "invalid"

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            write.side_effect = change_url_during_write
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        write.assert_awaited_once()
        mock_page.keyboard.type.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_refuses_a_composer_that_already_holds_a_draft(self, mock_page):
        """A draft in the editor is not ours to send, and not ours to clear."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            # The recipient check first, then the read taken immediately
            # before focus: that one still finds the author's draft.
            states=[
                {"status": "valid", "active": False},
                {"status": "valid", "active": False, "empty": False},
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "composer_occupied"
        assert result["sent"] is False
        # Nothing is typed, nothing is submitted, and the draft is left where
        # its author put it.
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_rejects_recipient_change_before_focus(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=[
                {"status": "valid", "active": False},
                {"status": "recipient_mismatch", "active": False},
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "compose_interact_failed"
        focus.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_editor_change_before_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=[
                {"status": "valid", "active": False},
                {"status": "valid", "active": False},
            ],
            write_result="invalid",
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "compose_interact_failed"
        mock_page.keyboard.type.assert_not_awaited()

    async def test_missing_owner_is_retryable_before_dispatch(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        owner = mock_page.evaluate_handle.return_value
        owner.as_element.return_value = None
        with ExitStack() as stack:
            entered = [stack.enter_context(item) for item in patches[1:]]
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        entered[6].assert_not_awaited()
        owner.dispose.assert_awaited_once_with()

    async def test_rejects_ambiguous_submit_after_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page, submission="invalid")
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_awaited_once()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_disabled_pinned_submit_cleans_before_retryable_failure(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        owner = mock_page.evaluate_handle.return_value
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9] as prepare,
            patches[10],
            patch.object(
                extractor,
                "_wait_for_verified_submit",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                extractor, "_cleanup_owned_message", new_callable=AsyncMock
            ) as cleanup,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_awaited_once()
        prepare.assert_not_awaited()
        submit.assert_not_awaited()
        cleanup.assert_awaited_once_with("Hello!", owner)

    @pytest.mark.parametrize(
        ("submit_count", "submit_usable"),
        [(0, False), (2, False)],
        ids=["missing", "ambiguous"],
    )
    async def test_only_one_active_submit_path_can_send(
        self, mock_page, submit_count, submit_usable
    ):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=[
                {"status": "valid"},
                {
                    "status": "valid",
                    "submitCount": submit_count,
                    "submitUsable": submit_usable,
                },
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_observer_is_prepared_after_typing_and_before_submission(
        self, mock_page
    ):
        """The mutation observer starts immediately before the only submit."""
        extractor = LinkedInExtractor(mock_page)
        steps: list[str] = []
        patches = self._patch_to_composer(extractor, mock_page)

        async def write(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("write")
            return "written"

        async def prepare(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("prepare")
            return "confirmation-token"

        async def submit(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("submit")
            return "clicked"

        async def confirmed(message, *, target, owner, confirmation):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append(f"confirm:{confirmation}")
            return True

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(
                extractor,
                "_write_verified_message",
                new_callable=AsyncMock,
                side_effect=write,
            ),
            patch.object(
                extractor,
                "_submit_verified_message",
                new_callable=AsyncMock,
                side_effect=submit,
            ),
            patches[8],
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                side_effect=prepare,
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                side_effect=confirmed,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "sent"
        assert steps == [
            "write",
            "prepare",
            "submit",
            "confirm:confirmation-token",
        ]

    async def test_interrupted_submission_is_not_a_failure(self, mock_page):
        """A click round trip can fail after dispatching the local event."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        visible = AsyncMock()

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as submit,
            patches[8],
            patches[9],
            patch.object(extractor, "_message_send_confirmed", visible),
        ):
            submit.side_effect = PatchrightError("execution context was destroyed")
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        visible.assert_not_awaited()

    @pytest.mark.parametrize(
        "stage",
        ["dispatch", "confirmation", "owner-cleanup"],
    )
    async def test_cancellation_after_dispatch_is_logged(
        self, mock_page, caplog, stage
    ):
        """Cancellation in the destructive window leaves a warning behind."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        if stage == "owner-cleanup":
            mock_page.evaluate_handle.return_value.dispose = AsyncMock(
                side_effect=asyncio.CancelledError()
            )

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10] as confirmed,
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.extractor"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            if stage == "dispatch":
                submit.side_effect = asyncio.CancelledError()
            elif stage == "confirmation":
                confirmed.side_effect = asyncio.CancelledError()
            await extractor.send_message("testuser", "Hello!", confirm_send=True)

        # Cancellation has to keep propagating, or the surrounding scope
        # never unwinds. The warning names the duplicate-delivery risk that
        # the discarded result can no longer report.
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("retry may deliver the message twice" in w for w in warnings), (
            warnings
        )

    async def test_cancellation_while_writing_does_not_warn(self, mock_page, caplog):
        """Validated text cannot submit before the explicit submit path."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.extractor"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            write.side_effect = asyncio.CancelledError()
            await extractor.send_message("testuser", "Hello there!", confirm_send=True)

        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("retry may deliver the message twice" in w for w in warnings)

    async def test_ordinary_error_after_dispatch_still_answers(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10] as confirmed,
        ):
            confirmed.side_effect = RuntimeError("context destroyed")
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_owner_cleanup_runs_when_confirmation_cleanup_fails(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        owner = mock_page.evaluate_handle.return_value

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            patch.object(
                extractor,
                "_dispose_message_confirmation",
                new_callable=AsyncMock,
                side_effect=RuntimeError("cleanup failed"),
            ),
            patch.object(
                extractor, "_dispose_message_owner", new_callable=AsyncMock
            ) as dispose_owner,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False
        dispose_owner.assert_awaited_once_with(owner)

    async def test_an_error_before_anything_can_submit_is_raised(self, mock_page):
        """Without a newline nothing has submitted yet, so the error is the answer.

        The pair to the case above. Reporting `send_unconfirmed` here would
        claim a duplicate-delivery risk that cannot exist and take the real
        error away from a caller who can simply retry.
        """
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            pytest.raises(RuntimeError, match="page closed"),
        ):
            write.side_effect = RuntimeError("page closed")
            await extractor.send_message("testuser", "Single line", confirm_send=True)

    async def test_send_unconfirmed_when_click_adds_nothing(self, mock_page):
        """A clicked Send button that changes nothing is not a sent message."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=False,
            ) as visible,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        # The click happened, so nothing here proves the message did not go
        # out. Answering "not sent" would invite a retry that delivers twice,
        # which is what `retry_safe` says and `sent` cannot.
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        visible.assert_awaited_once_with(
            "Hello!",
            target=self._target(),
            owner=mock_page.evaluate_handle.return_value,
            confirmation=1,
        )


class TestResolveMessageComposeBox:
    async def test_requires_exactly_one_visible_editor(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        locator = MagicMock(count=AsyncMock(return_value=2))
        locator.first = MagicMock()
        mock_page.locator.return_value = locator

        assert await extractor._resolve_message_compose_box() is None

        mock_page.locator.assert_called_once_with(
            f"{extractor_module._MESSAGING_COMPOSE_SELECTOR}:visible"
        )


class TestMessageConfirmation:
    """Tests for the owner-pinned message-list mutation contract."""

    @staticmethod
    def _arguments():
        target = TestSendMessage._target()
        owner = MagicMock()
        return target, owner

    async def test_owner_handle_uses_the_shared_recipient_inspection(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        owner.as_element.return_value = owner
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        expected_route = "https://www.linkedin.com/messaging/thread/ALICE/"

        assert (
            await extractor._resolve_message_owner(
                target, expected_route=expected_route
            )
            is owner
        )

        mock_page.evaluate_handle.assert_awaited_once_with(
            _MESSAGE_COMPOSER_OWNER_JS,
            arg={
                "target": {
                    "profilePath": target.profile_path,
                    "profileUrn": target.profile_urn,
                },
                "expectedRoute": expected_route,
            },
        )

    async def test_invalid_owner_handle_is_released(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        owner.as_element.return_value = None
        owner.dispose = AsyncMock()
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        assert (
            await extractor._resolve_message_owner(
                target,
                expected_route="https://www.linkedin.com/messaging/thread/ALICE/",
            )
            is None
        )
        owner.dispose.assert_awaited_once_with()

    async def test_owner_disposal_error_is_suppressed(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        owner = MagicMock(dispose=AsyncMock(side_effect=RuntimeError("closed")))

        await extractor._dispose_message_owner(owner)

        owner.dispose.assert_awaited_once_with()

    async def test_prepare_installs_observer_in_the_target_owner(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.evaluate = AsyncMock(return_value="confirmation-token")

        assert (
            await extractor._prepare_message_confirmation(
                "Hello!", target=target, owner=owner
            )
            == "confirmation-token"
        )
        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_PREPARE_JS,
            {
                "profilePath": target.profile_path,
                "profileUrn": target.profile_urn,
                "expected": "Hello!",
                "owner": owner,
            },
        )

    @pytest.mark.parametrize("result", [None, "", 0, {"token": "wrong"}])
    async def test_invalid_prepare_result_fails_closed(self, mock_page, result):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.evaluate = AsyncMock(return_value=result)

        assert (
            await extractor._prepare_message_confirmation(
                "Hello!", target=target, owner=owner
            )
            is None
        )

    async def test_confirmation_waits_for_the_exact_token(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.wait_for_function = AsyncMock(return_value=None)

        assert (
            await extractor._message_send_confirmed(
                "Hello!",
                target=target,
                owner=owner,
                confirmation="confirmation-token",
            )
            is True
        )
        mock_page.wait_for_function.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_READY_JS,
            arg={
                "profilePath": target.profile_path,
                "profileUrn": target.profile_urn,
                "expected": "Hello!",
                "owner": owner,
                "token": "confirmation-token",
            },
        )

    @pytest.mark.parametrize(
        "error",
        [
            PlaywrightTimeoutError("timeout"),
            PatchrightError("execution context destroyed"),
        ],
        ids=["timeout", "context-destroyed"],
    )
    async def test_confirmation_errors_do_not_confirm(self, mock_page, error):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.wait_for_function = AsyncMock(side_effect=error)

        assert (
            await extractor._message_send_confirmed(
                "Hello!",
                target=target,
                owner=owner,
                confirmation="confirmation-token",
            )
            is False
        )

    async def test_dispose_disconnects_the_owner_token(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        _target, owner = self._arguments()
        mock_page.evaluate = AsyncMock()

        await extractor._dispose_message_confirmation(owner, "confirmation-token")

        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_DISPOSE_JS,
            {"owner": owner, "token": "confirmation-token"},
        )


class TestEveryNormalizedEntryPoint:
    """Each method that was rewired, refusing a value that redirects the path.

    Without this, removing normalization from one method leaves every other test
    untouched: the bare-identifier assertions build the same URL either way. The
    traversal value is the one input whose result differs, and it has to fail
    before any navigation rather than after one.
    """

    @staticmethod
    def _calls(extractor: LinkedInExtractor):
        # Both patches land on the owner class rather than on the facade
        # instance, because two of the parameterized methods now reach the
        # capture through a collaborator of their own. A facade patch would
        # intercept nothing for those and the assertion would hold whatever
        # the code did.
        return (
            patch.object(SectionCapture, "extract_page", new_callable=AsyncMock),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        )

    @pytest.mark.parametrize(
        "method,args,kwargs",
        [
            ("scrape_person", ("../../feed", {"main_profile"}), {}),
            ("connect_with_person", ("../../feed",), {}),
            ("get_sidebar_profiles", ("../../feed",), {}),
            ("_open_conversation_by_username", ("../../feed",), {}),
            ("send_message", ("../../feed", "hi"), {"confirm_send": False}),
            ("scrape_company", ("../../feed", {"about"}), {}),
            ("get_company_employees", ("../../feed",), {}),
            ("scrape_job", ("../../feed",), {}),
            ("get_conversation", (), {"thread_id": "../../feed"}),
        ],
    )
    async def test_refuses_a_traversal_value_before_navigating(
        self, mock_page, method: str, args: tuple, kwargs: dict
    ):
        extractor = LinkedInExtractor(mock_page)
        extract_patch, navigate_patch = self._calls(extractor)
        with extract_patch as mock_extract, navigate_patch as mock_navigate:
            with pytest.raises(InvalidReferenceError):
                await getattr(extractor, method)(*args, **kwargs)
        mock_extract.assert_not_called()
        mock_navigate.assert_not_called()
