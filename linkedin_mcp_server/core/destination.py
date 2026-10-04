"""Whether the page a navigation ended on is one LinkedIn served."""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NoReturn
from urllib.parse import urlsplit

from .exceptions import OffLinkedInLandingError

logger = logging.getLogger(__name__)

# linkedin.com and every host under it, matched whole so `evil-linkedin.com`
# and `linkedin.com.evil.test` are not. The same shape `identifiers.py` accepts
# a reference on: the root, `www`, and the locale subdomains that serve a
# profile themselves.
_LINKEDIN_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")

# The host pattern above, in the one syntax both Python and JavaScript read the
# same way, so the in-page check below cannot drift from it.
LINKEDIN_HOST_PATTERN = _LINKEDIN_HOST.pattern

# `is_linkedin_landing` for a page script, which has to decide inside its own
# evaluation, before it clicks, whether the document it is about to act on is
# LinkedIn's: the address Python saw belongs to a moment that has passed. Called
# as `(href, hostPattern)` with LINKEDIN_HOST_PATTERN. The rules are Python's,
# rule for rule; `tests/test_off_linkedin_landing_dom.py` holds the two to the
# same answers.
LINKEDIN_LANDING_JS = r"""(href, hostPattern) => {
    let url;
    try {
        url = new URL(href);
    } catch {
        return false;
    }
    const host = url.hostname.replace(/\.$/, '');
    return url.protocol === 'https:'
        && (url.port === '' || url.port === '443')
        && url.username === ''
        && url.password === ''
        && new RegExp(hostPattern).test(host);
}"""

# Reads the address of the document an element lives in, so an action is
# judged by the page holding the element rather than the page the driver last
# reported.
_OWNER_DOCUMENT_ADDRESS_JS = "element => element.ownerDocument.location.href"


def is_linkedin_landing(url: object) -> bool:
    """Return whether *url*, a page's address, is a document LinkedIn served.

    A document without a host is never one. `about:blank`, `data:` and
    `chrome-error://` are what an interrupted navigation and an enterprise
    proxy clearing the page leave behind, and calling them LinkedIn's would let
    an empty page pass as a signed-in feed. Nothing here is asked about a
    relative address: a page's own address is always absolute.
    """
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    # A single trailing dot is the fully qualified spelling of the same host.
    host = (parsed.hostname or "").removesuffix(".")
    return (
        parsed.scheme == "https"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and _LINKEDIN_HOST.fullmatch(host) is not None
    )


def is_another_site(url: object) -> bool:
    """Return whether *url* is a web page served by a host other than LinkedIn.

    Narrower than "not a LinkedIn landing": a blank document or the browser's
    own error page is what a failed request leaves behind, and the failure
    that produced it already says more than where it landed.
    """
    if not isinstance(url, str):
        return False
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return False
    return scheme in ("http", "https") and not is_linkedin_landing(url)


def describe_landing(url: object) -> str:
    """Name where a page landed, without its path or query.

    The origin is the useful fact, and the rest of a portal's address can carry
    a token or the whole address it intercepted.
    """
    if not isinstance(url, str) or not url:
        return "an unknown page"
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return "an unreadable address"
    if host:
        origin = f"{parsed.scheme}://{host}"
        return origin if port is None else f"{origin}:{port}"
    if parsed.scheme == "about":
        return f"about:{parsed.path}"[:40]
    if parsed.scheme:
        return f"a {parsed.scheme}: document"
    return "an unknown page"


def refuse_landing(url: object) -> NoReturn:
    """Raise for a page that is not LinkedIn's, whoever decided it.

    Raises:
        OffLinkedInLandingError: Always.
    """
    landed_on = describe_landing(url)
    logger.warning("Navigation ended off LinkedIn, on %s", landed_on)
    raise OffLinkedInLandingError(landed_on)


def raise_if_off_linkedin(url: object) -> None:
    """Refuse a page LinkedIn did not serve.

    Raises:
        OffLinkedInLandingError: When *url* is not a LinkedIn document.
    """
    if not is_linkedin_landing(url):
        refuse_landing(url)


@asynccontextmanager
async def linkedin_element(
    locator: Any, *, timeout: float | None = None
) -> AsyncIterator[Any]:
    """Resolve *locator* to one element, yielded only from a LinkedIn document.

    Act through the yielded handle, never through the locator. A locator
    resolves again for every action, so a redirect after this check would aim
    the click at the new page; a handle belongs to one document, and a
    navigation away fails the action instead of retargeting it.

    Raises:
        OffLinkedInLandingError: When the element's own document is not
            LinkedIn's.
    """
    if timeout is None:
        handle = await locator.element_handle()
    else:
        handle = await locator.element_handle(timeout=timeout)
    try:
        raise_if_off_linkedin(await handle.evaluate(_OWNER_DOCUMENT_ADDRESS_JS))
        yield handle
    finally:
        try:
            await handle.dispose()
        except Exception:
            logger.debug("Could not release an element handle", exc_info=True)
