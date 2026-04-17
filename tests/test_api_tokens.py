"""Unit tests for linkedin_mcp_server.api.tokens."""

import time
from unittest.mock import MagicMock, patch

import pytest

from linkedin_mcp_server.api.tokens import (
    TokenData,
    clear_tokens,
    is_expired,
    load_tokens,
    refresh_access_token,
    save_tokens,
    token_path,
)

_EXPIRY_BUFFER = 300  # seconds — must match the module constant


@pytest.fixture(autouse=True)
def patch_token_path(tmp_path, monkeypatch):
    """Redirect token file to a temp location so tests never touch ~/.linkedin-mcp."""
    fake_path = tmp_path / "user-tokens.json"
    monkeypatch.setattr(
        "linkedin_mcp_server.api.tokens._TOKEN_FILE",
        fake_path,
    )
    return fake_path


def _sample_tokens(
    access_token: str = "test-access-token",
    expires_at: float | None = None,
    refresh_token: str | None = "test-refresh-token",
    person_id: str | None = "urn:li:person:TestPerson",
) -> TokenData:
    return TokenData(
        access_token=access_token,
        expires_at=expires_at if expires_at is not None else time.time() + 3600,
        refresh_token=refresh_token,
        person_id=person_id,
    )


class TestTokenPath:
    def test_returns_path(self):
        assert token_path().name == "user-tokens.json"


class TestSaveAndLoadTokens:
    def test_roundtrip(self):
        tokens = _sample_tokens()
        save_tokens(tokens)
        loaded = load_tokens()
        assert loaded is not None
        assert loaded.access_token == tokens.access_token
        assert loaded.refresh_token == tokens.refresh_token
        assert loaded.person_id == tokens.person_id
        assert loaded.expires_at == pytest.approx(tokens.expires_at, abs=1)

    def test_file_permissions_600(self):
        tokens = _sample_tokens()
        save_tokens(tokens)
        path = token_path()
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_load_returns_none_when_missing(self):
        assert load_tokens() is None

    def test_load_returns_none_on_corrupt_file(self, tmp_path):
        token_path().write_text("not-valid-json{{")
        assert load_tokens() is None

    def test_save_without_refresh_token(self):
        tokens = _sample_tokens(refresh_token=None)
        save_tokens(tokens)
        loaded = load_tokens()
        assert loaded is not None
        assert loaded.refresh_token is None

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        deep_path = tmp_path / "nested" / "dirs" / "tokens.json"
        monkeypatch.setattr("linkedin_mcp_server.api.tokens._TOKEN_FILE", deep_path)
        save_tokens(_sample_tokens())
        assert deep_path.exists()


class TestClearTokens:
    def test_removes_file(self):
        save_tokens(_sample_tokens())
        assert token_path().exists()
        clear_tokens()
        assert not token_path().exists()

    def test_noop_when_no_file(self):
        clear_tokens()  # must not raise


class TestIsExpired:
    def test_not_expired(self):
        tokens = _sample_tokens(expires_at=time.time() + 3600)
        assert not is_expired(tokens)

    def test_expired(self):
        tokens = _sample_tokens(expires_at=time.time() - 1)
        assert is_expired(tokens)

    def test_within_buffer_is_expired(self):
        # Token expires in 4 minutes — less than the 5-minute buffer
        tokens = _sample_tokens(expires_at=time.time() + _EXPIRY_BUFFER - 60)
        assert is_expired(tokens)

    def test_just_outside_buffer_is_not_expired(self):
        tokens = _sample_tokens(expires_at=time.time() + _EXPIRY_BUFFER + 60)
        assert not is_expired(tokens)


class TestRefreshAccessToken:
    def test_raises_without_refresh_token(self):
        tokens = _sample_tokens(refresh_token=None)
        with pytest.raises(ValueError, match="No refresh token"):
            refresh_access_token(tokens, "client_id", "client_secret")

    def test_successful_refresh(self):
        tokens = _sample_tokens()
        new_expires_in = 7200
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "new-access-token",
            "expires_in": new_expires_in,
            "refresh_token": "new-refresh-token",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("linkedin_mcp_server.api.tokens.httpx.post", return_value=mock_resp):
            refreshed = refresh_access_token(tokens, "cid", "csecret")

        assert refreshed.access_token == "new-access-token"
        assert refreshed.refresh_token == "new-refresh-token"
        assert refreshed.person_id == tokens.person_id
        assert refreshed.expires_at > time.time()

    def test_refresh_keeps_old_refresh_token_when_not_returned(self):
        tokens = _sample_tokens(refresh_token="old-refresh")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "new-access",
            "expires_in": 3600,
            # no "refresh_token" key
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("linkedin_mcp_server.api.tokens.httpx.post", return_value=mock_resp):
            refreshed = refresh_access_token(tokens, "cid", "csecret")

        assert refreshed.refresh_token == "old-refresh"

    def test_refresh_persists_tokens(self):
        tokens = _sample_tokens()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "new", "expires_in": 3600}
        mock_resp.raise_for_status = MagicMock()

        with patch("linkedin_mcp_server.api.tokens.httpx.post", return_value=mock_resp):
            refresh_access_token(tokens, "cid", "csecret")

        saved = load_tokens()
        assert saved is not None
        assert saved.access_token == "new"
