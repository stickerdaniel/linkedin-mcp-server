import pytest

from linkedin_mcp_server.authentication import clear_profile, get_authentication_source
from linkedin_mcp_server.exceptions import CredentialsNotFoundError


def test_get_auth_source_profile(profile_dir, monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda: True
    )
    assert get_authentication_source() is True


def test_get_auth_source_none_raises(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda: False
    )
    with pytest.raises(CredentialsNotFoundError):
        get_authentication_source()


def test_clear_profile_removes_dir(profile_dir):
    assert profile_dir.exists()
    result = clear_profile(profile_dir)
    assert result is True
    assert not profile_dir.exists()


def test_clear_profile_no_dir(isolate_profile_dir):
    result = clear_profile(isolate_profile_dir)
    assert result is True  # No error even if dir doesn't exist
