"""Whether the page a navigation ended on is one LinkedIn served."""

import logging
import re
from urllib.parse import urlsplit

from .exceptions import OffLinkedInLandingError

logger = logging.getLogger(__name__)

# linkedin.com and every host under it, matched whole so `evil-linkedin.com`
# and `linkedin.com.evil.test` are not. The same shape `identifiers.py` accepts
# a reference on: the root, `www`, and the locale subdomains that serve a
# profile themselves.
_LINKEDIN_HOST = re.compile(r"^(?:[a-z0-9-]+\.)*linkedin\.com$")


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


def raise_if_off_linkedin(url: object) -> None:
    """Refuse a page LinkedIn did not serve.

    Raises:
        OffLinkedInLandingError: When *url* is not a LinkedIn document.
    """
    if is_linkedin_landing(url):
        return
    landed_on = describe_landing(url)
    logger.warning("Navigation ended off LinkedIn, on %s", landed_on)
    raise OffLinkedInLandingError(landed_on)
