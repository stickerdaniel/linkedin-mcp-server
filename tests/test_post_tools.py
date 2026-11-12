"""MCP registration and validation contracts for reading posts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.tools.post import register_post_tools


POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:1234567890123/"


async def test_the_server_registers_post_reads_with_the_call_timeout():
    server = create_mcp_server(tool_timeout=17)
    tools = {tool.name: tool for tool in await server.list_tools()}

    for name in [
        "get_post",
        "get_my_posts",
        "get_post_analytics",
        "get_scheduled_posts",
    ]:
        assert name in tools
        assert tools[name].timeout == 17
    for name in [
        "get_post",
        "get_my_posts",
        "get_post_analytics",
        "get_scheduled_posts",
    ]:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("existing_dialog", ErrorCode.BROWSER_BUSY),
        ("unsupported_locale", ErrorCode.PAGE_UNREADABLE),
    ],
)
async def test_schedule_refusals_remain_actionable_when_error_details_are_masked(
    monkeypatch, reason, code
):
    from linkedin_mcp_server.linkedin.post_management import PostQueueReadRefused

    server = FastMCP("post-tests", mask_error_details=True)
    register_post_tools(server)
    extractor = SimpleNamespace(
        get_scheduled_posts=AsyncMock(side_effect=PostQueueReadRefused(reason))
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.post.get_ready_extractor",
        AsyncMock(return_value=extractor),
    )
    with pytest.raises(CodedToolError, match=reason) as error:
        await server.call_tool("get_scheduled_posts", {})
    assert error.value.code is code


@pytest.mark.parametrize("max_scrolls", [0, 50])
async def test_post_read_normalizes_the_permalink_and_passes_scroll_boundaries(
    monkeypatch, max_scrolls
):
    server = FastMCP("post-tests")
    register_post_tools(server)
    expected = {"url": POST_URL, "sections": {"post": "Synthetic post text"}}
    extractor = SimpleNamespace(get_post=AsyncMock(return_value=expected))
    ready = AsyncMock(return_value=extractor)
    monkeypatch.setattr("linkedin_mcp_server.tools.post.get_ready_extractor", ready)

    result = await server.call_tool(
        "get_post",
        {"post_url": "urn:li:activity:1234567890123", "max_scrolls": max_scrolls},
    )

    assert result.structured_content == expected
    readiness_call = ready.await_args
    assert readiness_call is not None
    assert readiness_call.kwargs == {"tool_name": "get_post"}
    extractor.get_post.assert_awaited_once_with(POST_URL, max_scrolls=max_scrolls)


async def test_invalid_post_reference_is_an_argument_error_before_readiness(
    monkeypatch,
):
    server = FastMCP("post-tests")
    register_post_tools(server)
    extractor = SimpleNamespace(
        get_post=AsyncMock(return_value={"url": POST_URL, "sections": {}})
    )
    ready = AsyncMock(return_value=extractor)
    monkeypatch.setattr("linkedin_mcp_server.tools.post.get_ready_extractor", ready)

    with pytest.raises(CodedToolError) as error:
        await server.call_tool("get_post", {"post_url": "https://example.com/post"})

    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    ready.assert_not_awaited()


@pytest.mark.parametrize("max_scrolls", [-1, 51])
async def test_the_tool_refuses_scroll_budgets_outside_its_schema(
    monkeypatch, max_scrolls
):
    from fastmcp.exceptions import ValidationError

    server = FastMCP("post-tests")
    register_post_tools(server)
    ready = AsyncMock()
    monkeypatch.setattr("linkedin_mcp_server.tools.post.get_ready_extractor", ready)

    with pytest.raises(ValidationError):
        await server.call_tool(
            "get_post", {"post_url": POST_URL, "max_scrolls": max_scrolls}
        )

    ready.assert_not_awaited()


@pytest.mark.parametrize("kind", ["share", "ugcPost"])
async def test_analytics_refuses_unsupported_entity_types_before_readiness(
    monkeypatch, kind
):
    server = FastMCP("post-tests")
    register_post_tools(server)
    ready = AsyncMock()
    monkeypatch.setattr("linkedin_mcp_server.tools.post.get_ready_extractor", ready)

    with pytest.raises(CodedToolError) as error:
        await server.call_tool(
            "get_post_analytics", {"post_url": f"urn:li:{kind}:1234567890123"}
        )

    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    ready.assert_not_awaited()
