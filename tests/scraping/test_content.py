"""Tests for the raw page content reader."""

from __future__ import annotations

from unittest.mock import AsyncMock

from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.session import ScrapingSession


def _reader(page) -> PageContentReader:
    return PageContentReader(ScrapingSession(page))


async def test_root_content_filters_empty_href_before_resolution(mock_page):
    mock_page.evaluate = AsyncMock(
        return_value={
            "source": "root",
            "text": "Sample profile text",
            "references": [],
        }
    )
    reader = _reader(mock_page)

    await reader._extract_root_content(["main"])

    await_args = mock_page.evaluate.await_args
    assert await_args is not None
    script = await_args.args[0]
    assert "MAX_HEADING_CONTAINERS = 300" in script
    assert "MAX_REFERENCE_ANCHORS = 500" in script
    assert "const getPreviousHeading = node =>" in script
    assert "index < 3" in script
    assert "if (!rawHref || rawHref === '#')" in script
    assert ".slice(0, MAX_REFERENCE_ANCHORS)" in script
    assert "in_list" not in script
    assert ".filter(Boolean);" in script


async def test_the_caller_selectors_reach_the_page_unchanged(mock_page):
    """The overlay read names three roots in priority order.

    A read that hard-codes ``main`` would still answer every profile-page
    caller, and only the contact-info overlay would come back as page chrome.
    """
    mock_page.evaluate = AsyncMock(
        return_value={"source": "root", "text": "Contact", "references": []}
    )
    reader = _reader(mock_page)

    await reader._extract_root_content(
        ["dialog[open]", ".artdeco-modal__content", "main"]
    )

    await_args = mock_page.evaluate.await_args
    assert await_args is not None
    assert await_args.args[1] == {
        "selectors": ["dialog[open]", ".artdeco-modal__content", "main"]
    }
