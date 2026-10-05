"""Recognition and safe reporting of proxy failures.

A misconfigured or unreachable proxy does not fail at browser launch. Chromium
starts normally and the failure lands on the first navigation, where the auth
checks read it as a dead session. These helpers let those call sites tell the
two apart before they draw that conclusion.
"""

import asyncio
import logging
from typing import Any
from urllib.parse import quote

from linkedin_mcp_server.config.schema import BrowserConfig

from .exceptions import ProxyConnectionError

logger = logging.getLogger(__name__)


def _browser_config() -> BrowserConfig:
    """Return the active browser config, or defaults if it cannot be read.

    These helpers run on the failure path, so they must never raise on their
    own. Falling back to an unconfigured instance costs only the proxy address
    in the message; it keeps the original error from being replaced by whatever
    went wrong while reporting it.

    """
    try:
        from linkedin_mcp_server.config import get_config

        return get_config().browser
    except Exception:
        return BrowserConfig()


# Chromium network-stack errors that mean the proxy itself is the problem, not
# LinkedIn and not the stored session. Matched case-insensitively as substrings,
# mirroring the marker-list helpers in linkedin_mcp_server.dependencies.
PROXY_ERROR_MARKERS = (
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_proxy_auth_requested",
    "err_proxy_certificate_invalid",
    "err_unexpected_proxy_auth",
    "err_socks_connection_failed",
    "err_socks_connection_host_unreachable",
    "err_no_supported_proxies",
    "err_mandatory_proxy_configuration_failed",
    "err_proxy_unable_to_connect_to_destination",
)

# Rejected credentials. Kept apart because the code does not name the proxy:
# it can equally mean a site's own HTTP auth failed, so it only counts when a
# proxy is actually configured.
AMBIGUOUS_AUTH_MARKERS = ("err_invalid_auth_credentials",)


def is_proxy_error(error: BaseException) -> bool:
    """Return whether *error* reports a failure of the configured proxy."""
    if isinstance(error, ProxyConnectionError):
        return True
    message = str(error).lower()
    if any(marker in message for marker in PROXY_ERROR_MARKERS):
        return True
    return bool(_browser_config().proxy_server) and any(
        marker in message for marker in AMBIGUOUS_AUTH_MARKERS
    )


def redact_proxy_credentials(message: str) -> str:
    """Strip the configured proxy credentials from *message*.

    Error text from the driver can quote the proxy URL, and the top-level
    handlers log exceptions with their full cause chain. The username is masked
    alongside the password because residential providers encode the account,
    zone and session in it. Percent-encoded forms are covered too, since that is
    how credentials appear inside a URL.
    """
    config = _browser_config()
    for secret in (config.proxy_password, config.proxy_username):
        if not secret:
            continue
        for variant in (secret, quote(secret, safe="")):
            message = message.replace(variant, "***")
    return message


def as_proxy_error(error: BaseException) -> ProxyConnectionError:
    """Convert *error* into a credential-free :class:`ProxyConnectionError`.

    The original exception is deliberately not chained: the top-level handlers
    call ``logger.exception``, which prints the whole cause chain and would put
    the raw driver message -- possibly including the proxy URL -- back into the
    log this redaction exists to keep clean.
    """
    if isinstance(error, ProxyConnectionError):
        return error
    server = _browser_config().proxy_server or "the configured proxy"
    detail = redact_proxy_credentials(str(error))
    return ProxyConnectionError(
        f"Could not reach LinkedIn through proxy {server}: {detail}. "
        "Check that the proxy is running and that its address and credentials "
        "are correct. The saved LinkedIn session was not changed."
    )


def raise_if_proxy_error(error: BaseException) -> None:
    """Re-raise *error* as a :class:`ProxyConnectionError` when it is one."""
    if is_proxy_error(error):
        raise as_proxy_error(error) from None


def redacted_copy(error: Exception) -> Exception:
    """Return *error* with the proxy credentials stripped from its message.

    For re-raising across a boundary that logs exceptions. The type is
    preserved so callers branching on it are unaffected; only the message is
    rewritten. Exceptions whose constructor takes more than a message are
    returned unchanged rather than being rebuilt wrongly -- losing the
    redaction is better than losing the error.
    """
    message = str(error)
    redacted = redact_proxy_credentials(message)
    if redacted == message:
        return error
    try:
        return type(error)(redacted)
    except Exception:
        return ProxyConnectionError(redacted)


def raise_if_proxy_configured(error: BaseException) -> None:
    """Re-raise a failed navigation as a proxy fault when a proxy is in use.

    For callers that would otherwise read a navigation failure as a dead
    session. Not every proxy failure identifies itself: wrong credentials
    produce a plain timeout, because Chromium retries the 407 challenge until
    the navigation expires. Attributing an unexplained failure to the proxy is
    the safe reading -- it leaves the stored session alone, where the opposite
    mistake discards a working profile and reruns login through the same broken
    proxy.
    """
    if not _browser_config().proxy_server:
        return
    if is_proxy_error(error):
        raise as_proxy_error(error) from None
    server = _browser_config().proxy_server
    detail = redact_proxy_credentials(str(error))
    raise ProxyConnectionError(
        f"LinkedIn could not be reached through proxy {server}: {detail}. "
        "Wrong proxy credentials look exactly like this, because the browser "
        "retries the challenge until the page times out. Check the proxy "
        "address and credentials. The saved LinkedIn session was not changed."
    ) from None


