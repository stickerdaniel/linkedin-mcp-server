import pytest

from linkedin_mcp_server.authentication import clear_session, get_authentication_source
from linkedin_mcp_server.exceptions import CredentialsNotFoundError


def test_get_auth_source_session(session_file, monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.session_exists", lambda: True
    )
    assert get_authentication_source() == "session"


def test_get_auth_source_cookie(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.session_exists", lambda: False
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.get_linkedin_cookie", lambda: "cookie"
    )
    assert get_authentication_source() == "cookie"


def test_get_auth_source_none_raises(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.session_exists", lambda: False
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.authentication.get_linkedin_cookie", lambda: None
    )
    with pytest.raises(CredentialsNotFoundError):
        get_authentication_source()


def test_clear_session_removes_file(session_file):
    assert session_file.exists()
    result = clear_session(session_file)
    assert result is True
    assert not session_file.exists()


def test_clear_session_no_file(isolate_session_path):
    result = clear_session(isolate_session_path)
    assert result is True  # No error even if file doesn't exist
