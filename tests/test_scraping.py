"""Tests for the LinkedInExtractor scraping engine."""

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.connection import (
    ActionSignals,
    detect_connection_state,
)
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    LinkedInExtractor,
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


class TestEveryNormalizedEntryPoint:
    """Each method that was rewired, refusing a value that redirects the path.

    Without this, removing normalization from one method leaves every other test
    untouched: the bare-identifier assertions build the same URL either way. The
    traversal value is the one input whose result differs, and it has to fail
    before any navigation rather than after one.

    ``_open_conversation_by_username`` left this table with the conversation
    reader and is covered against the owner in
    ``tests/scraping/test_conversations.py``; ``get_conversation`` stays,
    because normalizing a ``thread_id`` is still reached through the facade
    delegate.
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