#: The product's navigation budget, in milliseconds, counted from the moment
#: the browser sends the navigation request. Patchright's own default is the
#: same number but starts at the ``goto`` call. On a Windows runner the first
#: request of a fresh browser is not sent for seconds after that call (1.1s to
#: 2.5s on a green idle leg; a loaded one never sent it inside 30s), so a clock
#: that starts at the call expires before the origin has anything to answer.
#: A held request is inside this budget: the gate's 20s deadline leaves the
#: answer ten seconds, and the relay drops a silent tunnel at 30s.
NAVIGATION_BUDGET_MS = 30_000


def _page_reports_requests(page: Any) -> bool:
    """Whether *page* is a real page, not a stand-in whose ``on`` is invented.

    A mock grows ``on`` the moment it is asked for, and treating that as a
    listener would wait for a request the stand-in never emits. A method that
    exists on the class is one the page actually has.
    """
    kind = type(page)
    return callable(getattr(kind, "on", None)) and callable(
        getattr(kind, "remove_listener", None)
    )


def _is_main_frame_navigation(page: Any, request: Any) -> bool:
    """Whether *request* is the navigation ``goto`` is waiting on.

    A subresource, or a frame that is not the page's main one, must not start
    the budget: the answer the gate holds is the main document.
    """
    is_navigation = getattr(request, "is_navigation_request", None)
    if callable(is_navigation) and not is_navigation():
        return False
    frame = getattr(request, "frame", None)
    main = getattr(page, "main_frame", None)
    return frame is None or main is None or frame is main


async def _stop_goto(goto: asyncio.Future[Any]) -> None:
    """Cancel a ``goto`` that is still running, and wait until it has stopped."""
    if goto.done():
        return
    goto.cancel()
    await asyncio.shield(asyncio.gather(goto, return_exceptions=True))


async def _goto_within_budget(page: Any, url: str, **kwargs: Any) -> Any:
    """``page.goto``, with :data:`NAVIGATION_BUDGET_MS` starting at the request.

    A caller that passes ``timeout`` sets that budget. ``0`` keeps Patchright's
    meaning, no limit. A page that cannot report its requests is unchanged:
    the driver's own clock applies, which is the only clock it has.
    """
    caller_set_timeout = "timeout" in kwargs
    timeout = kwargs.pop("timeout", None)
    if not _page_reports_requests(page):
        if caller_set_timeout:
            kwargs["timeout"] = timeout
        return await page.goto(url, **kwargs)
    if timeout is None:
        timeout = NAVIGATION_BUDGET_MS
    if timeout == 0:
        return await page.goto(url, timeout=0, **kwargs)

    sent: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def on_request(request: Any) -> None:
        if sent.done() or not _is_main_frame_navigation(page, request):
            return
        sent.set_result(None)

    page.on("request", on_request)
    # ``timeout=0`` turns the driver's clock off. It would otherwise include
    # the time before this browser sends anything, which is not the navigation.
    goto = asyncio.ensure_future(page.goto(url, timeout=0, **kwargs))
    try:
        while not goto.done() and not sent.done():
            await asyncio.wait({goto, sent}, return_when=asyncio.FIRST_COMPLETED)
        if goto.done():
            return await goto
        try:
            return await asyncio.wait_for(asyncio.shield(goto), timeout / 1000)
        except TimeoutError:
            await _stop_goto(goto)
            raise TimeoutError(
                f"Page.goto: Timeout {timeout:g}ms exceeded after the request was sent."
            ) from None
    finally:
        page.remove_listener("request", on_request)
        await _stop_goto(goto)


async def goto_reporting_proxy_errors(page: Any, url: str, **kwargs: Any) -> Any:
    """``page.goto(url)``, reporting a proxy fault as :class:`ProxyConnectionError`.

    A shared wrapper rather than a check at each call site: the navigations that
    happen before any auth check (manual login, cookie-import validation, the
    runtime bridge) would otherwise surface the raw driver error, which reads
    like a LinkedIn problem and can carry the proxy URL into a log.

    The navigation budget starts when the browser sends the request, not when
    this function is called. See :data:`NAVIGATION_BUDGET_MS`.
    """
    try:
        return await _goto_within_budget(page, url, **kwargs)
    except Exception as exc:
        raise_if_proxy_error(exc)
        raise


def proxy_hint() -> str:
    """Return a suffix naming the proxy as a possible cause, or an empty string.

    Not every proxy failure is identifiable. A wrong password in particular
    produces no proxy error code at all: Chromium retries the 407 challenge
    until the navigation times out, which is indistinguishable from a slow page
    (verified against a local authenticating relay). Auth failures therefore
    mention the proxy whenever one is configured, so the advice to log in again
    does not send someone chasing a session problem that is not there.
    """
    server = _browser_config().proxy_server
    if not server:
        return ""
    return (
        f" Traffic is routed through proxy {server}; if it is unreachable or "
        "its credentials are wrong, that looks the same as a failed sign-in."
    )
