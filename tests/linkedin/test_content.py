"""Tests for the raw page content reader."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from linkedin_mcp_server.core.exceptions import OffLinkedInLandingError
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.session import PageSession


def _reader(page) -> PageContentReader:
    return PageContentReader(PageSession(page))


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
    """The overlay read names two accepted roots in priority order.

    A read that hard-codes ``main`` would still answer every profile-page
    caller, and only the contact-info overlay would come back as page chrome.
    """
    mock_page.evaluate = AsyncMock(
        return_value={"source": "root", "text": "Contact", "references": []}
    )
    reader = _reader(mock_page)

    await reader._extract_root_content(["dialog[open]", ".artdeco-modal__content"])

    await_args = mock_page.evaluate.await_args
    assert await_args is not None
    assert await_args.args[1] == {
        "selectors": ["dialog[open]", ".artdeco-modal__content"]
    }


async def test_the_read_judges_the_document_it_read_not_the_driver(mock_page):
    """The driver still names LinkedIn; the document that was read does not."""
    mock_page.url = "https://www.linkedin.com/in/testuser/"
    mock_page.evaluate = AsyncMock(
        return_value={
            "source": "root",
            "text": "OFFLINE INTERSTITIAL, NOT A PROFILE",
            "references": [],
            "url": "https://portal.invalid/interstitial",
        }
    )

    with pytest.raises(OffLinkedInLandingError, match="https://portal.invalid"):
        await _reader(mock_page)._extract_root_content(["main"])


async def test_a_linkedin_document_is_returned_in_the_existing_shape(mock_page):
    mock_page.evaluate = AsyncMock(
        return_value={
            "source": "root",
            "text": "Sample profile text",
            "references": [],
            "url": "https://de.linkedin.com/in/testuser/",
        }
    )

    result = await _reader(mock_page)._extract_root_content(["main"])

    assert result == {"source": "root", "text": "Sample profile text", "references": []}
