"""Unit tests for linkedin_mcp_server.tools.posts."""

import time
from typing import Any, Callable, cast
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import FastMCP
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.api.client import LinkedInApiClient
from linkedin_mcp_server.api.tokens import TokenData


def _valid_tokens(
    access_token: str = "access-tok",
    expires_at: float | None = None,
    refresh_token: str | None = "refresh-tok",
    person_id: str | None = "urn:li:person:TestId",
) -> TokenData:
    return TokenData(
        access_token=access_token,
        expires_at=expires_at if expires_at is not None else time.time() + 3600,
        refresh_token=refresh_token,
        person_id=person_id,
    )


def _mock_resp(status_code: int = 201, json_body=None, headers=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    resp.json.return_value = json_body or {}
    resp.headers = headers or {}
    resp.text = ""
    return resp


async def _get_tool(mcp: FastMCP, name: str) -> Callable[..., Any]:
    tool = await mcp.get_tool(name)
    if tool is None:
        raise ValueError(f"Tool '{name}' not found in MCP server")
    return cast(FunctionTool, tool).fn


@pytest.fixture()
def api_client() -> LinkedInApiClient:
    """LinkedInApiClient with pre-loaded valid tokens (no network I/O)."""
    client = LinkedInApiClient()
    client._tokens = _valid_tokens()
    return client


@pytest.fixture(autouse=True)
def patch_is_expired():
    with patch("linkedin_mcp_server.api.client.is_expired", return_value=False):
        yield


@pytest.fixture(autouse=True)
def reset_api_client_singleton():
    import linkedin_mcp_server.api.client as client_mod

    client_mod._client = None
    yield
    client_mod._client = None


@pytest.fixture()
def mcp_server(api_client) -> FastMCP:
    from linkedin_mcp_server.tools.posts import register_post_tools

    mcp = FastMCP("test")
    with patch(
        "linkedin_mcp_server.tools.posts.get_api_client", return_value=api_client
    ):
        register_post_tools(mcp)
    return mcp


class TestCreatePost:
    async def test_text_post(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "create_post")
        resp = _mock_resp(201, headers={"x-restli-id": "urn:li:ugcPost:999"})

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp) as mock_post,
        ):
            result = fn(text="Hello LinkedIn!")

        assert result["post_urn"] == "urn:li:ugcPost:999"
        assert result["status"] == "published"
        body = mock_post.call_args[0][1]
        content = body["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert content["shareMediaCategory"] == "NONE"
        assert content["shareCommentary"]["text"] == "Hello LinkedIn!"
        assert (
            body["visibility"]["com.linkedin.ugc.MemberNetworkVisibility"] == "PUBLIC"
        )

    async def test_link_post(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "create_post")
        resp = _mock_resp(201, headers={"x-restli-id": "urn:li:ugcPost:888"})

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp) as mock_post,
        ):
            result = fn(text="Check this", url="https://example.com")

        assert result["post_urn"] == "urn:li:ugcPost:888"
        body = mock_post.call_args[0][1]
        content = body["specificContent"]["com.linkedin.ugc.ShareContent"]
        assert content["shareMediaCategory"] == "ARTICLE"
        assert content["media"][0]["originalUrl"] == "https://example.com"

    async def test_connections_visibility(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "create_post")
        resp = _mock_resp(201, headers={"x-restli-id": "urn:li:ugcPost:1"})

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp) as mock_post,
        ):
            fn(text="Private", visibility="CONNECTIONS")

        body = mock_post.call_args[0][1]
        assert (
            body["visibility"]["com.linkedin.ugc.MemberNetworkVisibility"]
            == "CONNECTIONS"
        )

    async def test_author_is_person_id(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "create_post")
        resp = _mock_resp(201, headers={"x-restli-id": "urn:li:ugcPost:1"})

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp) as mock_post,
        ):
            fn(text="Author test")

        body = mock_post.call_args[0][1]
        assert body["author"] == "urn:li:person:TestId"


class TestDeletePost:
    async def test_delete(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "delete_post")
        resp = _mock_resp(204)

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "delete", return_value=resp) as mock_del,
        ):
            result = fn(post_urn="urn:li:ugcPost:123")

        assert result["post_urn"] == "urn:li:ugcPost:123"
        assert result["status"] == "deleted"
        called_path = mock_del.call_args[0][0]
        assert "urn%3Ali%3AugcPost%3A123" in called_path


class TestCreateComment:
    async def test_creates_comment(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "create_comment")
        resp = _mock_resp(
            201, json_body={"$URN": "urn:li:comment:(urn:li:activity:123,456)"}
        )

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp) as mock_post,
        ):
            result = fn(post_urn="urn:li:ugcPost:123", text="Great post!")

        assert result["comment_urn"] == "urn:li:comment:(urn:li:activity:123,456)"
        assert result["post_urn"] == "urn:li:ugcPost:123"
        assert result["status"] == "created"
        body = mock_post.call_args[0][1]
        assert body["message"]["text"] == "Great post!"
        assert body["actor"] == "urn:li:person:TestId"

    async def test_falls_back_to_header_urn(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "create_comment")
        resp = _mock_resp(201, json_body={}, headers={"x-restli-id": "fallback-urn"})

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp),
        ):
            result = fn(post_urn="urn:li:ugcPost:123", text="Hi")

        assert result["comment_urn"] == "fallback-urn"


class TestReplyToComment:
    async def test_creates_reply(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "reply_to_comment")
        resp = _mock_resp(
            201, json_body={"$URN": "urn:li:comment:(urn:li:activity:123,789)"}
        )
        comment_urn = "urn:li:comment:(urn:li:activity:123,456)"

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "post", return_value=resp) as mock_post,
        ):
            result = fn(comment_urn=comment_urn, text="Thanks!")

        assert result["parent_comment_urn"] == comment_urn
        assert result["status"] == "created"
        called_path = mock_post.call_args[0][0]
        assert "urn%3Ali%3Acomment%3A" in called_path


class TestDeleteComment:
    async def test_deletes_comment(self, mcp_server, api_client):
        fn = await _get_tool(mcp_server, "delete_comment")
        resp = _mock_resp(204)

        with (
            patch(
                "linkedin_mcp_server.tools.posts.get_api_client",
                return_value=api_client,
            ),
            patch.object(api_client, "delete", return_value=resp) as mock_del,
        ):
            result = fn(post_urn="urn:li:ugcPost:123", comment_id="456")

        assert result["comment_id"] == "456"
        assert result["post_urn"] == "urn:li:ugcPost:123"
        assert result["status"] == "deleted"
        called_path = mock_del.call_args[0][0]
        assert "/comments/456" in called_path
        assert "actor=" in called_path
