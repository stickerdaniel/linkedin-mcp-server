"""
Centralized error handling for LinkedIn MCP Server using FastMCP ToolError.

Provides raise_tool_error() which maps known LinkedIn exceptions to user-friendly
ToolError messages. Unknown exceptions are re-raised as-is for mask_error_details
to handle.
"""

import logging
from enum import StrEnum
from typing import Any, NoReturn

import mcp.types as mt
from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.core.proxy_errors import (
    redact_proxy_credentials,
    redacted_copy,
)
from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    InvalidAnswerError,
    InvalidReferenceError,
    AuthenticationError,
    ElementNotFoundError,
    LinkedInOperationError,
    NetworkError,
    OffLinkedInLandingError,
    PageReadError,
    ProfileNotFoundError,
    ProxyConnectionError,
    RateLimitError,
)

from linkedin_mcp_server.exceptions import (
    AuthenticationBootstrapFailedError,
    AuthenticationInProgressError,
    AuthenticationStartedError,
    BrowserBinaryMissingError,
    BrowserBusyError,
    BrowserDowngradeError,
    BrowserSetupFailedError,
    BrowserSetupInProgressError,
    CredentialsNotFoundError,
    OwnerStandingDownError,
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
    LinkedInMCPError,
    OwnerCannotAuthenticateError,
    SessionExpiredError,
)
from linkedin_mcp_server.core.throttle import throttle_evidence
from linkedin_mcp_server.session_state import PeerSessionInPlaceError
from linkedin_mcp_server.error_diagnostics import (
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """What kind of failure a tool call ended in, for a program to branch on.

    A closed set, and part of the contract: a value is never renamed or given a
    new meaning, because clients match on it instead of on the message, which is
    written for people and changes between releases.
    """

    AUTH_REQUIRED = "auth_required"
    ACCOUNT_RESTRICTED = "account_restricted"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    PAGE_UNREADABLE = "page_unreadable"
    INVALID_ARGUMENT = "invalid_argument"
    NETWORK = "network"
    BROWSER_BUSY = "browser_busy"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    OUTCOME_UNKNOWN = "outcome_unknown"
    INTERNAL = "internal"


#: Namespaced beside the daemon's ``linkedin.dev/auth`` marker. Version 1 is
#: ``{"v", "code"}`` plus ``retry_after`` (seconds) on ``rate_limited``.
ERROR_META_KEY = "linkedin.dev/error"
ERROR_META_VERSION = 1


def error_result(
    message: str,
    code: ErrorCode,
    *,
    retry_after: int | None = None,
    meta: dict[str, Any] | None = None,
    structured_content: dict[str, Any] | None = None,
) -> ToolResult:
    """The error result every failure this server answers with is built from.

    A raised failure reaches it through :class:`ErrorCodeMiddleware`; an answer
    the daemon returns instead of raising (a marker for the frontend, an unknown
    outcome) calls it directly, so both carry the code the same way. *meta* and
    *structured_content* are kept as given beside the code.
    """
    marker: dict[str, Any] = {"v": ERROR_META_VERSION, "code": code}
    if retry_after is not None:
        marker["retry_after"] = retry_after
    return ToolResult(
        content=[mt.TextContent(type="text", text=message)],
        structured_content=structured_content,
        meta={**(meta or {}), ERROR_META_KEY: marker},
        is_error=True,
    )


class CodedToolError(ToolError):
    """A shaped tool failure that names its :class:`ErrorCode`.

    The code has a default because ``redacted_copy`` rebuilds an exception from
    its message alone; the pass-through below puts the code back.
    """

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.INTERNAL,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class ErrorCodeMiddleware(Middleware):
    """Answer a failed call with an error result whose ``_meta`` names its code.

    FastMCP turns a raised ``ToolError`` into a result carrying only its text, so
    the code has to leave as a result. Outermost on every role: the middleware
    inside it still sees the exception, including the owner's auth marker, which
    walks ``__cause__``. Text and ``isError`` are what FastMCP would have sent.

    A returned error result passes as it is. One from this server already names
    its code through :func:`error_result`; one forwarded from an older owner may
    not, and its own markers say more than ``internal`` would.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except (ToolError, ValidationError, NotFoundError) as exc:
            retry_after = exc.retry_after if isinstance(exc, CodedToolError) else None
            return error_result(str(exc), _code_of(exc), retry_after=retry_after)


def _code_of(exc: Exception) -> ErrorCode:
    if isinstance(exc, CodedToolError):
        return exc.code
    # An unknown or disabled tool is a wrong name in the request.
    if isinstance(exc, (ValidationError, NotFoundError)):
        return ErrorCode.INVALID_ARGUMENT
    # FastMCP's own masked "Error calling tool", and any ToolError raised
    # without a code.
    return ErrorCode.INTERNAL


def _raise_tool_error_with_diagnostics(
    exception: Exception,
    message: str,
    *,
    context: str,
    code: ErrorCode,
) -> NoReturn:
    try:
        diagnostics = build_issue_diagnostics(exception, context=context)
    except Exception:
        logger.debug("Could not build issue diagnostics", exc_info=True)
        diagnostics = None

    if diagnostics is not None:
        message = format_tool_error_with_diagnostics(message, diagnostics)
    raise CodedToolError(message, code) from exception


def raise_tool_error(exception: Exception, context: str = "") -> NoReturn:
    """Shape the failure, then say so when LinkedIn was throttling this call.

    The evidence is appended rather than substituted: throttling explains why
    a call failed, but the shaped message is what says *which* call and what it
    was doing, and a session that expired while LinkedIn happened to refuse one
    beacon is still an expired session.

    An unclassified exception is the case this exists for. It is re-raised for
    ``mask_error_details`` to reduce to "Error calling tool", which is the
    right answer for an internal error and the wrong one for a throttled
    LinkedIn: the diagnosis was in the browser and reaching the client with
    nothing is what makes throttling read as a parser bug. When the record has
    something to say, it is said as a ``ToolError``, which survives masking;
    the underlying exception still goes nowhere.

    The evidence changes no code. A shaped failure keeps the one it was given,
    and an unclassified one stays ``internal``: a refused beacon during the
    call does not prove it is why the call failed.
    """
    try:
        _shape_tool_error(exception, context=context)
    except ToolError as shaped:
        evidence = throttle_evidence()
        if evidence is None or evidence in str(shaped):
            raise
        coded = shaped if isinstance(shaped, CodedToolError) else None
        # `from shaped.__cause__`, not `from shaped`: a chain walked by the
        # daemon middleware has to keep reaching the domain exception in one
        # hop, which `tests/test_error_handler.py` pins.
        raise CodedToolError(
            f"{shaped}\n\n{evidence}",
            coded.code if coded else ErrorCode.INTERNAL,
            retry_after=coded.retry_after if coded else None,
        ) from (shaped.__cause__ or shaped)
    except Exception as unclassified:
        evidence = throttle_evidence()
        if evidence is None:
            raise
        logger.warning(
            "Masked failure%s arrived while LinkedIn was throttling",
            f" in {context}" if context else "",
        )
        # Chained to what the catch-all raised, which is already the redacted
        # copy with its own chain severed, so no credential is reachable here.
        # It keeps the failure's type one hop from the ToolError, where the
        # daemon middleware walks `__cause__` to classify it.
        raise CodedToolError(evidence, ErrorCode.INTERNAL) from unclassified


def _shape_tool_error(exception: Exception, context: str = "") -> NoReturn:
    """
    Raise a ToolError for known LinkedIn exceptions, or re-raise unknown ones.

    Known exceptions are mapped to user-friendly messages via ToolError.
    Unknown exceptions are re-raised as-is so mask_error_details can mask them.
    A ToolError has already been shaped, so it keeps its cause instead of being
    re-derived, unless its message carries proxy credentials and has to be
    rewritten.

    Args:
        exception: The exception that occurred
        context: Optional context about which tool failed (for log correlation)

    Raises:
        ToolError: For known LinkedIn exception types
        Exception: Re-raises unknown exceptions as-is
    """
    ctx = f" in {context}" if context else ""

    if isinstance(exception, ToolError):
        # Already shaped, by an inner call to this very function or by a tool
        # body. `tool_errors` catches every `Exception` a tool raises while the
        # helpers it calls shape their own failures, so one failure arrives here
        # twice: once as the domain exception, then again as the ToolError that
        # produced.
        #
        # The second pass matched none of the branches below and fell through to
        # the catch-all, whose `raise ... from None` severs the chain. Middleware
        # that classifies a failure by walking `__cause__` then sees a bare
        # ToolError. Measured through a real tool: a failure wrapped twice arrived
        # as ['ToolError'] where the same one wrapped once arrived as
        # ['ToolError', <the cause>]. The catch-all also logs it a second time as
        # an unexpected error.
        #
        # Redaction still has to happen, and skipping it here was a real leak
        # rather than a theoretical one. Measured with a proxy password in a
        # ToolError message: passing the exception straight through published the
        # credential in clear text where the catch-all had been redacting it.
        # `mask_error_details` does not save this, because FastMCP logs the
        # exception and its traceback before masking the client-facing reply.
        #
        # So the cause is preserved only where it costs nothing:
        # `redacted_copy` returns the *same object* when the text needs no
        # rewriting (`core/proxy_errors.py:128-129`), which is the ordinary case.
        # When it does need rewriting there is no way to keep both, and the
        # credential boundary wins.
        safe = redacted_copy(exception)
        if safe is exception:
            raise exception
        logger.warning("Redacted an already-shaped tool error%s", ctx)
        if isinstance(exception, CodedToolError):
            safe = CodedToolError(
                str(safe), exception.code, retry_after=exception.retry_after
            )
        raise safe from None

    if isinstance(exception, CredentialsNotFoundError):
        logger.warning("Credentials not found%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Authentication not found. Run with --login to create a browser profile.",
            context=context,
            code=ErrorCode.AUTH_REQUIRED,
        )

    elif isinstance(exception, BrowserSetupInProgressError):
        logger.info("Browser setup in progress%s: %s", ctx, exception)
        raise CodedToolError(
            str(exception), ErrorCode.BROWSER_UNAVAILABLE
        ) from exception

    elif isinstance(exception, BrowserSetupFailedError):
        logger.warning("Browser setup failed%s: %s", ctx, exception)
        detail = redact_proxy_credentials(str(exception))
        raise CodedToolError(
            f"LinkedIn browser setup was not ready: {detail}. Retry this tool to "
            "start a fresh background setup attempt.",
            ErrorCode.BROWSER_UNAVAILABLE,
        ) from exception

    elif isinstance(exception, OwnerStandingDownError):
        logger.info("Browser owner standing down%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.BROWSER_BUSY) from exception

    elif isinstance(exception, AuthenticationStartedError):
        logger.info("Authentication started%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.AUTH_REQUIRED) from exception

    elif isinstance(exception, AuthenticationInProgressError):
        logger.info("Authentication in progress%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.AUTH_REQUIRED) from exception

    elif isinstance(exception, AuthenticationBootstrapFailedError):
        logger.warning("Authentication bootstrap failed%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.AUTH_REQUIRED) from exception

    # Not a failure at all: another client signed in while this one was asking
    # to. Diagnostics-free for the same reason as the branches above, and worth a
    # branch of its own so it does not reach the catch-all, where an ordinary
    # RuntimeError is masked into something the user cannot act on.
    elif isinstance(exception, PeerSessionInPlaceError):
        logger.info("Another client already signed in%s", ctx)
        raise CodedToolError(str(exception), ErrorCode.BROWSER_BUSY) from exception

    # Its own branch rather than the LinkedInMCPError catch-all, which appends an
    # issue-report template. A shared browser that cannot log in by itself is the
    # designed behaviour, not a bug, and inviting a bug report for it would send
    # users to the tracker for something working as intended. The two
    # Authentication*Error branches above are diagnostics-free for the same
    # reason.
    elif isinstance(exception, OwnerCannotAuthenticateError):
        logger.info("The shared browser needs this client to log in%s", ctx)
        raise CodedToolError(str(exception), ErrorCode.AUTH_REQUIRED) from exception

    # Contention, not a bug: no issue diagnostics, following RateLimitError.
    elif isinstance(exception, BrowserBusyError):
        logger.info("Shared browser busy%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.BROWSER_BUSY) from exception

    elif isinstance(exception, DockerHostLoginRequiredError):
        logger.warning("Docker host login required%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.AUTH_REQUIRED) from exception

    elif isinstance(exception, LinuxBrowserDependencyError):
        logger.warning("Linux browser dependency missing%s: %s", ctx, exception)
        raise CodedToolError(
            str(exception), ErrorCode.BROWSER_UNAVAILABLE
        ) from exception

    elif isinstance(exception, BrowserBinaryMissingError):
        logger.warning("Browser binary missing%s: %s", ctx, exception)
        raise CodedToolError(
            str(exception), ErrorCode.BROWSER_UNAVAILABLE
        ) from exception

    # Diagnostics-free, following BrowserBusyError. A browser refusing a profile
    # a newer one wrote is the guard doing its job, and the message already
    # names both versions and the two ways out. Appending an issue template
    # would send users to the tracker for something working as intended.
    elif isinstance(exception, BrowserDowngradeError):
        logger.warning("Browser older than the profile%s: %s", ctx, exception)
        raise CodedToolError(
            str(exception), ErrorCode.BROWSER_UNAVAILABLE
        ) from exception

    # Diagnostics-free, following BrowserDowngradeError: LinkedIn's decision
    # about the account is not a bug, and the message already names the only
    # way out.
    elif isinstance(exception, AccountRestrictedError):
        logger.warning("LinkedIn account restricted%s", ctx)
        raise CodedToolError(
            str(exception), ErrorCode.ACCOUNT_RESTRICTED
        ) from exception

    elif isinstance(exception, SessionExpiredError):
        logger.warning("Session expired%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Session expired. Run with --login to create a new browser profile.",
            context=context,
            code=ErrorCode.AUTH_REQUIRED,
        )

    elif isinstance(exception, AuthenticationError):
        logger.warning("Authentication failed%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Authentication failed. Run with --login to re-authenticate.",
            context=context,
            code=ErrorCode.AUTH_REQUIRED,
        )

    elif isinstance(exception, RateLimitError):
        wait_time = getattr(exception, "suggested_wait_time", 300)
        logger.warning("Rate limit%s: %s (wait=%ds)", ctx, exception, wait_time)
        raise CodedToolError(
            f"Rate limit detected. Wait {wait_time} seconds before trying again.",
            ErrorCode.RATE_LIMITED,
            retry_after=wait_time,
        ) from exception

    elif isinstance(exception, ProfileNotFoundError):
        logger.warning("Profile not found%s: %s", ctx, exception)
        raise CodedToolError(
            "Profile not found. Check the profile URL is correct.",
            ErrorCode.NOT_FOUND,
        ) from exception

    elif isinstance(exception, ElementNotFoundError):
        logger.warning("Element not found%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Element not found. LinkedIn page structure may have changed.",
            context=context,
            code=ErrorCode.PAGE_UNREADABLE,
        )

    elif isinstance(exception, ProxyConnectionError):
        # Ahead of NetworkError, which it subclasses. No issue diagnostics: a
        # proxy that is down or misconfigured is not a bug worth reporting.
        logger.warning("Proxy error%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.NETWORK) from exception

    # Also ahead of NetworkError, whose generic text drops the one useful fact:
    # where the browser landed. No issue diagnostics, following
    # ProxyConnectionError: a portal on the user's network is not a bug.
    elif isinstance(exception, OffLinkedInLandingError):
        logger.warning("Navigation ended off LinkedIn%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.NETWORK) from exception

    elif isinstance(exception, NetworkError):
        logger.warning("Network error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Network error. Check your connection and try again.",
            context=context,
            code=ErrorCode.NETWORK,
        )

    elif isinstance(exception, PageReadError):
        logger.warning("Page read error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Could not read the page. LinkedIn page structure may have changed.",
            context=context,
            code=ErrorCode.PAGE_UNREADABLE,
        )

    # Ahead of the catch-all, which they subclass. No issue diagnostics: an
    # argument the caller can correct is not a bug worth reporting, and an
    # issue template appended to it buries the correction it already names.
    elif isinstance(exception, (InvalidReferenceError, InvalidAnswerError)):
        logger.info("Invalid argument%s: %s", ctx, exception)
        raise CodedToolError(str(exception), ErrorCode.INVALID_ARGUMENT) from exception

    elif isinstance(exception, (LinkedInOperationError, LinkedInMCPError)):
        # Catch-all for base exception types and any future subclasses
        # without a dedicated handler above. Passes through str(exception).
        logger.warning("LinkedIn error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            str(exception),
            context=context,
            code=ErrorCode.INTERNAL,
        )

    else:
        # The catch-all for exceptions nothing else classified, so a raw driver
        # error quoting the proxy URL arrives here intact. Redacting this log is
        # not enough on its own: FastMCP catches whatever leaves this function
        # and calls logger.exception (server.py:1343), which writes the message
        # and the traceback again before masking the client-facing reply. So the
        # exception itself is sanitised, not just the log. `from None` because
        # the chained original would be printed there too.
        safe = redacted_copy(exception)
        logger.error("Unexpected error%s: %s", ctx, f"{type(safe).__name__}: {safe}")
        raise safe from None
