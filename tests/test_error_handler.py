from linkedin_scraper.core.exceptions import RateLimitError

from linkedin_mcp_server.error_handler import handle_tool_error
from linkedin_mcp_server.exceptions import (
    CredentialsNotFoundError,
    SessionExpiredError,
)


def test_handles_session_expired():
    result = handle_tool_error(SessionExpiredError(), "test_tool")
    assert result["error"] == "session_expired"
    assert "message" in result
    assert "resolution" in result


def test_handles_credentials_not_found():
    result = handle_tool_error(CredentialsNotFoundError("no creds"), "test_tool")
    assert result["error"] == "authentication_not_found"


def test_handles_generic_exception():
    result = handle_tool_error(ValueError("oops"), "test_tool")
    assert result["error"] == "unknown_error"
    assert "oops" in result["message"]


def test_handles_rate_limit_with_suggested_wait():
    """Test RateLimitError with custom suggested_wait_time attribute."""
    error = RateLimitError("Rate limited")
    error.suggested_wait_time = 600
    result = handle_tool_error(error, "test_tool")
    assert result["error"] == "rate_limit"
    assert result["suggested_wait_seconds"] == 600
    assert "600" in result["resolution"]


def test_handles_rate_limit_default_wait():
    """Test RateLimitError without suggested_wait_time uses default 300."""
    error = RateLimitError("Rate limited")
    result = handle_tool_error(error, "test_tool")
    assert result["error"] == "rate_limit"
    assert result["suggested_wait_seconds"] == 300
    assert "300" in result["resolution"]
