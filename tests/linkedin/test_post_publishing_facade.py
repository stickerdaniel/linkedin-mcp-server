"""Post facade contracts, patched at the public workflow-owner boundary."""

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.linkedin import LinkedInExtractor
from linkedin_mcp_server.linkedin.person import PersonReader
from linkedin_mcp_server.linkedin.post_writer import PostWriter


AUTHOR = "https://www.linkedin.com/in/synthetic-author/"
TEXT = "  Synthetic preview.\n\nPreserve café, emoji 🎵 and trailing spaces.  "


@pytest.fixture(autouse=True)
def available_browser(mock_page):
    """The owner-level DOM tests separately cover the existing-work guard."""
    mock_page.evaluate.side_effect = None
    mock_page.evaluate.return_value = {"blocked": False}


async def test_prepare_resolves_the_author_and_preserves_exact_text(mock_page):
    with patch.object(
        PersonReader, "get_my_profile", AsyncMock(return_value={"url": AUTHOR})
    ) as profile:
        facade = LinkedInExtractor(mock_page)
        preview = await facade.prepare_post(TEXT)
    assert preview["author_url"] == AUTHOR
    assert preview["text"] == TEXT
    assert preview["published"] is False
    assert preview["audience"] == "anyone"
    profile.assert_awaited_once_with(max_scrolls=0)
    mock_page.goto.assert_not_awaited()


async def test_invalid_text_is_refused_before_author_resolution(mock_page):
    with patch.object(PersonReader, "get_my_profile", AsyncMock()) as profile:
        facade = LinkedInExtractor(mock_page)
        with pytest.raises(ValueError, match="1–3000"):
            await facade.prepare_post(" \n")
    profile.assert_not_awaited()


async def test_failed_author_resolution_does_not_create_a_preview(mock_page):
    with (
        patch.object(
            PersonReader,
            "get_my_profile",
            AsyncMock(side_effect=RuntimeError("Synthetic error")),
        ),
        patch.object(PostWriter, "prepare") as prepare,
    ):
        facade = LinkedInExtractor(mock_page)
        with pytest.raises(RuntimeError, match="Synthetic error"):
            await facade.prepare_post(TEXT)
    prepare.assert_not_called()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_publish_delegates_exact_approval(mock_page, confirmed):
    expected = {"status": "confirmation_required", "published": False}
    with patch.object(
        PostWriter, "publish", AsyncMock(return_value=expected)
    ) as publish:
        facade = LinkedInExtractor(mock_page)
        assert (
            await facade.publish_post("synthetic-token", TEXT, confirmed=confirmed)
            is expected
        )
    publish.assert_awaited_once_with("synthetic-token", TEXT, confirmed=confirmed)


async def test_fresh_facades_share_a_preview_for_the_same_page(mock_page):
    with patch.object(
        PersonReader, "get_my_profile", AsyncMock(return_value={"url": AUTHOR})
    ):
        preview = await LinkedInExtractor(mock_page).prepare_post(TEXT)
    other = LinkedInExtractor(mock_page)
    assert await other.publish_post(preview["draft_id"], TEXT) == {
        "status": "confirmation_required",
        "published": False,
    }
    mock_page.goto.assert_not_awaited()
