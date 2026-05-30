"""Minimal OAuth provider for MCP HTTP auth."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastmcp.server.auth import AccessToken, AuthProvider
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

# Defaults: 24h access, 7d refresh (override via MCP_OAUTH_*_TTL_SECONDS).
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 86400
DEFAULT_REFRESH_TOKEN_TTL_SECONDS = 604800


@dataclass
class _TokenState:
    client_id: str
    expires_at: int
    scopes: list[str]


@dataclass
class _RefreshTokenState:
    client_id: str
    expires_at: int
    scopes: list[str]


@dataclass
class _AuthorizationCodeState:
    client_id: str
    redirect_uri: str
    expires_at: int
    code_challenge: str
    code_challenge_method: str
    scopes: list[str]


class MinimalOAuthProvider(AuthProvider):
    """OAuth provider with authorization_code + PKCE, refresh_token, and client_credentials."""

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        allowed_redirect_uris: list[str],
        token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
        refresh_token_ttl_seconds: int = DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
        auth_code_ttl_seconds: int = 120,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(base_url=base_url, required_scopes=required_scopes)
        self._client_id = client_id
        self._client_secret = client_secret
        self._allowed_redirect_uris = set(allowed_redirect_uris)
        self._token_ttl_seconds = token_ttl_seconds
        self._refresh_token_ttl_seconds = refresh_token_ttl_seconds
        self._auth_code_ttl_seconds = auth_code_ttl_seconds
        self._token_store: dict[str, _TokenState] = {}
        self._refresh_token_store: dict[str, _RefreshTokenState] = {}
        self._auth_code_store: dict[str, _AuthorizationCodeState] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        state = self._token_store.get(token)
        if state is None:
            return None
        if state.expires_at <= int(time.time()):
            self._token_store.pop(token, None)
            return None
        return AccessToken(
            token=token,
            client_id=state.client_id,
            scopes=state.scopes,
            expires_at=state.expires_at,
            claims={"sub": state.client_id},
        )

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        self.set_mcp_path(mcp_path)
        return [
            Route("/authorize", endpoint=self._authorize_endpoint, methods=["GET"]),
            Route("/token", endpoint=self._token_endpoint, methods=["POST", "OPTIONS"]),
            Route(
                "/.well-known/oauth-authorization-server",
                endpoint=self._oauth_metadata,
                methods=["GET"],
            ),
        ]

    async def _authorize_endpoint(self, request: Request) -> RedirectResponse:
        params = request.query_params
        response_type = params.get("response_type", "")
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")
        scope_raw = params.get("scope", "")

        if response_type != "code" or not self._is_valid_client_id(client_id):
            return self._redirect_error(
                redirect_uri=redirect_uri,
                state=state,
                error="invalid_request",
            )
        if not self._is_allowed_redirect_uri(redirect_uri):
            return self._redirect_error(
                redirect_uri=redirect_uri,
                state=state,
                error="invalid_request",
            )
        if not code_challenge or code_challenge_method != "S256":
            return self._redirect_error(
                redirect_uri=redirect_uri,
                state=state,
                error="invalid_request",
            )

        code = secrets.token_urlsafe(32)
        scopes = [s for s in scope_raw.split() if s] if scope_raw else []
        self._auth_code_store[code] = _AuthorizationCodeState(
            client_id=client_id,
            redirect_uri=redirect_uri,
            expires_at=int(time.time()) + self._auth_code_ttl_seconds,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scopes=scopes,
        )
        payload: dict[str, str] = {"code": code}
        if state:
            payload["state"] = state
        return RedirectResponse(
            url=f"{redirect_uri}?{urlencode(payload)}", status_code=302
        )

    async def _token_endpoint(self, request: Request) -> JSONResponse:
        client_id, client_secret = await self._extract_client_credentials(request)
        if not self._is_valid_client(client_id, client_secret):
            return JSONResponse({"error": "invalid_client"}, status_code=401)

        form = await request.form()
        grant_type = str(form.get("grant_type") or "")
        if grant_type == "client_credentials":
            return self._issue_access_token(
                client_id=client_id, scope_raw=form.get("scope"), include_refresh=False
            )
        if grant_type == "authorization_code":
            return self._exchange_authorization_code(client_id=client_id, form=form)
        if grant_type == "refresh_token":
            return self._exchange_refresh_token(
                client_id=client_id,
                refresh_token=str(form.get("refresh_token") or ""),
            )
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    def _exchange_authorization_code(
        self,
        *,
        client_id: str,
        form: Any,
    ) -> JSONResponse:
        code = str(form.get("code") or "")
        redirect_uri = str(form.get("redirect_uri") or "")
        code_verifier = str(form.get("code_verifier") or "")
        if not code or not redirect_uri or not code_verifier:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        state = self._auth_code_store.get(code)
        if state is None or state.expires_at <= int(time.time()):
            self._auth_code_store.pop(code, None)
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if state.client_id != client_id or state.redirect_uri != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not self._verify_pkce(verifier=code_verifier, state=state):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        self._auth_code_store.pop(code, None)
        return self._issue_access_token(
            client_id=client_id,
            scope_raw=" ".join(state.scopes),
            include_refresh=True,
        )

    def _exchange_refresh_token(
        self,
        *,
        client_id: str,
        refresh_token: str,
    ) -> JSONResponse:
        if not refresh_token:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

        state = self._refresh_token_store.get(refresh_token)
        if state is None or state.expires_at <= int(time.time()):
            self._refresh_token_store.pop(refresh_token, None)
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if state.client_id != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Rotate refresh token: old refresh is single-use.
        self._refresh_token_store.pop(refresh_token, None)
        return self._issue_access_token(
            client_id=client_id,
            scope_raw=" ".join(state.scopes),
            include_refresh=True,
        )

    def _issue_access_token(
        self,
        *,
        client_id: str,
        scope_raw: Any,
        include_refresh: bool,
    ) -> JSONResponse:
        scope_text = str(scope_raw or "").strip()
        scopes = [s for s in scope_text.split() if s] if scope_text else []
        now = int(time.time())
        access_expires_at = now + self._token_ttl_seconds
        access_token = secrets.token_urlsafe(48)
        self._token_store[access_token] = _TokenState(
            client_id=client_id,
            expires_at=access_expires_at,
            scopes=scopes,
        )

        payload: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": self._token_ttl_seconds,
            "scope": " ".join(scopes),
        }

        if include_refresh:
            refresh_expires_at = now + self._refresh_token_ttl_seconds
            refresh_token = secrets.token_urlsafe(48)
            self._refresh_token_store[refresh_token] = _RefreshTokenState(
                client_id=client_id,
                expires_at=refresh_expires_at,
                scopes=scopes,
            )
            payload["refresh_token"] = refresh_token

        return JSONResponse(
            payload,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def _oauth_metadata(self, request: Request) -> JSONResponse:
        del request
        base = str(self.base_url).rstrip("/")  # type: ignore[union-attr]
        metadata: dict[str, Any] = {
            "issuer": f"{base}/",
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": [
                "authorization_code",
                "client_credentials",
                "refresh_token",
            ],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.required_scopes:
            metadata["scopes_supported"] = self.required_scopes
        return JSONResponse(metadata)

    async def _extract_client_credentials(self, request: Request) -> tuple[str, str]:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            encoded = auth_header.split(" ", 1)[1].strip()
            try:
                decoded = base64.b64decode(encoded).decode("utf-8")
                client_id, client_secret = decoded.split(":", 1)
                return client_id, client_secret
            except Exception:
                return "", ""

        form = await request.form()
        return str(form.get("client_id") or ""), str(form.get("client_secret") or "")

    def _is_valid_client_id(self, client_id: str) -> bool:
        return hmac.compare_digest(client_id, self._client_id)

    def _is_valid_client(self, client_id: str, client_secret: str) -> bool:
        return self._is_valid_client_id(client_id) and hmac.compare_digest(
            client_secret,
            self._client_secret,
        )

    def _is_allowed_redirect_uri(self, redirect_uri: str) -> bool:
        return redirect_uri in self._allowed_redirect_uris

    def _verify_pkce(self, *, verifier: str, state: _AuthorizationCodeState) -> bool:
        if state.code_challenge_method != "S256":
            return False
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return hmac.compare_digest(expected, state.code_challenge)

    def _redirect_error(
        self,
        *,
        redirect_uri: str,
        state: str,
        error: str,
    ) -> RedirectResponse:
        if not self._is_allowed_redirect_uri(redirect_uri):
            return RedirectResponse(url="/", status_code=302)
        payload = {"error": error}
        if state:
            payload["state"] = state
        return RedirectResponse(
            url=f"{redirect_uri}?{urlencode(payload)}",
            status_code=302,
        )
