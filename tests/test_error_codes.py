"""The error code a failed call names, beside the message written for people."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import Client

import linkedin_mcp_server.server as server_module
from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    ElementNotFoundError,
    InvalidAnswerError,
    InvalidReferenceError,
    LinkedInOperationError,
    NetworkError,
    OffLinkedInLandingError,
    PageReadError,
    ProfileNotFoundError,
    ProxyConnectionError,
    RateLimitError,
)
from linkedin_mcp_server.error_handler import (
    ERROR_META_KEY,
    CodedToolError,
    ErrorCode,
    raise_tool_error,
)
from linkedin_mcp_server.exceptions import (
    AuthenticationBootstrapFailedError,
    AuthenticationInProgressError,
    AuthenticationStartedError,
    AuthMissingOnOwnerError,
    BrowserBinaryMissingError,
    BrowserBusyError,
    BrowserDowngradeError,
    BrowserSetupFailedError,
    BrowserSetupInProgressError,
    CredentialsNotFoundError,
    DockerHostLoginRequiredError,
    LinkedInMCPError,
    LinuxBrowserDependencyError,
    OwnerStandingDownError,
    SessionExpiredError,
)
from linkedin_mcp_server.sequential_tool_middleware import (
    SequentialToolExecutionMiddleware,
)
from linkedin_mcp_server.server import create_mcp_server
from linkedin_mcp_server.session_state import PeerSessionInPlaceError

C = ErrorCode

#: One failure per branch of `raise_tool_error`, with the code it must name.
_BRANCHES = [
    (CredentialsNotFoundError("x"), C.AUTH_REQUIRED),
    (BrowserSetupInProgressError("x"), C.BROWSER_UNAVAILABLE),
    (BrowserSetupFailedError("x"), C.BROWSER_UNAVAILABLE),
    (OwnerStandingDownError("x"), C.BROWSER_BUSY),
    (AuthenticationStartedError("x"), C.AUTH_REQUIRED),
    (AuthenticationInProgressError("x"), C.AUTH_REQUIRED),
    (AuthenticationBootstrapFailedError("x"), C.AUTH_REQUIRED),
    (PeerSessionInPlaceError("x"), C.BROWSER_BUSY),
    (AuthMissingOnOwnerError("x"), C.AUTH_REQUIRED),
    (BrowserBusyError(), C.BROWSER_BUSY),
    (DockerHostLoginRequiredError("x"), C.AUTH_REQUIRED),
    (LinuxBrowserDependencyError("x"), C.BROWSER_UNAVAILABLE),
    (BrowserBinaryMissingError("x"), C.BROWSER_UNAVAILABLE),
    (
        BrowserDowngradeError(profile_version="2.0", browser_version="1.0"),
        C.BROWSER_UNAVAILABLE,
    ),
    (AccountRestrictedError(), C.ACCOUNT_RESTRICTED),
    (SessionExpiredError(), C.AUTH_REQUIRED),
    (AuthenticationError("x"), C.AUTH_REQUIRED),
    (RateLimitError("x", suggested_wait_time=120), C.RATE_LIMITED),
    (ProfileNotFoundError("x"), C.NOT_FOUND),
    (ElementNotFoundError("x"), C.PAGE_UNREADABLE),
    (ProxyConnectionError("x"), C.NETWORK),
    (OffLinkedInLandingError("https://portal.example/"), C.NETWORK),
    (NetworkError("x"), C.NETWORK),
    (PageReadError("x"), C.PAGE_UNREADABLE),
    (InvalidReferenceError("x"), C.INVALID_ARGUMENT),
    (InvalidAnswerError("x"), C.INVALID_ARGUMENT),
    (LinkedInOperationError("x"), C.INTERNAL),
    (LinkedInMCPError("x"), C.INTERNAL),
]


@pytest.fixture(autouse=True)
def no_issue_reports(monkeypatch):
    """Keep the diagnostics branches from writing issue notes."""

    def refuse(*_args, **_kwargs):
        raise RuntimeError("no diagnostics in this test")

    monkeypatch.setattr(
        "linkedin_mcp_server.error_handler.build_issue_diagnostics", refuse
    )


@pytest.mark.parametrize(
    ("failure", "code"), _BRANCHES, ids=[type(f).__name__ for f, _ in _BRANCHES]
)
def test_every_branch_names_its_code(failure, code):
    with pytest.raises(CodedToolError) as caught:
        raise_tool_error(failure, "get_person_profile")

    assert caught.value.code is code
    assert caught.value.retry_after == (120 if code is C.RATE_LIMITED else None)


def test_a_redacted_pass_through_keeps_its_code(monkeypatch):
    from linkedin_mcp_server.config.schema import AppConfig

    user, secret = "acct" + "zone9", "s3" + "cr3t"
    config = AppConfig()
    config.browser.proxy_server = "http://gate.example:7000"
    config.browser.proxy_username = user
    config.browser.proxy_password = secret
    monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)
    shaped = CodedToolError(
        f"failed via http://{user}:{secret}@gate.example:7000",
        C.RATE_LIMITED,
        retry_after=60,
    )

    with pytest.raises(CodedToolError) as caught:
        raise_tool_error(shaped, "get_person_profile")

    assert secret not in str(caught.value)
    assert (caught.value.code, caught.value.retry_after) == (C.RATE_LIMITED, 60)


async def test_a_lease_wait_that_gives_up_is_browser_busy(monkeypatch):
    lease = MagicMock()
    lease.try_acquire.return_value = False
    lease.acquire = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "linkedin_mcp_server.sequential_tool_middleware.get_profile_lease",
        lambda: lease,
    )
    context = MagicMock(fastmcp_context=None)
    context.message.name = "get_person_profile"

    with pytest.raises(CodedToolError) as caught:
        await SequentialToolExecutionMiddleware().on_call_tool(context, AsyncMock())

    assert caught.value.code is C.BROWSER_BUSY


@pytest.fixture
def server(monkeypatch):
    """The production server, with a lifespan that touches no browser."""
    for name in (
        "initialize_bootstrap",
        "get_runtime_policy",
        "report_retained_browser_revisions_if_ready",
    ):
        monkeypatch.setattr(server_module, name, MagicMock())
    for name in (
        "start_background_browser_setup_if_needed",
        "watch_for_handoff_requests",
        "stop_background_browser_setup",
        "close_browser",
    ):
        monkeypatch.setattr(server_module, name, AsyncMock())
    return create_mcp_server()


_PROFILE = ("get_person_profile", {"linkedin_username": "someone"})


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize(
    ("call", "failure", "text", "marker"),
    [
        (
            _PROFILE,
            RateLimitError("x", suggested_wait_time=120),
            "Rate limit detected. Wait 120 seconds before trying again.",
            {"v": 1, "code": "rate_limited", "retry_after": 120},
        ),
        (
            _PROFILE,
            RuntimeError("not classified"),
            "Error calling tool 'get_person_profile'",
            {"v": 1, "code": "internal"},
        ),
        (
            ("get_person_profile", {}),
            None,
            None,
            {"v": 1, "code": "invalid_argument"},
        ),
        (
            ("search_people", {"keywords": "a", "network": ["Z"]}),
            None,
            None,
            {"v": 1, "code": "invalid_argument"},
        ),
        (
            ("get_person_profiles", {"linkedin_username": "someone"}),
            None,
            None,
            {"v": 1, "code": "invalid_argument"},
        ),
    ],
    ids=["shaped", "masked", "schema", "filter", "unknown tool"],
)
async def test_a_client_reads_the_code_beside_the_message(
    server, mode, call, failure, text, marker
):
    name, arguments = call
    ready = AsyncMock(side_effect=failure or AssertionError("reached the browser"))
    with patch("linkedin_mcp_server.tools.person.get_ready_extractor", ready):
        async with Client(server, mode=mode) as client:
            result = await client.call_tool(name, arguments, raise_on_error=False)

    assert result.is_error
    assert result.structured_content is None
    assert (result.meta or {}).get(ERROR_META_KEY) == marker
    if text is not None:
        assert [block.text for block in result.content] == [text]
