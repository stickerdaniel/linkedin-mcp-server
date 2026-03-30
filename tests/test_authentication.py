import json

import pytest
from linkedin_mcp_server.authentication import (
    clear_auth_state,
    clear_profile,
    get_authentication_source,
)
from linkedin_mcp_server.drivers.browser import profile_exists
from linkedin_mcp_server.exceptions import CredentialsNotFoundError
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
)


def _write_source_metadata(profile_dir, *, runtime_id="macos-arm64-host"):
    portable_cookie_path(profile_dir).write_text(
        json.dumps([{"name": "li_at", "domain": ".linkedin.com"}])
    )
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": runtime_id,
                "login_generation": "gen-1",
                "created_at": "2026-03-12T17:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(portable_cookie_path(profile_dir)),
            }
        )
    )


def test_profile_exists_missing_dir(tmp_path):
    assert profile_exists(tmp_path / "nonexistent") is False


def test_profile_exists_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert profile_exists(empty) is False


def test_profile_exists_non_empty_dir(profile_dir):
    assert profile_exists(profile_dir) is True


def test_profile_exists_file_path(tmp_path):
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("data")
    assert profile_exists(file_path) is False


def test_get_authentication_source_requires_metadata(profile_dir):  # noqa: ARG001
    with pytest.raises(CredentialsNotFoundError, match="source session metadata"):
        get_authentication_source()


def test_get_authentication_source_accepts_source_session(profile_dir):
    _write_source_metadata(profile_dir)
    assert get_authentication_source() is True


def test_get_authentication_source_none_raises(isolate_profile_dir):  # noqa: ARG001
    with pytest.raises(CredentialsNotFoundError):
        get_authentication_source()


def test_clear_profile_removes_dir(profile_dir):
    assert profile_dir.exists()
    result = clear_profile(profile_dir)
    assert result is True
    assert not profile_dir.exists()


def test_clear_auth_state_removes_source_files(profile_dir):
    _write_source_metadata(profile_dir)

    assert clear_auth_state(profile_dir) is True
    assert not profile_dir.exists()
    assert not portable_cookie_path(profile_dir).exists()
    assert not source_state_path(profile_dir).exists()
