import json
import time

from starlette.requests import Request
from urllib.parse import parse_qs, urlparse

from linkedin_mcp_server.oauth_auth import (
    DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
    MinimalOAuthProvider,
)

PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
ENCODED_REDIRECT_URI = "https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"


def _request(
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> Request:
    async def _receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": headers or [],
        },
        receive=_receive,
    )


def _token_body(**fields: str) -> bytes:
    return "&".join(f"{key}={value}" for key, value in fields.items()).encode()


def _parse_token_response(response) -> dict:
    return json.loads(bytes(response.body))


def _provider(
    *,
    token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
    refresh_token_ttl_seconds: int = 604800,
) -> MinimalOAuthProvider:
    return MinimalOAuthProvider(
        base_url="https://example.com",
        client_id="cid",
        client_secret="secret",
        allowed_redirect_uris=[REDIRECT_URI],
        token_ttl_seconds=token_ttl_seconds,
        refresh_token_ttl_seconds=refresh_token_ttl_seconds,
    )


async def _authorization_code_token_pair(provider: MinimalOAuthProvider) -> dict:
    authorize_req = _request(method="GET", path="/authorize")
    authorize_req.scope["query_string"] = (
        "response_type=code&client_id=cid"
        f"&redirect_uri={ENCODED_REDIRECT_URI}"
        "&state=s123"
        f"&code_challenge={PKCE_CHALLENGE}"
        "&code_challenge_method=S256"
    ).encode()

    authorize_resp = await provider._authorize_endpoint(authorize_req)
    assert authorize_resp.status_code == 302
    code = parse_qs(urlparse(authorize_resp.headers["location"]).query)["code"][0]

    token_req = _request(
        method="POST",
        path="/token",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
        body=_token_body(
            grant_type="authorization_code",
            client_id="cid",
            client_secret="secret",
            code=code,
            redirect_uri=REDIRECT_URI,
            code_verifier=PKCE_VERIFIER,
        ),
    )
    token_resp = await provider._token_endpoint(token_req)
    assert token_resp.status_code == 200
    return _parse_token_response(token_resp)


async def test_token_endpoint_issues_token():
    provider = _provider()
    req = _request(
        method="POST",
        path="/token",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
        body=_token_body(
            grant_type="client_credentials",
            client_id="cid",
            client_secret="secret",
        ),
    )
    resp = await provider._token_endpoint(req)
    assert resp.status_code == 200
    payload = _parse_token_response(resp)
    assert "access_token" in payload
    assert "refresh_token" not in payload


async def test_verify_token_rejects_invalid_token():
    provider = _provider()
    token = await provider.verify_token("invalid")
    assert token is None


async def test_authorization_code_pkce_flow():
    provider = _provider()
    payload = await _authorization_code_token_pair(provider)
    assert payload["token_type"] == "Bearer"
    assert payload["expires_in"] == DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    assert "refresh_token" in payload
    assert await provider.verify_token(payload["access_token"]) is not None


async def test_refresh_token_grant_issues_new_tokens():
    provider = _provider(
        token_ttl_seconds=3600,
        refresh_token_ttl_seconds=604800,
    )
    initial = await _authorization_code_token_pair(provider)
    refresh_resp = await provider._token_endpoint(
        _request(
            method="POST",
            path="/token",
            headers=[(b"content-type", b"application/x-www-form-urlencoded")],
            body=_token_body(
                grant_type="refresh_token",
                client_id="cid",
                client_secret="secret",
                refresh_token=initial["refresh_token"],
            ),
        )
    )
    assert refresh_resp.status_code == 200
    refreshed = _parse_token_response(refresh_resp)
    assert refreshed["access_token"] != initial["access_token"]
    assert refreshed["refresh_token"] != initial["refresh_token"]
    assert refreshed["expires_in"] == 3600
    assert await provider.verify_token(refreshed["access_token"]) is not None


async def test_refresh_token_rotation_rejects_reuse():
    provider = _provider()
    initial = await _authorization_code_token_pair(provider)
    body = _token_body(
        grant_type="refresh_token",
        client_id="cid",
        client_secret="secret",
        refresh_token=initial["refresh_token"],
    )
    first = await provider._token_endpoint(
        _request(
            method="POST",
            path="/token",
            headers=[(b"content-type", b"application/x-www-form-urlencoded")],
            body=body,
        )
    )
    assert first.status_code == 200

    second = await provider._token_endpoint(
        _request(
            method="POST",
            path="/token",
            headers=[(b"content-type", b"application/x-www-form-urlencoded")],
            body=body,
        )
    )
    assert second.status_code == 400
    assert _parse_token_response(second)["error"] == "invalid_grant"


async def test_expired_refresh_token_rejected():
    provider = _provider(refresh_token_ttl_seconds=1)
    initial = await _authorization_code_token_pair(provider)
    provider._refresh_token_store[initial["refresh_token"]].expires_at = (
        int(time.time()) - 1
    )

    resp = await provider._token_endpoint(
        _request(
            method="POST",
            path="/token",
            headers=[(b"content-type", b"application/x-www-form-urlencoded")],
            body=_token_body(
                grant_type="refresh_token",
                client_id="cid",
                client_secret="secret",
                refresh_token=initial["refresh_token"],
            ),
        )
    )
    assert resp.status_code == 400


async def test_oauth_metadata_advertises_refresh_token_grant():
    provider = _provider()
    resp = await provider._oauth_metadata(_request(method="GET", path="/.well-known"))
    metadata = json.loads(bytes(resp.body))
    assert "refresh_token" in metadata["grant_types_supported"]
