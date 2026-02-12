import pytest

from linkedin_mcp_server.authentication import clear_profile, get_authentication_source
from linkedin_mcp_server.drivers.browser import profile_exists
from linkedin_mcp_server.exceptions import CredentialsNotFoundError


# --- profile_exists() tests ---


def test_profile_exists_missing_dir(tmp_path):
    """Missing directory returns False."""
    assert profile_exists(tmp_path / "nonexistent") is False


def test_profile_exists_empty_dir(tmp_path):
    """Empty directory returns False."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert profile_exists(empty) is False


def test_profile_exists_non_empty_dir(profile_dir):
    """Non-empty directory returns True."""
    assert profile_exists(profile_dir) is True


def test_profile_exists_file_path(tmp_path):
    """A file (not directory) returns False."""
    f = tmp_path / "not_a_dir"
    f.write_text("data")
    assert profile_exists(f) is False


# --- get_authentication_source() tests ---


def test_get_auth_source_profile(profile_dir, monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda _dir=None: True
    )
    assert get_authentication_source() is True


def test_get_auth_source_none_raises(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda _dir=None: False
    )
    with pytest.raises(CredentialsNotFoundError):
        get_authentication_source()


# --- clear_profile() tests ---


def test_clear_profile_removes_dir(profile_dir):
    assert profile_dir.exists()
    result = clear_profile(profile_dir)
    assert result is True
    assert not profile_dir.exists()


def test_clear_profile_no_dir(isolate_profile_dir):
    result = clear_profile(isolate_profile_dir)
    assert result is True  # No error even if dir doesn't exist
