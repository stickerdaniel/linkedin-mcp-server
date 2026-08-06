"""
Centralized error handling for LinkedIn MCP Server using FastMCP ToolError.

Provides raise_tool_error() which maps known LinkedIn exceptions to user-friendly
ToolError messages. Unknown exceptions are re-raised as-is for mask_error_details
to handle.
"""

import logging
from typing import NoReturn

from fastmcp.exceptions import ToolError

from linkedin_mcp_server.core.proxy_errors import redacted_copy
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    ElementNotFoundError,
    LinkedInScraperException,
    NetworkError,
    ProfileNotFoundError,
    ProxyConnectionError,
    RateLimitError,
    ScrapingError,
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
    DockerHostLoginRequiredError,
    LinuxBrowserDependencyError,
    LinkedInMCPError,
    OwnerCannotAuthenticateError,
    SessionExpiredError,
)
from linkedin_mcp_server.session_state import PeerSessionInPlaceError
from linkedin_mcp_server.error_diagnostics import (
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)

logger = logging.getLogger(__name__)


def _raise_tool_error_with_diagnostics(
    exception: Exception,
    message: str,
    *,
    context: str,
) -> NoReturn:
    try:
        diagnostics = build_issue_diagnostics(exception, context=context)
    except Exception:
        logger.debug("Could not build issue diagnostics", exc_info=True)
        diagnostics = None

    if diagnostics is not None:
        message = format_tool_error_with_diagnostics(message, diagnostics)
    raise ToolError(message) from exception


def raise_tool_error(exception: Exception, context: str = "") -> NoReturn:
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
        # Already shaped, by an inner call to this very function. A tool wraps its
        # body in `except Exception` while the helpers it calls shape their own
        # failures, so one failure arrives here twice: once as the domain
        # exception, then again as the ToolError that produced. 16 of the 18 tool
        # catch sites are like that; `search_people` and `search_posts` already
        # guard with their own `except ToolError` (`tools/person.py:175`,
        # `tools/post.py:106`), which is the same conclusion reached one tool at a
        # time.
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
        raise safe from None

    if isinstance(exception, CredentialsNotFoundError):
        logger.warning("Credentials not found%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Authentication not found. Run with --login to create a browser profile.",
            context=context,
        )

    elif isinstance(exception, BrowserSetupInProgressError):
        logger.info("Browser setup in progress%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, BrowserSetupFailedError):
        logger.warning("Browser setup failed%s: %s", ctx, exception)
        raise ToolError(
            "LinkedIn browser setup was not ready. A fresh setup attempt has started in the background. Retry this tool in a few minutes."
        ) from exception

    elif isinstance(exception, AuthenticationStartedError):
        logger.info("Authentication started%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, AuthenticationInProgressError):
        logger.info("Authentication in progress%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, AuthenticationBootstrapFailedError):
        logger.warning("Authentication bootstrap failed%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    # Not a failure at all: another client signed in while this one was asking
    # to. Diagnostics-free for the same reason as the branches above, and worth a
    # branch of its own so it does not reach the catch-all, where an ordinary
    # RuntimeError is masked into something the user cannot act on.
    elif isinstance(exception, PeerSessionInPlaceError):
        logger.info("Another client already signed in%s", ctx)
        raise ToolError(str(exception)) from exception

    # Its own branch rather than the LinkedInMCPError catch-all, which appends an
    # issue-report template. A shared browser that cannot log in by itself is the
    # designed behaviour, not a bug, and inviting a bug report for it would send
    # users to the tracker for something working as intended. The two
    # Authentication*Error branches above are diagnostics-free for the same
    # reason.
    elif isinstance(exception, OwnerCannotAuthenticateError):
        logger.info("The shared browser needs this client to log in%s", ctx)
        raise ToolError(str(exception)) from exception

    # Contention, not a bug: no issue diagnostics, following RateLimitError.
    elif isinstance(exception, BrowserBusyError):
        logger.info("Shared browser busy%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, DockerHostLoginRequiredError):
        logger.warning("Docker host login required%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, LinuxBrowserDependencyError):
        logger.warning("Linux browser dependency missing%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, BrowserBinaryMissingError):
        logger.warning("Browser binary missing%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    # Diagnostics-free, following BrowserBusyError. A browser refusing a profile
    # a newer one wrote is the guard doing its job, and the message already
    # names both versions and the two ways out. Appending an issue template
    # would send users to the tracker for something working as intended.
    elif isinstance(exception, BrowserDowngradeError):
        logger.warning("Browser older than the profile%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, SessionExpiredError):
        logger.warning("Session expired%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Session expired. Run with --login to create a new browser profile.",
            context=context,
        )

    elif isinstance(exception, AuthenticationError):
        logger.warning("Authentication failed%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Authentication failed. Run with --login to re-authenticate.",
            context=context,
        )

    elif isinstance(exception, RateLimitError):
        wait_time = getattr(exception, "suggested_wait_time", 300)
        logger.warning("Rate limit%s: %s (wait=%ds)", ctx, exception, wait_time)
        raise ToolError(
            f"Rate limit detected. Wait {wait_time} seconds before trying again."
        ) from exception

    elif isinstance(exception, ProfileNotFoundError):
        logger.warning("Profile not found%s: %s", ctx, exception)
        raise ToolError(
            "Profile not found. Check the profile URL is correct."
        ) from exception

    elif isinstance(exception, ElementNotFoundError):
        logger.warning("Element not found%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Element not found. LinkedIn page structure may have changed.",
            context=context,
        )

    elif isinstance(exception, ProxyConnectionError):
        # Ahead of NetworkError, which it subclasses. No issue diagnostics: a
        # proxy that is down or misconfigured is not a bug worth reporting.
        logger.warning("Proxy error%s: %s", ctx, exception)
        raise ToolError(str(exception)) from exception

    elif isinstance(exception, NetworkError):
        logger.warning("Network error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Network error. Check your connection and try again.",
            context=context,
        )

    elif isinstance(exception, ScrapingError):
        logger.warning("Scraping error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            "Scraping failed. LinkedIn page structure may have changed.",
            context=context,
        )

    elif isinstance(exception, (LinkedInScraperException, LinkedInMCPError)):
        # Catch-all for base exception types and any future subclasses
        # without a dedicated handler above. Passes through str(exception).
        logger.warning("LinkedIn error%s: %s", ctx, exception)
        _raise_tool_error_with_diagnostics(
            exception,
            str(exception),
            context=context,
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
