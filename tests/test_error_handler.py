import pytest
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.exceptions import (
    NetworkError,
    ProfileNotFoundError,
    ProxyConnectionError,
    RateLimitError,
    ScrapingError,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    BrowserBinaryMissingError,
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


def test_raises_tool_error_for_browser_binary_missing():
    with pytest.raises(ToolError, match="Patchright Chromium browser is missing"):
        raise_tool_error(
            BrowserBinaryMissingError(
                "Patchright Chromium browser is missing. "
                "Run 'uv run patchright install chromium', "
                "or restart the server to auto-install."
            )
        )


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


def test_authentication_in_progress_surfaces_poll_friendly_message_verbatim():
    """The pending-login message passes through verbatim with no diagnostics block."""
    message = (
        "A LinkedIn login window is open and login is still in progress. "
        "This is not a failure. Complete the sign-in in the browser, then "
        "call this exact tool again in about 30 seconds to resume."
    )
    with pytest.raises(ToolError, match="not a failure") as exc_info:
        raise_tool_error(AuthenticationInProgressError(message))

    surfaced = str(exc_info.value)
    assert surfaced == message
    # Plain str() branch, not the LinkedInMCPError catch-all that appends an
    # issue-template diagnostics block.
    assert "issue" not in surfaced.lower()


def test_reraises_unknown_exception():
    """Unknown exceptions are re-raised as-is, not wrapped in ToolError."""
    with pytest.raises(ValueError, match="oops"):
        raise_tool_error(ValueError("oops"))


def test_proxy_error_reports_the_proxy_not_a_network_problem():
    # It subclasses NetworkError, so the specific branch has to come first;
    # otherwise the user is told to check their connection.
    with pytest.raises(ToolError, match="proxy"):
        raise_tool_error(
            ProxyConnectionError("Could not reach LinkedIn through proxy gate:7000")
        )


def test_proxy_error_skips_issue_diagnostics(monkeypatch):
    # A proxy that is down or misconfigured is not a bug worth reporting.
    monkeypatch.setattr(
        "linkedin_mcp_server.error_handler.build_issue_diagnostics",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("diagnostics should not run")
        ),
    )

    with pytest.raises(ToolError):
        raise_tool_error(ProxyConnectionError("proxy gate:7000 is unreachable"))


def test_unknown_exception_log_is_redacted(monkeypatch, caplog):
    """The catch-all log must not carry proxy credentials.

    Anything the handler cannot classify lands here, so a raw driver error
    quoting the proxy URL arrives intact. This is the boundary every tool
    wrapper funnels unknown failures through.
    """
    import logging

    from linkedin_mcp_server.config.schema import AppConfig

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_username = "acctzone9"
    config.browser.proxy_password = "s3cr3t"
    monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

    with caplog.at_level(logging.DEBUG), pytest.raises(Exception) as excinfo:
        raise_tool_error(
            Exception("failed via http://acctzone9:s3cr3t@gate.example:7000"),
            "get_person_profile",
        )

    assert "s3cr3t" not in caplog.text
    assert "acctzone9" not in caplog.text
    # The exception that leaves this function must be clean too: FastMCP calls
    # logger.exception on it (server.py:1343), writing the message and the
    # whole traceback again before the client-facing reply is masked.
    assert "s3cr3t" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


async def test_fastmcp_boundary_logging_stays_clean(monkeypatch):
    """The real boundary: FastMCP logs whatever leaves raise_tool_error.

    `mask_error_details` only sanitises the reply to the client. The server
    still writes the raw exception and its traceback to its own log first, so
    the exception itself has to be clean by the time it gets there.

    Asserted against the rendered output rather than caplog: FastMCP logs via
    logger.exception through a Rich handler, so the credentials live in the
    traceback rather than the message. A caplog.text assertion passes while the
    rendered traceback still leaks -- verified by reintroducing the bug.
    """
    import contextlib
    import io

    from fastmcp import FastMCP

    from linkedin_mcp_server.config.schema import AppConfig

    # Built rather than written literally: Rich renders the failing source line
    # inside its traceback, so a literal here would appear in the captured
    # output and fail the assertion for the wrong reason.
    user = "acct" + "zone9"
    secret = "s3" + "cr3t"

    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_username = user
    config.browser.proxy_password = secret
    monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

    mcp = FastMCP("test", mask_error_details=True)

    @mcp.tool
    async def failing_tool() -> str:
        raise_tool_error(
            Exception(f"failed via http://{user}:{secret}@gate.example:7000"),
            "failing_tool",
        )

    rendered = io.StringIO()
    with contextlib.redirect_stderr(rendered), contextlib.redirect_stdout(rendered):
        with pytest.raises(Exception):
            # call_tool, not _call_tool: the logging boundary lives in the
            # public wrapper, and the private one never reaches it.
            await mcp.call_tool("failing_tool", {})

    output = rendered.getvalue()
    assert secret not in output
    assert user not in output
