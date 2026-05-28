"""MCP transport authentication helpers."""

from __future__ import annotations

import secrets

from fastmcp.server.auth import AccessToken, AuthProvider


class StaticBearerAuthProvider(AuthProvider):
    """Validate one static bearer token for private HTTP MCP deployments."""

    def __init__(self, token: str) -> None:
        super().__init__(required_scopes=["mcp:access"])
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="static-bearer-client",
            scopes=["mcp:access"],
        )
