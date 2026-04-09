"""
LinkedIn user OAuth token storage and refresh.

Stores the authenticated LinkedIn *user's* tokens — not the developer app credentials.

  App credentials  (Client ID / Secret)  → env vars LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET
  User OAuth tokens (access / refresh)   → ~/.linkedin-mcp/user-tokens.json  (this module)
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path("~/.linkedin-mcp/user-tokens.json")
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
# Refresh 5 minutes before actual expiry
_EXPIRY_BUFFER_SECONDS = 300


@dataclass
class TokenData:
    access_token: str
    expires_at: float  # Unix timestamp
    refresh_token: str | None = None
    person_id: str | None = (
        None  # e.g. urn:li:person:123456 (used by UGC Posts / Social Actions)
    )


def token_path() -> Path:
    return _TOKEN_FILE.expanduser()


def load_tokens() -> TokenData | None:
    path = token_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return TokenData(**data)
    except Exception:
        logger.debug("Failed to load API tokens", exc_info=True)
        return None


def save_tokens(tokens: TokenData) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(tokens), indent=2))
    path.chmod(0o600)


def clear_tokens() -> None:
    path = token_path()
    if path.exists():
        path.unlink()


def is_expired(tokens: TokenData) -> bool:
    return time.time() >= tokens.expires_at - _EXPIRY_BUFFER_SECONDS


def refresh_access_token(
    tokens: TokenData, client_id: str, client_secret: str
) -> TokenData:
    """Exchange the stored refresh token for a new access token."""
    if not tokens.refresh_token:
        raise ValueError(
            "No refresh token available — run `--linkedin-auth` to re-authenticate"
        )
    resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    refreshed = TokenData(
        access_token=data["access_token"],
        expires_at=time.time() + data["expires_in"],
        refresh_token=data.get("refresh_token", tokens.refresh_token),
        person_id=tokens.person_id,
    )
    save_tokens(refreshed)
    logger.debug("LinkedIn API access token refreshed")
    return refreshed
