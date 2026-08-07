import logging

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
    AuthMissingOnOwnerError,
    AuthStaleOnOwnerError,
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


def test_browser_downgrade_skips_issue_diagnostics():
    """A browser refused for being older than the profile is the guard working.

    Without its own branch it falls into the ``LinkedInMCPError`` catch-all,
    which appends the issue-report template and sends the user to the tracker
    for something behaving exactly as designed. It also has to keep its own
    message: both versions and the two ways out are in it, and a generic
    summary would leave nobody able to act.

    Asserted on the surfaced text, not by patching ``build_issue_diagnostics``:
    that builder's failures are swallowed, so a patched-out one makes the
    catch-all fall back to a template-free message and the mutation survives.
    Measured, exactly as it was for the owner-auth branch above.
    """
    from linkedin_mcp_server.exceptions import BrowserDowngradeError

    error = BrowserDowngradeError(
        profile_version="151.0.7922.34",
        browser_version="148.0.7778.96",
        browser_product="Google Chrome for Testing",
        profile_dir="/home/pwuser/.linkedin-mcp/profile",
    )

    with pytest.raises(ToolError) as caught:
        raise_tool_error(error, "get_person_profile")

    surfaced = str(caught.value)
    assert surfaced == str(error)
    assert "Diagnostics:" not in surfaced
    assert "issue" not in surfaced.lower()


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


def test_owner_auth_refusal_skips_issue_diagnostics():
    """A shared browser that cannot sign in is designed behaviour, not a bug.

    The LinkedInMCPError catch-all appends an issue-report template, which would
    send users to the tracker for something working as intended. Its own branch
    exists for that.

    Asserted on the surfaced text rather than by patching build_issue_diagnostics:
    a patched-out builder makes the catch-all fall back to a message with no
    template, so the mutation of removing the branch survived. Measured, the
    unbranched path really appends "Diagnostics:" and an issue-template path.
    """
    for error in (
        AuthMissingOnOwnerError("the shared browser has no session"),
        AuthStaleOnOwnerError("the shared browser's session stopped working"),
    ):
        with pytest.raises(ToolError) as caught:
            raise_tool_error(error, "get_person_profile")

        surfaced = str(caught.value)
        assert surfaced == str(error)
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


class TestAnAlreadyShapedToolErrorPassesThrough:
    """One failure reaches ``raise_tool_error`` twice, and the second pass must
    not re-derive it.

    A tool wraps its body in ``except Exception`` while the helpers it call shape
    their own failures, so one failure arrives once as the domain exception and
    again as the ``ToolError`` that produced. A ``ToolError`` matched none of the
    type branches, so it fell through to the catch-all's ``raise safe from None``.

    What that cost, checked rather than assumed. The client-visible *message*
    survived: ``redacted_copy`` returns the same object when the text needs no
    rewriting, so nothing was reworded. What was lost was ``__cause__``, and with
    it the ability of anything downstream to tell one failure from another, plus a
    second log line calling an already-classified failure unexpected.

    Redaction is the exception, and it is why the pass-through is conditional: a
    message that *does* need rewriting cannot keep its cause, because the original
    still carries the credential.
    """

    def test_the_original_cause_survives_the_second_pass(self):
        original = SessionExpiredError("the session on disk is stale")

        with pytest.raises(ToolError) as caught:
            try:
                raise_tool_error(original, "get_person_profile")
            except ToolError as shaped:
                # What a tool's own `except Exception` does with it.
                raise_tool_error(shaped, "get_person_profile")

        # The chain is what a middleware classifies a failure by. Before the fix
        # this was [ToolError] alone.
        chain = []
        current: BaseException | None = caught.value
        while current is not None:
            chain.append(type(current))
            current = current.__cause__
        assert chain == [ToolError, SessionExpiredError]

    def test_the_second_pass_is_not_logged_as_an_unexpected_error(self, caplog):
        # The catch-all logs at ERROR as "Unexpected error". A failure a branch
        # already classified and logged at WARNING must not be reported a second
        # time as something nothing recognised, which is what a support log then
        # shows for an ordinary expired session.
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ToolError):
                try:
                    raise_tool_error(SessionExpiredError(), "get_person_profile")
                except ToolError as shaped:
                    raise_tool_error(shaped, "get_person_profile")

        assert "Unexpected error" not in caplog.text

    def test_the_cause_reaches_a_middleware_at_a_real_tool_catch_site(self):
        # The property the daemon work depends on, asserted through a real tool
        # rather than by calling raise_tool_error directly: the double pass is
        # produced by the tool's own `except Exception`, so a synthetic
        # reproduction proves nothing about the shape the code actually takes.
        import asyncio

        from fastmcp import FastMCP
        from fastmcp.server.middleware import Middleware

        seen: dict[str, list[type]] = {}

        class Sniff(Middleware):
            async def on_call_tool(self, context, call_next):
                try:
                    return await call_next(context)
                except Exception as exc:
                    chain: list[type] = []
                    current: BaseException | None = exc
                    while current is not None:
                        chain.append(type(current))
                        current = current.__cause__
                    seen["chain"] = chain
                    raise

        mcp = FastMCP("test", mask_error_details=True)
        mcp.add_middleware(Sniff())

        @mcp.tool
        async def failing_tool() -> str:
            try:
                raise_tool_error(SessionExpiredError(), "failing_tool")
            except Exception as exc:  # exactly what every tool body does
                raise_tool_error(exc, "failing_tool")

        async def drive() -> None:
            with pytest.raises(Exception):
                await mcp.call_tool("failing_tool", {})

        asyncio.run(drive())

        assert seen["chain"] == [ToolError, SessionExpiredError]

    def test_a_shaped_error_carrying_a_credential_is_still_redacted(self, monkeypatch):
        # The pass-through must not become a way around redaction. Before this
        # was guarded, a proxy password inside an already-shaped ToolError reached
        # the client and the log in clear text, where the catch-all had been
        # rewriting it. mask_error_details does not help: FastMCP logs the
        # exception and its traceback before masking the reply.
        #
        # Built rather than written literally so the assertion cannot match this
        # source line if it is ever echoed back in a traceback.
        user = "acct" + "zone9"
        secret = "s3" + "cr3t"

        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = user
        config.browser.proxy_password = secret
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

        shaped = ToolError(f"failed via http://{user}:{secret}@gate.example:7000")

        with pytest.raises(ToolError) as caught:
            raise_tool_error(shaped, "get_person_profile")

        assert secret not in str(caught.value)
        assert user not in str(caught.value)
        # The one case where the cause cannot be kept: a rewritten message means
        # a new object, and the original still carries the credential.
        assert caught.value.__cause__ is None

        # That the pass-through is for ToolError only, and an unknown exception
        # still reaches the catch-all, is `test_reraises_unknown_exception` above.
