import pytest

from linkedin_mcp_server.authentication import clear_session, get_authentication_source
from linkedin_mcp_server.exceptions import CredentialsNotFoundError


def test_get_auth_source_profile(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda: True
    )
    assert get_authentication_source() == "profile"


def test_get_auth_source_cookie(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda: False
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.get_linkedin_cookie", lambda: "cookie"
    )
    assert get_authentication_source() == "cookie"


def test_get_auth_source_none_raises(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.profile_exists", lambda: False
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.get_linkedin_cookie", lambda: None
    )
    with pytest.raises(CredentialsNotFoundError):
        get_authentication_source()


def test_clear_session_removes_profile(browser_profile):
    assert browser_profile.exists()
    result = clear_session(browser_profile)
    assert result is True
    assert not browser_profile.exists()


def test_clear_session_no_profile(isolate_user_data_dir):
    result = clear_session(isolate_user_data_dir)
    assert result is True  # No error even if profile doesn't exist
