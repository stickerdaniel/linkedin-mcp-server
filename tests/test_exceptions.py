from linkedin_mcp_server.exceptions import (
    CredentialsNotFoundError,
    LinkedInMCPError,
    SessionExpiredError,
)


def test_base_exception():
    err = LinkedInMCPError("test")
    assert str(err) == "test"


def test_session_expired_default_message():
    err = SessionExpiredError()
    assert "expired" in str(err).lower()


def test_session_expired_custom_message():
    err = SessionExpiredError("custom")
    assert str(err) == "custom"


def test_inheritance():
    assert issubclass(SessionExpiredError, LinkedInMCPError)
    assert issubclass(CredentialsNotFoundError, LinkedInMCPError)
