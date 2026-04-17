"""Thin LinkedIn REST API client with automatic token refresh."""

import logging
from typing import Any

import httpx

from .tokens import TokenData, is_expired, load_tokens, refresh_access_token

logger = logging.getLogger(__name__)

_API_BASE = "https://api.linkedin.com"
# LinkedIn versioned API date — LinkedIn supports each version for ~1 year from release.
# Check https://learn.microsoft.com/en-us/linkedin/marketing/versioning for sunset dates.
# Last updated: 202603 (March 2026)
LINKEDIN_VERSION = "202603"


class LinkedInApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"LinkedIn API {status_code}: {message}")


class LinkedInApiClient:
    """Stateless-ish client that resolves and refreshes tokens on each call."""

    def __init__(
        self, client_id: str | None = None, client_secret: str | None = None
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens: TokenData | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_tokens(self) -> TokenData:
        tokens = self._tokens or load_tokens()
        if tokens is None:
            raise LinkedInApiError(
                401,
                "No LinkedIn API tokens found — run `--linkedin-auth` to authenticate",
            )
        if is_expired(tokens):
            if not self._client_id or not self._client_secret:
                raise LinkedInApiError(
                    401,
                    "Access token expired and LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET "
                    "are not set — cannot refresh automatically",
                )
            tokens = refresh_access_token(tokens, self._client_id, self._client_secret)
        self._tokens = tokens
        return tokens

    def _headers(self, tokens: TokenData) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {tokens.access_token}",
            "LinkedIn-Version": LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.is_error:
            try:
                msg = resp.json().get("message") or resp.text
            except Exception:
                msg = resp.text
            raise LinkedInApiError(resp.status_code, msg)

    # ------------------------------------------------------------------
    # HTTP verbs
    # ------------------------------------------------------------------

    def post(self, path: str, body: Any = None) -> httpx.Response:
        tokens = self._resolve_tokens()
        resp = httpx.post(
            f"{_API_BASE}{path}", headers=self._headers(tokens), json=body, timeout=30.0
        )
        self._check(resp)
        return resp

    def delete(self, path: str) -> httpx.Response:
        tokens = self._resolve_tokens()
        resp = httpx.delete(
            f"{_API_BASE}{path}", headers=self._headers(tokens), timeout=30.0
        )
        self._check(resp)
        return resp

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def person_id(self) -> str:
        tokens = self._resolve_tokens()
        if not tokens.person_id:
            raise LinkedInApiError(
                401,
                "Person ID not stored — run `--linkedin-auth` to re-authenticate",
            )
        return tokens.person_id


# ---------------------------------------------------------------------------
# Module-level singleton — created lazily so config is loaded first
# ---------------------------------------------------------------------------

_client: LinkedInApiClient | None = None


def get_api_client() -> LinkedInApiClient:
    """Return the shared API client, initialising it from config on first call."""
    global _client
    if _client is None:
        from linkedin_mcp_server.config import get_config

        cfg = get_config().linkedin_api
        _client = LinkedInApiClient(
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
        )
    return _client
