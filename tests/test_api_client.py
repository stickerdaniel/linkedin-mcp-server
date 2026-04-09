"""Unit tests for linkedin_mcp_server.api.client."""

import time
from unittest.mock import MagicMock, patch

import pytest

from linkedin_mcp_server.api.client import (
    LINKEDIN_VERSION,
    LinkedInApiClient,
    LinkedInApiError,
)
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


def _mock_resp(status_code: int = 200, json_body=None, headers=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    resp.json.return_value = json_body or {}
    resp.headers = headers or {}
    resp.text = ""
    return resp


@pytest.fixture(autouse=True)
def reset_api_client():
    """Ensure the module-level singleton is cleared between tests."""
    import linkedin_mcp_server.api.client as client_mod

    client_mod._client = None
    yield
    client_mod._client = None


class TestLinkedInApiError:
    def test_message_format(self):
        err = LinkedInApiError(404, "not found")
        assert "404" in str(err)
        assert "not found" in str(err)

    def test_status_code_attribute(self):
        err = LinkedInApiError(403, "forbidden")
        assert err.status_code == 403


class TestLinkedInApiClientHeaders:
    def test_headers_contain_correct_fields(self):
        client = LinkedInApiClient()
        tokens = _valid_tokens()
        headers = client._headers(tokens)
        assert headers["Authorization"] == f"Bearer {tokens.access_token}"
        assert headers["LinkedIn-Version"] == LINKEDIN_VERSION
        assert headers["X-Restli-Protocol-Version"] == "2.0.0"
        assert headers["Content-Type"] == "application/json"


class TestLinkedInApiClientResolveTokens:
    def test_raises_when_no_tokens(self):
        client = LinkedInApiClient()
        with patch("linkedin_mcp_server.api.client.load_tokens", return_value=None):
            with pytest.raises(LinkedInApiError) as exc_info:
                client._resolve_tokens()
            assert exc_info.value.status_code == 401

    def test_uses_cached_unexpired_tokens(self):
        tokens = _valid_tokens()
        client = LinkedInApiClient()
        client._tokens = tokens
        with patch("linkedin_mcp_server.api.client.is_expired", return_value=False):
            result = client._resolve_tokens()
        assert result is tokens

    def test_refreshes_expired_tokens(self):
        expired = _valid_tokens(expires_at=time.time() - 100)
        refreshed = _valid_tokens(access_token="new-access")
        client = LinkedInApiClient(client_id="cid", client_secret="csecret")
        client._tokens = expired

        with (
            patch("linkedin_mcp_server.api.client.is_expired", return_value=True),
            patch(
                "linkedin_mcp_server.api.client.refresh_access_token",
                return_value=refreshed,
            ),
        ):
            result = client._resolve_tokens()

        assert result.access_token == "new-access"
        assert client._tokens is refreshed

    def test_refresh_raises_when_no_credentials(self):
        expired = _valid_tokens(expires_at=time.time() - 100)
        client = LinkedInApiClient()  # no client_id / client_secret
        client._tokens = expired
        with patch("linkedin_mcp_server.api.client.is_expired", return_value=True):
            with pytest.raises(LinkedInApiError) as exc_info:
                client._resolve_tokens()
            assert exc_info.value.status_code == 401


class TestLinkedInApiClientPost:
    def test_successful_post(self):
        client = LinkedInApiClient()
        tokens = _valid_tokens()
        client._tokens = tokens
        resp = _mock_resp(201)

        with (
            patch("linkedin_mcp_server.api.client.is_expired", return_value=False),
            patch(
                "linkedin_mcp_server.api.client.httpx.post", return_value=resp
            ) as mock_post,
        ):
            result = client.post("/v2/ugcPosts", {"key": "value"})

        assert result is resp
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == "https://api.linkedin.com/v2/ugcPosts"
        assert call_kwargs[1]["json"] == {"key": "value"}

    def test_post_raises_on_error_response(self):
        client = LinkedInApiClient()
        tokens = _valid_tokens()
        client._tokens = tokens
        resp = _mock_resp(403)

        with (
            patch("linkedin_mcp_server.api.client.is_expired", return_value=False),
            patch("linkedin_mcp_server.api.client.httpx.post", return_value=resp),
        ):
            with pytest.raises(LinkedInApiError) as exc_info:
                client.post("/v2/ugcPosts", {})
            assert exc_info.value.status_code == 403


class TestLinkedInApiClientDelete:
    def test_successful_delete(self):
        client = LinkedInApiClient()
        client._tokens = _valid_tokens()
        resp = _mock_resp(204)

        with (
            patch("linkedin_mcp_server.api.client.is_expired", return_value=False),
            patch(
                "linkedin_mcp_server.api.client.httpx.delete", return_value=resp
            ) as mock_del,
        ):
            result = client.delete("/v2/ugcPosts/urn%3Ali%3AugcPost%3A123")

        assert result is resp
        mock_del.assert_called_once()

    def test_delete_raises_on_error_response(self):
        client = LinkedInApiClient()
        client._tokens = _valid_tokens()
        resp = _mock_resp(404)

        with (
            patch("linkedin_mcp_server.api.client.is_expired", return_value=False),
            patch("linkedin_mcp_server.api.client.httpx.delete", return_value=resp),
        ):
            with pytest.raises(LinkedInApiError) as exc_info:
                client.delete("/v2/ugcPosts/missing")
            assert exc_info.value.status_code == 404


class TestLinkedInApiClientPersonId:
    def test_returns_person_id(self):
        client = LinkedInApiClient()
        client._tokens = _valid_tokens(person_id="urn:li:person:ABC")
        with patch("linkedin_mcp_server.api.client.is_expired", return_value=False):
            assert client.person_id() == "urn:li:person:ABC"

    def test_raises_when_person_id_missing(self):
        client = LinkedInApiClient()
        client._tokens = _valid_tokens(person_id=None)
        with patch("linkedin_mcp_server.api.client.is_expired", return_value=False):
            with pytest.raises(LinkedInApiError) as exc_info:
                client.person_id()
            assert exc_info.value.status_code == 401
