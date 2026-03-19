"""
OAuth 2.1 provider with password-based login for remote MCP deployments.

Subclasses FastMCP's InMemoryOAuthProvider to add a login page in the
authorization flow. All other OAuth infrastructure (DCR, PKCE, token
management, .well-known endpoints) is handled by the parent class.
"""

import html
import secrets
import time

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.providers.in_memory import (
    AuthorizationCode,
    InMemoryOAuthProvider,
    construct_redirect_uri,
)

# Pending auth requests expire after 10 minutes
_PENDING_REQUEST_TTL_SECONDS = 600

# Max failed password attempts before the request is invalidated
_MAX_FAILED_ATTEMPTS = 5


class PasswordOAuthProvider(InMemoryOAuthProvider):
    """OAuth provider that requires a password before issuing authorization codes.

    When a client (e.g. claude.ai) hits /authorize, the user is redirected to
    a login page. After entering the correct password, the authorization code
    is issued and the user is redirected back to the client's callback URL.
    """

    def __init__(
        self,
        *,
        base_url: str,
        password: str,
        **kwargs,
    ):
        from mcp.server.auth.settings import ClientRegistrationOptions

        super().__init__(
            base_url=base_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            **kwargs,
        )
        self._password = password
        self._pending_auth_requests: dict[str, dict] = {}

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect to login page instead of auto-approving."""
        self._cleanup_expired_requests()

        request_id = secrets.token_urlsafe(32)
        self._pending_auth_requests[request_id] = {
            "client_id": client.client_id,
            "params": params,
            "created_at": time.time(),
        }

        base = str(self.base_url).rstrip("/")
        return f"{base}/login?request_id={request_id}"

    def get_login_routes(self) -> list[Route]:
        """Return Starlette routes for the login page."""
        return [
            Route("/login", endpoint=self._handle_login, methods=["GET", "POST"]),
        ]

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """Extend parent routes with login page."""
        routes = super().get_routes(mcp_path)
        routes.extend(self.get_login_routes())
        return routes

    async def _handle_login(self, request: Request) -> Response:
        if request.method == "GET":
            return await self._render_login(request)
        return await self._process_login(request)

    async def _render_login(self, request: Request) -> Response:
        request_id = request.query_params.get("request_id", "")
        if not request_id or request_id not in self._pending_auth_requests:
            return HTMLResponse("Invalid or expired login request.", status_code=400)

        return HTMLResponse(self._login_html(request_id))

    async def _process_login(self, request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        password = str(form.get("password", ""))

        pending = self._pending_auth_requests.get(request_id)
        if not pending:
            return HTMLResponse("Invalid or expired login request.", status_code=400)

        if not secrets.compare_digest(password, self._password):
            pending["failed_attempts"] = pending.get("failed_attempts", 0) + 1
            if pending["failed_attempts"] >= _MAX_FAILED_ATTEMPTS:
                del self._pending_auth_requests[request_id]
                return HTMLResponse(
                    "Too many failed attempts. Please restart the authorization flow.",
                    status_code=403,
                )
            remaining = _MAX_FAILED_ATTEMPTS - pending["failed_attempts"]
            return HTMLResponse(
                self._login_html(
                    request_id,
                    error=f"Invalid password. {remaining} attempt(s) remaining.",
                ),
                status_code=200,
            )

        # Password correct — create the authorization code and redirect
        del self._pending_auth_requests[request_id]

        client = await self.get_client(pending["client_id"])
        if not client:
            return HTMLResponse("Client not found.", status_code=400)

        params: AuthorizationParams = pending["params"]
        scopes_list = params.scopes if params.scopes is not None else []

        auth_code_value = f"auth_code_{secrets.token_hex(16)}"
        expires_at = time.time() + 300  # 5 min

        auth_code = AuthorizationCode(
            code=auth_code_value,
            client_id=pending["client_id"],
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=scopes_list,
            expires_at=expires_at,
            code_challenge=params.code_challenge,
        )
        self.auth_codes[auth_code_value] = auth_code

        redirect_url = construct_redirect_uri(
            str(params.redirect_uri), code=auth_code_value, state=params.state
        )
        return RedirectResponse(redirect_url, status_code=302)

    def _cleanup_expired_requests(self) -> None:
        now = time.time()
        expired = [
            rid
            for rid, data in self._pending_auth_requests.items()
            if now - data["created_at"] > _PENDING_REQUEST_TTL_SECONDS
        ]
        for rid in expired:
            del self._pending_auth_requests[rid]

    @staticmethod
    def _login_html(request_id: str, error: str = "") -> str:
        error_html = (
            f'<p style="color:#dc2626">{html.escape(error)}</p>' if error else ""
        )
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LinkedIn MCP Server — Login</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
  .card {{ background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.1);
           max-width: 400px; width: 100%; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 1.5rem; }}
  input[type=password] {{ width: 100%; padding: .5rem; margin: .5rem 0 1rem; box-sizing: border-box;
                          border: 1px solid #ccc; border-radius: 4px; font-size: 1rem; }}
  button {{ width: 100%; padding: .6rem; background: #0a66c2; color: white; border: none;
            border-radius: 4px; font-size: 1rem; cursor: pointer; }}
  button:hover {{ background: #004182; }}
</style>
</head>
<body>
<div class="card">
  <h1>LinkedIn MCP Server</h1>
  <p>Enter the server password to authorize this connection.</p>
  {error_html}
  <form method="POST" action="/login">
    <input type="hidden" name="request_id" value="{html.escape(request_id)}">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required autofocus>
    <button type="submit">Authorize</button>
  </form>
</div>
</body>
</html>"""
