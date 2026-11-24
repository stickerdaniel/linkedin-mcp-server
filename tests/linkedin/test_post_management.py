"""Synthetic contracts for private post analytics and scheduled-post reading."""

from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.capture import CaptureMode, CapturePlan
from linkedin_mcp_server.linkedin.contracts import (
    ExtractedSection,
    RATE_LIMITED_SECTION_TEXT,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.post_management import (
    PostManagement,
    post_analytics_url,
)


POST = "https://www.linkedin.com/feed/update/urn:li:activity:123456789/"
ANALYTICS = "https://www.linkedin.com/analytics/post-summary/urn:li:activity:123456789/"


def management():
    session = AsyncMock()
    navigator = AsyncMock()
    capture = AsyncMock()
    profile = AsyncMock()
    return (
        PostManagement(session, navigator, capture, profile),
        session,
        navigator,
        capture,
        profile,
    )


@pytest.mark.parametrize(
    "value",
    [POST, "urn:li:activity:123456789", "/posts/synthetic-activity-123456789-ABcd"],
)
def test_analytics_uses_the_requested_activity_and_discards_tracking(value):
    assert post_analytics_url(value) == ANALYTICS


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/post",
        "/feed/",
        "urn:li:share:123456789",
        "/posts/a-activity-111-x-share-222-token/",
        "/posts/a-activity-111-x-activity-222-token/",
        "/posts/x-activity-111-share-222-tail/",
    ],
)
async def test_unverifiable_activity_is_refused_before_any_page_access(value):
    reader, session, navigator, capture, profile = management()
    with pytest.raises(InvalidReferenceError):
        await reader.get_post_analytics(value)
    capture.capture.assert_not_awaited()
    navigator._navigate_to_page.assert_not_awaited()


async def test_my_posts_uses_the_own_profile_resolver_and_requested_scroll_budget():
    reader, session, navigator, capture, profile = management()
    profile_references: list[Reference] = [{"kind": "person", "url": "/in/synthetic/"}]
    post_references: list[Reference] = [
        {"kind": "feed_post", "url": "/feed/update/urn:li:activity:123/"}
    ]
    profile_error = {
        "error_type": "partial_result",
        "error_message": "Synthetic profile",
    }
    post_error = {"error_type": "partial_result", "error_message": "Synthetic posts"}
    profile.return_value = {
        "url": "https://www.linkedin.com/in/synthetic/",
        "sections": {"main_profile": "Synthetic owner"},
        "profile_urn": "urn:li:fsd_profile:SYNTHETIC",
        "references": {"main_profile": profile_references},
        "section_errors": {"main_profile": profile_error},
    }
    capture.capture.return_value = ExtractedSection(
        "An own published post", post_references, error=post_error
    )

    result = await reader.get_my_posts(max_scrolls=7)

    assert result == {
        "url": "https://www.linkedin.com/in/synthetic/",
        "sections": {
            "main_profile": "Synthetic owner",
            "posts": "An own published post",
        },
        "profile_urn": "urn:li:fsd_profile:SYNTHETIC",
        "references": {"main_profile": profile_references, "posts": post_references},
        "section_errors": {"main_profile": profile_error, "posts": post_error},
    }
    profile.assert_awaited_once_with({"main_profile"}, max_scrolls=7)
    capture.capture.assert_awaited_once_with(
        "https://www.linkedin.com/in/synthetic/recent-activity/posts/",
        "posts",
        CapturePlan(CaptureMode.ACTIVITY, max_scrolls=7),
    )


async def test_my_posts_preserves_profile_when_posts_are_rate_limited():
    reader, _, _, capture, profile = management()
    profile.return_value = {
        "url": "https://www.linkedin.com/in/synthetic/",
        "sections": {"main_profile": "Synthetic owner"},
    }
    capture.capture.return_value = ExtractedSection(RATE_LIMITED_SECTION_TEXT, [])

    result = await reader.get_my_posts()

    assert result["sections"] == {"main_profile": "Synthetic owner"}
    assert result["section_errors"]["posts"]["error_type"] == "rate_limit"


async def test_my_posts_stops_after_a_profile_rate_limit():
    reader, _, _, capture, profile = management()
    profile.return_value = {
        "url": "https://www.linkedin.com/in/synthetic/",
        "sections": {},
        "section_errors": {"main_profile": {"error_type": "rate_limit"}},
    }
    result = await reader.get_my_posts()
    assert result["sections"] == {}
    capture.capture.assert_not_awaited()


@pytest.mark.parametrize("url", ["https://example.com/in/synthetic/", "/in/me/"])
async def test_my_posts_requires_a_resolved_personal_profile(url):
    reader, _, _, capture, profile = management()
    profile.return_value = {"url": url, "sections": {}}
    with pytest.raises(InvalidReferenceError):
        await reader.get_my_posts()
    capture.capture.assert_not_awaited()


async def test_analytics_returns_visible_counts_without_interpreting_or_filling_them():
    reader, session, navigator, capture, profile = management()
    capture.capture.return_value = ExtractedSection(
        "Reactions\n8\nComments\n0\nReposts\n0", []
    )
    result = await reader.get_post_analytics(POST)
    assert result == {
        "url": ANALYTICS,
        "sections": {"post_analytics": "Reactions\n8\nComments\n0\nReposts\n0"},
    }
    assert capture.capture.await_args.args[0] == ANALYTICS


async def test_analytics_empty_capture_is_reported_as_an_error():
    reader, session, navigator, capture, profile = management()
    capture.capture.return_value = ExtractedSection("", [])
    result = await reader.get_post_analytics(POST)
    assert result["sections"] == {}
    assert result["section_errors"]["post_analytics"]["error_type"] == "empty_content"


async def test_analytics_rate_limit_is_never_returned_as_content():
    reader, session, navigator, capture, profile = management()
    capture.capture.return_value = ExtractedSection(RATE_LIMITED_SECTION_TEXT, [])
    result = await reader.get_post_analytics(POST)
    assert result["sections"] == {}
    assert result["section_errors"]["post_analytics"]["error_type"] == "rate_limit"


async def test_existing_operator_dialog_is_preserved_before_navigation():
    reader, session, navigator, capture, profile = management()
    session.run_on_linkedin.return_value = {"ok": False, "reason": "existing_dialog"}
    with pytest.raises(ValueError, match="existing_dialog"):
        await reader.get_scheduled_posts()
    navigator._navigate_to_page.assert_not_awaited()
