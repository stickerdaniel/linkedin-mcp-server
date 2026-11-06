"""Contracts for reading one post without performing a social action."""

from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
)
from linkedin_mcp_server.linkedin.capture import CaptureMode, CapturePlan
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.post_reader import PostReader


def reader_with(section: ExtractedSection):
    capture = AsyncMock()
    capture.capture.return_value = section
    return PostReader(capture), capture


async def test_reads_one_permalink_with_the_requested_scroll_budget():
    text = "A synthetic post\n18 reactions\n3 comments\n2 reposts\nA comment"
    reader, capture = reader_with(ExtractedSection(text=text, references=[]))

    result = await reader.get_post("urn:li:activity:1234567890123", max_scrolls=7)

    assert result == {
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:1234567890123/",
        "sections": {"post": text},
    }
    capture.capture.assert_awaited_once_with(
        result["url"],
        section_name="post",
        plan=CapturePlan(CaptureMode.STANDARD, max_scrolls=7),
    )


async def test_preserves_links_to_rendered_comment_authors_and_original_posts():
    references: list[Reference] = [
        {"kind": "person", "url": "/in/synthetic-commenter/"},
        {"kind": "feed_post", "url": "/feed/update/urn:li:activity:9876543210987/"},
    ]
    reader, _ = reader_with(ExtractedSection(text="Post", references=references))

    result = await reader.get_post("urn:li:share:1234567890123")

    assert result["references"] == {"post": references}


async def test_an_empty_page_has_no_fabricated_engagement_or_optional_fields():
    reader, _ = reader_with(ExtractedSection(text="", references=[]))

    result = await reader.get_post("urn:li:ugcPost:1234567890123")

    assert result == {
        "url": "https://www.linkedin.com/feed/update/urn:li:ugcPost:1234567890123/",
        "sections": {},
    }


async def test_an_extraction_failure_is_reported_as_a_section_error():
    error = {"error_type": "TimeoutError", "error_message": "Synthetic timeout"}
    reader, _ = reader_with(ExtractedSection(text="", references=[], error=error))

    result = await reader.get_post("urn:li:activity:1234567890123")

    assert result["sections"] == {}
    assert result["section_errors"] == {"post": error}


async def test_the_rate_limit_sentinel_is_never_returned_as_post_content():
    reader, _ = reader_with(
        ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
    )

    result = await reader.get_post("urn:li:activity:1234567890123")

    assert result["sections"] == {}
    assert result["section_errors"] == {"post": rate_limited_section_error()}


async def test_invalid_links_are_refused_before_any_capture():
    reader, capture = reader_with(ExtractedSection(text="wrong page", references=[]))

    with pytest.raises(InvalidReferenceError):
        await reader.get_post("https://example.com/feed/update/urn:li:activity:123/")

    capture.capture.assert_not_awaited()


async def test_authentication_errors_are_not_disguised_as_empty_posts():
    reader, capture = reader_with(ExtractedSection(text="", references=[]))
    capture.capture.side_effect = AuthenticationError("Sign in is required")

    with pytest.raises(AuthenticationError, match="Sign in"):
        await reader.get_post("urn:li:activity:1234567890123")
