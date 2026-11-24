"""Post facade contracts, patched at the public workflow-owner boundary."""

from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server.linkedin import LinkedInExtractor
from linkedin_mcp_server.linkedin.post_management import PostManagement
from linkedin_mcp_server.linkedin.post_reader import PostReader


POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1234567890123/"


async def test_post_read_delegates_without_changing_the_result(mock_page):
    expected = {"url": POST_URL, "sections": {"post": "Unparsed post text"}}
    with patch.object(PostReader, "get_post", AsyncMock(return_value=expected)) as read:
        facade = LinkedInExtractor(mock_page)
        assert await facade.get_post(POST_URL, max_scrolls=8) is expected
    read.assert_awaited_once_with(POST_URL, max_scrolls=8)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("get_my_posts", {"max_scrolls": 9}),
        ("get_post_analytics", {"post_url": POST_URL}),
        ("get_scheduled_posts", {"max_scrolls": 6}),
    ],
)
async def test_management_reads_preserve_the_result(mock_page, method, arguments):
    expected = {"url": POST_URL, "sections": {"synthetic": "Unparsed page text"}}
    with patch.object(
        PostManagement, method, AsyncMock(return_value=expected)
    ) as operation:
        facade = LinkedInExtractor(mock_page)
        assert await getattr(facade, method)(**arguments) is expected
    if method == "get_post_analytics":
        operation.assert_awaited_once_with(POST_URL)
    else:
        operation.assert_awaited_once_with(**arguments)
