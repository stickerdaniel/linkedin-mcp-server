import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from linkedin_mcp_server.auth import PasswordOAuthProvider


@pytest.fixture
def provider():
    return PasswordOAuthProvider(
        base_url="http://localhost:8000",
        password="test-secret",
    )


class TestPasswordOAuthProvider:
    def test_init_stores_password(self, provider):
        assert provider._password == "test-secret"

    async def test_authorize_returns_login_url(self, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            client_name="Test",
            redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        provider.clients["test-client"] = client_info

        params = AuthorizationParams(
            state="test-state",
            scopes=[],
            code_challenge="test-challenge",
            redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            redirect_uri_provided_explicitly=True,
        )

        result = await provider.authorize(client_info, params)
        assert "/login?" in result
        assert "request_id=" in result

    async def test_authorize_stores_pending_request(self, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        client_info = OAuthClientInformationFull(
            client_id="test-client",
            client_name="Test",
            redirect_uris=[AnyUrl("https://example.com/callback")],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
        provider.clients["test-client"] = client_info

        params = AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge="c",
            redirect_uri=AnyUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
        )

        await provider.authorize(client_info, params)
        assert len(provider._pending_auth_requests) == 1


class TestLoginRoutes:
    @pytest.fixture
    def app(self, provider):
        routes = provider.get_login_routes()
        return Starlette(routes=routes)

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_get_login_renders_form(self, client, provider):
        provider._pending_auth_requests["req123"] = {
            "client_id": "test",
            "params": None,
            "created_at": time.time(),
        }

        response = client.get("/login?request_id=req123")
        assert response.status_code == 200
        assert "password" in response.text
        assert "req123" in response.text

    def test_get_login_invalid_request_id(self, client):
        response = client.get("/login?request_id=nonexistent")
        assert response.status_code == 400

    def test_get_login_missing_request_id(self, client):
        response = client.get("/login")
        assert response.status_code == 400

    def test_login_page_has_security_headers(self, client, provider):
        provider._pending_auth_requests["req-hdr"] = {
            "client_id": "test",
            "params": None,
            "created_at": time.time(),
        }
        response = client.get("/login?request_id=req-hdr")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_post_login_correct_password(self, client, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        params = AuthorizationParams(
            state="test-state",
            scopes=[],
            code_challenge="test-challenge",
            redirect_uri=AnyUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
        )
        provider._pending_auth_requests["req123"] = {
            "client_id": "test-client",
            "params": params,
            "created_at": time.time(),
        }
        provider.clients["test-client"] = OAuthClientInformationFull(
            client_id="test-client",
            client_name="Test",
            redirect_uris=[AnyUrl("https://example.com/callback")],
            grant_types=["authorization_code"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )

        response = client.post(
            "/login",
            data={"request_id": "req123", "password": "test-secret"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "code=" in response.headers["location"]
        assert "state=test-state" in response.headers["location"]
        # Pending request consumed
        assert "req123" not in provider._pending_auth_requests

    def test_post_login_wrong_password(self, client, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyUrl

        params = AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge="c",
            redirect_uri=AnyUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
        )
        provider._pending_auth_requests["req123"] = {
            "client_id": "test-client",
            "params": params,
            "created_at": time.time(),
        }

        response = client.post(
            "/login",
            data={"request_id": "req123", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "invalid" in response.text.lower()
        assert "4 attempt(s) remaining" in response.text
        # Pending request NOT consumed
        assert "req123" in provider._pending_auth_requests

    def test_post_login_expired_request_rejected(self, client, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyUrl

        params = AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge="c",
            redirect_uri=AnyUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
        )
        provider._pending_auth_requests["req-expired"] = {
            "client_id": "test-client",
            "params": params,
            "created_at": time.time() - 700,  # 11+ minutes ago
        }

        response = client.post(
            "/login",
            data={"request_id": "req-expired", "password": "test-secret"},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "expired" in response.text.lower()
        assert "req-expired" not in provider._pending_auth_requests

    def test_post_login_global_rate_limit(self, client, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyUrl

        params = AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge="c",
            redirect_uri=AnyUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
        )

        # Simulate 20 global failures (across different request_ids)
        provider._global_failed_attempts = [time.time()] * 19
        provider._pending_auth_requests["req-global"] = {
            "client_id": "test-client",
            "params": params,
            "created_at": time.time(),
        }

        # This 20th failure should trigger global lockout
        response = client.post(
            "/login",
            data={"request_id": "req-global", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 429
        assert "try again later" in response.text.lower()

        # Subsequent attempts also blocked even with new request_id
        provider._pending_auth_requests["req-blocked"] = {
            "client_id": "test-client",
            "params": params,
            "created_at": time.time(),
        }
        response = client.post(
            "/login",
            data={"request_id": "req-blocked", "password": "test-secret"},
            follow_redirects=False,
        )
        assert response.status_code == 429

    def test_post_login_lockout_after_max_attempts(self, client, provider):
        from mcp.server.auth.provider import AuthorizationParams
        from pydantic import AnyUrl

        params = AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge="c",
            redirect_uri=AnyUrl("https://example.com/callback"),
            redirect_uri_provided_explicitly=True,
        )
        provider._pending_auth_requests["req-lock"] = {
            "client_id": "test-client",
            "params": params,
            "created_at": time.time(),
        }

        # Exhaust all 5 attempts
        for i in range(5):
            response = client.post(
                "/login",
                data={"request_id": "req-lock", "password": "wrong"},
                follow_redirects=False,
            )

        assert response.status_code == 403
        assert "too many" in response.text.lower()
        # Request invalidated
        assert "req-lock" not in provider._pending_auth_requests


class TestOAuthIntegration:
    """Integration tests verifying OAuth through the HTTP layer."""

    @pytest.fixture
    def oauth_mcp(self, provider):
        from fastmcp import FastMCP

        mcp = FastMCP("test-oauth", auth=provider)

        @mcp.tool
        async def echo(message: str) -> dict:
            return {"echo": message}

        return mcp

    @pytest.fixture
    def http_client(self, oauth_mcp):
        app = oauth_mcp.http_app(transport="streamable-http")
        return TestClient(app)

    def test_unauthenticated_request_returns_401(self, http_client):
        response = http_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    def test_well_known_oauth_metadata_accessible(self, http_client):
        response = http_client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        data = response.json()
        assert "authorization_endpoint" in data
        assert "token_endpoint" in data
        assert "registration_endpoint" in data

    def test_login_page_accessible_without_auth(self, http_client, provider):
        """Login page should be reachable without a bearer token."""
        provider._pending_auth_requests["int-req"] = {
            "client_id": "test",
            "params": None,
            "created_at": time.time(),
        }
        response = http_client.get("/login?request_id=int-req")
        assert response.status_code == 200
        assert "password" in response.text
