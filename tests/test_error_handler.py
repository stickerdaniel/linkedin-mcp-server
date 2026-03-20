import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import (
    NetworkError,
    ProfileNotFoundError,
    RateLimitError,
    ScrapingError,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    CredentialsNotFoundError,
    LinkedInMCPError,
    SessionExpiredError,
)


def test_raises_tool_error_for_session_expired():
    with pytest.raises(ToolError, match="Session expired"):
        raise_tool_error(SessionExpiredError())


def test_raises_tool_error_for_credentials_not_found():
    with pytest.raises(ToolError, match="Authentication not found"):
        raise_tool_error(CredentialsNotFoundError("no creds"))


def test_raises_tool_error_for_rate_limit_with_custom_wait():
    error = RateLimitError("Rate limited")
    error.suggested_wait_time = 600
    with pytest.raises(ToolError, match="Wait 600 seconds"):
        raise_tool_error(error)


def test_raises_tool_error_for_rate_limit_default_wait():
    error = RateLimitError("Rate limited")
    with pytest.raises(ToolError, match="Wait 300 seconds"):
        raise_tool_error(error)


def test_raises_tool_error_for_profile_not_found():
    with pytest.raises(ToolError, match="Profile not found"):
        raise_tool_error(ProfileNotFoundError("gone"))


def test_rate_limit_skips_issue_diagnostics(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.error_handler.build_issue_diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("diagnostics should not run")
        ),
    )
    error = RateLimitError("Rate limited")

    with pytest.raises(ToolError, match="Wait 300 seconds"):
        raise_tool_error(error)


def test_profile_not_found_skips_issue_diagnostics(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.error_handler.build_issue_diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("diagnostics should not run")
        ),
    )

    with pytest.raises(ToolError, match="Profile not found"):
        raise_tool_error(ProfileNotFoundError("gone"))


def test_raises_tool_error_for_network_error():
    with pytest.raises(ToolError, match="Network error"):
        raise_tool_error(NetworkError("timeout"))


def test_raises_tool_error_for_scraping_error():
    with pytest.raises(ToolError, match="Scraping failed"):
        raise_tool_error(ScrapingError("bad html"))


def test_raises_tool_error_for_base_scraper_exception():
    from linkedin_mcp_server.core.exceptions import LinkedInScraperException

    with pytest.raises(ToolError, match="generic scraper error"):
        raise_tool_error(LinkedInScraperException("generic scraper error"))


def test_raises_tool_error_for_linkedin_mcp_error():
    with pytest.raises(ToolError, match="custom mcp error"):
        raise_tool_error(LinkedInMCPError("custom mcp error"))


def test_raises_tool_error_for_authentication_error():
    from linkedin_mcp_server.core.exceptions import AuthenticationError

    with pytest.raises(ToolError, match="Authentication failed"):
        raise_tool_error(AuthenticationError("bad creds"))


def test_raises_tool_error_for_element_not_found():
    from linkedin_mcp_server.core.exceptions import ElementNotFoundError

    with pytest.raises(ToolError, match="Element not found"):
        raise_tool_error(ElementNotFoundError("missing"))


def test_reraises_unknown_exception():
    """Unknown exceptions are re-raised as-is, not wrapped in ToolError."""
    with pytest.raises(ValueError, match="oops"):
        raise_tool_error(ValueError("oops"))
