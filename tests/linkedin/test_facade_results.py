"""Facade result and error propagation contracts."""

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin import LinkedInExtractor
from linkedin_mcp_server.linkedin.capture import SectionCapture
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.navigation import PageNavigator


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    return ExtractedSection(text=text, references=references or [], error=error)


class TestPostPublishingDelegates:
    async def test_prepare_reads_own_author_then_preserves_exact_text(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        author = "https://www.linkedin.com/in/test-author/"
        with (
            patch.object(
                extractor, "get_my_profile", AsyncMock(return_value={"url": author})
            ) as own,
            patch.object(
                extractor._post_writer, "prepare", return_value={"status": "preview"}
            ) as prepare,
        ):
            assert await extractor.prepare_post("Exact\ntext") == {"status": "preview"}
            own.assert_awaited_once_with({"main_profile"})
            prepare.assert_called_once_with("Exact\ntext", author)

    async def test_publish_preserves_confirmation_and_error(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor._post_writer,
            "publish",
            AsyncMock(side_effect=ValueError("Preview expired")),
        ) as publish:
            with pytest.raises(ValueError, match="expired"):
                await extractor.publish_post("id", "Exact text", confirmed=True)
            publish.assert_awaited_once_with("id", "Exact text", confirmed=True)


class TestSingleSectionRateLimits:
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
        with patch.object(
            SectionCapture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await getattr(extractor, method)(*args)

        assert result["sections"] == {}
        assert result["section_errors"][section]["error_type"] == "rate_limit"


class TestEveryNormalizedEntryPoint:
    @pytest.mark.parametrize(
        "method,args,kwargs",
        [
            ("read_person", ("../../feed", {"main_profile"}), {}),
            ("connect_with_person", ("../../feed",), {}),
            ("get_sidebar_profiles", ("../../feed",), {}),
            ("send_message", ("../../feed", "hi"), {"confirm_send": False}),
            ("read_company", ("../../feed", {"about"}), {}),
            ("get_company_employees", ("../../feed",), {}),
            ("read_job", ("../../feed",), {}),
            ("get_conversation", (), {"thread_id": "../../feed"}),
        ],
    )
    async def test_refuses_a_traversal_value_before_navigating(
        self, mock_page, method: str, args: tuple, kwargs: dict
    ):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(SectionCapture, "capture", new_callable=AsyncMock) as capture,
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
        ):
            with pytest.raises(InvalidReferenceError):
                await getattr(extractor, method)(*args, **kwargs)

        capture.assert_not_called()
        navigate.assert_not_called()
