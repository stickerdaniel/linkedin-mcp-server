"""LinkedIn OAuth 2.0 authorization flow with local browser callback."""

import logging
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .tokens import TokenData, save_tokens

logger = logging.getLogger(__name__)

_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_SCOPES = "w_member_social openid profile"
_CALLBACK_PORT = 8397
_CALLBACK_TIMEOUT_SECONDS = 300  # 5 minutes for the user to log in


def run_oauth_flow(client_id: str, client_secret: str) -> TokenData:
    """
    Full OAuth 2.0 flow:
    1. Open the OS default browser to LinkedIn's consent page
    2. Capture the authorization code via a local callback server
    3. Exchange the code for access + refresh tokens
    4. Fetch the person ID and persist everything to ~/.linkedin-mcp/user-tokens.json
    """
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{_CALLBACK_PORT}/callback"

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": _SCOPES,
        "prompt": "consent",  # force fresh consent so scope changes take effect
    }
    auth_url = f"{_AUTH_URL}?{urlencode(params)}"

    # Clear any stale token so the new one is cleanly written
    from .tokens import clear_tokens

    clear_tokens()

    print("\nOpening LinkedIn authorization in your browser...")
    print(f"If it does not open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    code = _wait_for_callback(expected_state=state, port=_CALLBACK_PORT)
    print("Authorization code received. Exchanging for tokens...")

    resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    person_id = _fetch_person_id(data["access_token"])

    tokens = TokenData(
        access_token=data["access_token"],
        expires_at=time.time() + data["expires_in"],
        refresh_token=data.get("refresh_token"),
        person_id=person_id,
    )
    save_tokens(tokens)
    return tokens


_ME_URL = "https://api.linkedin.com/v2/me"


def _fetch_person_id(access_token: str) -> str | None:
    """
    Try to resolve the authenticated member's person URN.

    Tries /v2/me first (works with w_member_social on some apps),
    then /v2/userinfo (requires openid scope / Sign In with LinkedIn product).
    Returns None if both fail — caller will prompt the user.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    # Try /v2/me — available when the app has any member-scoped permission
    try:
        resp = httpx.get(_ME_URL, headers=headers)
        if resp.is_success:
            person_id = resp.json().get("id")
            if person_id:
                return f"urn:li:person:{person_id}"
    except Exception:
        logger.debug("Could not fetch person ID from /v2/me", exc_info=True)

    # Try /v2/userinfo — requires openid scope (Sign In with LinkedIn product)
    # Retry once: the endpoint can return 403 briefly after token exchange
    for _ in range(2):
        try:
            resp = httpx.get(_USERINFO_URL, headers=headers)
            if resp.is_success:
                sub = resp.json().get("sub")
                if sub:
                    raw_id = sub.split(":")[-1] if sub.startswith("urn:li:") else sub
                    return f"urn:li:person:{raw_id}"
            if resp.status_code != 403:
                break  # only retry on 403
        except Exception:
            logger.debug("Could not fetch person ID from /v2/userinfo", exc_info=True)
            break

    return None


def _wait_for_callback(expected_state: str, port: int) -> str:
    """
    Start a local HTTP server and loop until the OAuth callback arrives.

    Loops instead of handling a single request because browsers often fire
    extra requests (favicon, prefetch) before the actual /callback redirect.
    """
    received: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self._respond(404, b"Not found")
                return
            params = parse_qs(parsed.query)
            if params.get("state", [None])[0] != expected_state:
                self._respond(400, b"State mismatch - possible CSRF attempt")
                return
            if "error" in params:
                received["error"] = params["error"][0]
                self._respond(
                    200, b"<h2>Authorization failed. You can close this window.</h2>"
                )
            else:
                received["code"] = params.get("code", [""])[0]
                self._respond(
                    200, b"<h2>Authorization complete! You can close this window.</h2>"
                )

        def _respond(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # suppress access logs

    server = HTTPServer(("localhost", port), _Handler)
    server.timeout = _CALLBACK_TIMEOUT_SECONDS
    try:
        while not received:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in received:
        raise RuntimeError(f"LinkedIn OAuth error: {received['error']}")
    if not received.get("code"):
        raise RuntimeError(
            "No authorization code received — login timed out or was cancelled"
        )
    return received["code"]
