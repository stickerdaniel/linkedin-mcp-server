"""Authentication functions for LinkedIn."""

import asyncio
import logging
from enum import Enum
from urllib.parse import urlparse

from patchright.async_api import Page

from .exceptions import AuthenticationError, NetworkError
from .utils import TIMEOUT_ERRORS

logger = logging.getLogger(__name__)


class AuthBarrierKind(str, Enum):
    """Recoverability of a detected auth barrier.

    CHALLENGE: an interactive prompt that automation (or the user, on the
    next manual login) could plausibly click/verify through -- a security
    checkpoint, email verification, or the saved-account chooser.
    BLOCK: the session is simply not authenticated -- a login wall. Needs a
    full re-login, not a retry.
    """

    CHALLENGE = "challenge"
    BLOCK = "block"


class AuthBarrier(str):
    """A detected auth barrier's diagnostic reason, tagged with a kind.

    Subclasses str so every existing caller that treats the barrier as a
    plain string (logging, page-trace JSON payloads, ``barrier is not
    None`` checks, string formatting) keeps working unchanged. New code can
    additionally read ``.kind`` to distinguish a recoverable challenge from
    a hard block.
    """

    kind: AuthBarrierKind

    def __new__(cls, reason: str, kind: AuthBarrierKind) -> "AuthBarrier":
        obj = super().__new__(cls, reason)
        obj.kind = kind
        return obj


# URL-pattern -> kind. Patterns not listed here (shouldn't happen -- every
# entry in _AUTH_BLOCKER_URL_PATTERNS must have a mapping) default to BLOCK.
_URL_PATTERN_KIND = {
    "/login": AuthBarrierKind.BLOCK,
    "/authwall": AuthBarrierKind.BLOCK,
    "/uas/login": AuthBarrierKind.BLOCK,
    "/checkpoint": AuthBarrierKind.CHALLENGE,
    "/challenge": AuthBarrierKind.CHALLENGE,
    "/uas/consumer-email-challenge": AuthBarrierKind.CHALLENGE,
}
_AUTH_BLOCKER_URL_PATTERNS = tuple(_URL_PATTERN_KIND)
_REMEMBER_ME_CONTAINER_SELECTOR = "#rememberme-div"
_REMEMBER_ME_BUTTON_SELECTOR = "#rememberme-div button"
_SESSION_RESUME_TIMEOUT_MS = 15_000

# A profile page that rendered with essentially no text is a soft-block
# signal distinct from any URL/title-based barrier -- scoped to /in/ URLs
# specifically (the only page family this was confirmed against), so a
# legitimately thin non-profile page doesn't false-positive. Real profiles
# always carry at least a name/headline/location plus UI chrome, well over
# this floor; a genuinely blocked render typically has none of it.
_EMPTY_PROFILE_URL_MARKER = "/in/"
_MIN_PROFILE_MAIN_TEXT_LENGTH = 100


async def is_logged_in(page: Page) -> bool:
    """Check if currently logged in to LinkedIn.

    Uses a locale-independent three-tier strategy:
    1. Fail-fast on auth blocker URLs
    2. Check for structural navigation URLs and the auth cookie
    3. URL-based fallback for authenticated-only pages
    """
    try:
        current_url = page.url

        # A captive portal or lookalike page can reproduce LinkedIn-like nav
        # structure. Authentication proof is valid only on LinkedIn over TLS.
        if not _is_linkedin_url(current_url):
            return False

        # Step 1: Fail-fast on auth blockers
        if _is_auth_blocker_url(current_url):
            return False

        # Step 2: structural selector + cookie check. Never classify a button
        # by localized text ("Home", "Inicio", ...), aria-label verb, or a
        # LinkedIn layout class.
        nav_selectors = ", ".join(
            (
                'nav a[href*="/feed"]',
                'nav a[href*="/mynetwork"]',
                'nav a[href*="/messaging"]',
                'nav a[href*="/notifications"]',
            )
        )
        has_nav_elements = await page.locator(nav_selectors).count() > 0
        current_url = page.url
        if not _is_linkedin_url(current_url) or _is_auth_blocker_url(current_url):
            return False
        usable_cookie = await _has_usable_li_at(page)
        if not usable_cookie:
            return False
        current_url = page.url
        if not _is_linkedin_url(current_url) or _is_auth_blocker_url(current_url):
            return False

        # Step 3: URL fallback
        authenticated_only_paths = [
            "/feed",
            "/mynetwork",
            "/messaging",
            "/notifications",
        ]
        parsed_url = urlparse(current_url)
        is_authenticated_page = any(
            parsed_url.path == path or parsed_url.path.startswith(f"{path}/")
            for path in authenticated_only_paths
        )

        if not is_authenticated_page:
            return has_nav_elements

        if has_nav_elements:
            return True

        # Empty authenticated-only pages are a false positive during cookie
        # bridge recovery. Require some real page content before trusting URL.
        body_text = await page.evaluate("() => document.body?.innerText || ''")
        if not isinstance(body_text, str):
            return False
        current_url = page.url
        return (
            _is_linkedin_url(current_url)
            and not _is_auth_blocker_url(current_url)
            and bool(body_text.strip())
        )
    except TIMEOUT_ERRORS:
        logger.warning(
            "Timeout checking login status on %s — treating as not logged in",
            page.url,
        )
        return False
    except Exception:
        logger.error("Unexpected error checking login status", exc_info=True)
        raise


async def detect_auth_barrier(page: Page) -> AuthBarrier | None:
    """Detect LinkedIn auth/account-picker barriers on the current page."""
    return await _detect_auth_barrier(page, include_body_text=True)


async def _detect_auth_barrier(
    page: Page,
    *,
    include_body_text: bool,
) -> AuthBarrier | None:
    """Detect LinkedIn auth/account-picker barriers on the current page."""
    try:
        current_url = page.url
        matched_pattern = _matched_auth_blocker_pattern(current_url)
        if matched_pattern is not None:
            return AuthBarrier(
                f"auth blocker URL: {current_url}",
                _URL_PATTERN_KIND.get(matched_pattern, AuthBarrierKind.BLOCK),
            )

        # Authenticated tools must never accept LinkedIn's logged-out preview
        # pages as valid profile content.  Cookie presence is locale-independent
        # and catches the case where LinkedIn expires ``li_at`` without changing
        # the requested /in/... URL or using a login-page title.
        if _is_linkedin_url(current_url):
            usable_cookie = await _has_usable_li_at(page)
            latest_url = page.url
            if not _is_linkedin_url(latest_url):
                raise NetworkError(
                    "Auth barrier check was redirected outside the main "
                    f"LinkedIn origin: {latest_url}"
                )
            latest_pattern = _matched_auth_blocker_pattern(latest_url)
            if latest_pattern is not None:
                return AuthBarrier(
                    f"auth blocker URL: {latest_url}",
                    _URL_PATTERN_KIND.get(latest_pattern, AuthBarrierKind.BLOCK),
                )
            if not usable_cookie:
                return AuthBarrier(
                    "authenticated LinkedIn page has no usable li_at cookie",
                    AuthBarrierKind.BLOCK,
                )

        # ``include_body_text`` remains in the private signature for callers
        # that distinguish quick/full checks, but barrier classification is
        # intentionally URL/cookie/structure-only and locale-independent.
        del include_body_text
        return None
    except NetworkError:
        raise
    except TIMEOUT_ERRORS:
        logger.warning(
            "Timeout checking auth barrier on %s — continuing without barrier detection",
            page.url,
        )
        return None
    except Exception:
        logger.error("Unexpected error checking auth barrier", exc_info=True)
        return None


async def detect_auth_barrier_quick(page: Page) -> AuthBarrier | None:
    """Cheap auth-barrier check for normal navigations.

    Uses URL and title only, avoiding a full body-text fetch on healthy pages.
    """
    return await _detect_auth_barrier(page, include_body_text=False)


async def detect_empty_profile_barrier(page: Page, url: str) -> AuthBarrier | None:
    """Detect a profile page that rendered with no real content.

    LinkedIn can silently soft-block a request without any URL redirect or
    login-wall marker at all: the navigation succeeds, ``<main>`` exists,
    but it holds essentially no text -- nothing extractable. Confirmed live
    this doesn't overlap with the "logged-out preview" case (that page has
    *plenty* of boilerplate text, just untrustworthy content -- see
    ``normalizer.py``'s degraded-snapshot detection in the lynk-os-data
    pipeline); this catches the genuinely blank-render case instead.

    Deliberately NOT folded into ``detect_auth_barrier_quick`` (URL/title
    only, called on every navigation) since this needs a body-text read --
    call it from a scraping call site after the page has had its normal
    chance to settle, not from the cheap fail-fast check.

    ``url`` is the caller's own requested/target URL, NOT re-read from
    ``page.url`` -- matches every other page-type check in
    ``_extract_loaded_section`` (``is_activity``/``is_search``/``is_details``,
    all keyed off the same parameter), and avoids trusting mutable browser
    state that a redirect (or a test double) could leave out of sync with
    what's actually being extracted.
    """
    try:
        if _EMPTY_PROFILE_URL_MARKER not in url:
            return None

        try:
            main_text = await page.locator("main").inner_text(timeout=1000)
        except TIMEOUT_ERRORS:
            # <main> never appeared -- inconclusive, not necessarily a
            # block (see _extract_loaded_section's own tolerant wait for
            # the same element). Don't false-positive on a timing issue
            # already tolerated elsewhere.
            return None

        if len(main_text.strip()) >= _MIN_PROFILE_MAIN_TEXT_LENGTH:
            return None

        return AuthBarrier(
            f"empty profile page (main text length={len(main_text.strip())}): {url}",
            AuthBarrierKind.CHALLENGE,
        )
    except TIMEOUT_ERRORS:
        return None
    except Exception:
        logger.error("Unexpected error checking empty-profile barrier", exc_info=True)
        return None


async def resolve_remember_me_prompt(page: Page) -> bool:
    """Click through LinkedIn's saved-account chooser when it appears."""
    try:
        if not _is_linkedin_url(page.url):
            logger.debug("Ignoring remember-me prompt outside LinkedIn: %s", page.url)
            return False
        logger.debug("Checking remember-me prompt on %s", page.url)
        try:
            await page.wait_for_selector(_REMEMBER_ME_CONTAINER_SELECTOR, timeout=3000)
            logger.debug("Remember-me container appeared")
        except TIMEOUT_ERRORS:
            logger.debug("Remember-me container did not appear in time")
            return False
        if not _is_linkedin_url(page.url):
            logger.warning(
                "Remember-me wait redirected outside LinkedIn; refusing DOM access: %s",
                page.url,
            )
            return False

        target_locator = page.locator(_REMEMBER_ME_BUTTON_SELECTOR)
        target = target_locator.first
        try:
            target_count = await target_locator.count()
        except Exception:
            logger.debug(
                "Could not count remember-me buttons; continuing with first match",
                exc_info=True,
            )
            target_count = -1
        if not _is_linkedin_url(page.url):
            logger.warning(
                "Remember-me count redirected outside LinkedIn; refusing click: %s",
                page.url,
            )
            return False
        logger.debug(
            "Remember-me target count for %s: %d",
            _REMEMBER_ME_BUTTON_SELECTOR,
            target_count,
        )
        if target_count == 0:
            logger.debug(
                "Remember-me container appeared without any matching button selector"
            )
            return False
        try:
            await target.wait_for(state="visible", timeout=3000)
            logger.debug("Remember-me button became visible")
        except TIMEOUT_ERRORS:
            logger.debug(
                "Remember-me prompt container appeared without a visible login button"
            )
            return False
        if not _is_linkedin_url(page.url):
            logger.warning(
                "Remember-me visibility wait redirected outside LinkedIn; "
                "refusing click: %s",
                page.url,
            )
            return False

        logger.info("Clicking LinkedIn saved-account chooser to resume session")
        try:
            await target.scroll_into_view_if_needed(timeout=3000)
        except TIMEOUT_ERRORS:
            logger.debug("Remember-me button did not scroll into view in time")
        if not _is_linkedin_url(page.url):
            logger.warning(
                "Remember-me scroll redirected outside LinkedIn; refusing click: %s",
                page.url,
            )
            return False

        try:
            await target.click(timeout=5000)
            logger.debug("Remember-me button click succeeded")
        except TIMEOUT_ERRORS:
            if not _is_linkedin_url(page.url):
                logger.warning(
                    "Remember-me click redirected outside LinkedIn; refusing retry: %s",
                    page.url,
                )
                return False
            logger.debug("Retrying remember-me prompt click with force=True")
            await target.click(timeout=5000, force=True)
            logger.debug("Remember-me button force-click succeeded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except TIMEOUT_ERRORS:
            logger.debug("Remember-me prompt click did not finish loading in time")
        await asyncio.sleep(1)
        return _is_linkedin_url(page.url)
    except TIMEOUT_ERRORS:
        logger.debug("Remember-me prompt was present but not clickable in time")
        return False
    except Exception:
        logger.debug("Failed to resolve remember-me prompt", exc_info=True)
        return False


async def wait_for_session_resume_redirect(page: Page) -> bool:
    """Give LinkedIn's cookie-backed login interstitial one bounded chance.

    Some cold sessions briefly land on ``/login/?session_redirect=...`` even
    though a non-empty ``li_at`` is still installed.  The page can resume the
    account automatically without exposing a clickable control.  Wait only
    when that locale-independent cookie signal exists, then let the caller
    retry its target navigation once.  A genuinely logged-out page has no
    usable cookie and therefore fails immediately.
    """
    if _matched_auth_blocker_pattern(page.url) != "/login":
        return False
    if await _has_usable_li_at(page) is not True:
        return False

    logger.info("Waiting briefly for LinkedIn session-resume redirect")
    try:
        await page.wait_for_url(
            lambda url: _matched_auth_blocker_pattern(str(url)) is None,
            wait_until="domcontentloaded",
            timeout=_SESSION_RESUME_TIMEOUT_MS,
        )
    except TIMEOUT_ERRORS:
        # The interstitial sometimes updates the server-side session without
        # navigating itself.  Return True so the caller performs one explicit
        # target retry after the bounded wait.
        logger.debug("Session-resume page did not redirect within the wait budget")
    return True


def _is_linkedin_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme.lower() == "https"
        and port in {None, 443}
        and _is_linkedin_hostname(parsed.hostname)
    )


def _is_linkedin_hostname(hostname: str | None) -> bool:
    """Match the main LinkedIn web origin used by every authenticated tool."""
    hostname = (hostname or "").lower().rstrip(".")
    return hostname in {"linkedin.com", "www.linkedin.com"}


async def _has_usable_li_at(page: Page) -> bool:
    """Return auth-cookie presence; preserve driver failures as inconclusive."""
    try:
        cookies = await page.context.cookies("https://www.linkedin.com")
    except Exception as exc:
        raise NetworkError(
            "Browser driver could not inspect the LinkedIn authentication cookie"
        ) from exc
    return any(
        cookie.get("name") == "li_at"
        and isinstance(cookie.get("value"), str)
        and bool(cookie["value"].strip())
        for cookie in cookies
    )


def _matched_auth_blocker_pattern(url: str) -> str | None:
    """Return the matched auth-blocker URL pattern, or None.

    Matches real auth routes only on LinkedIn origins, not arbitrary slug
    substrings or a captive portal that happens to redirect to ``/login``.
    """
    if not _is_linkedin_url(url):
        return None
    parsed = urlparse(url)
    path = parsed.path or "/"

    if path in _AUTH_BLOCKER_URL_PATTERNS:
        return path

    for pattern in _AUTH_BLOCKER_URL_PATTERNS:
        if path == f"{pattern}/" or path.startswith(f"{pattern}/"):
            return pattern
    return None


def _is_auth_blocker_url(url: str) -> bool:
    """Return True only for real auth routes, not arbitrary slug substrings."""
    return _matched_auth_blocker_pattern(url) is not None


async def wait_for_manual_login(page: Page, timeout: int = 300000) -> None:
    """Wait for user to manually complete login.

    Args:
        page: Patchright page object
        timeout: Timeout in milliseconds. ``0`` waits with no time limit.

    Raises:
        AuthenticationError: If the timeout elapses before login completes.
    """
    minutes = timeout / 60000
    if timeout:
        logger.info(
            "Please complete the login process manually in the browser. "
            "Waiting up to %.0f minutes...",
            minutes,
        )
    else:
        logger.info(
            "Please complete the login process manually in the browser. "
            "Waiting with no time limit (LOGIN_TIMEOUT=0)..."
        )

    def _timeout_error() -> AuthenticationError:
        return AuthenticationError(
            f"Manual login timeout: login was not completed within {minutes:.0f} "
            "minutes. Increase the limit with LOGIN_TIMEOUT (seconds, 0 = no "
            "limit) and run --login again."
        )

    loop = asyncio.get_running_loop()
    start_time = loop.time()

    while True:
        if await resolve_remember_me_prompt(page):
            logger.info("Resolved saved-account chooser during manual login flow")
            elapsed = (loop.time() - start_time) * 1000
            if timeout and elapsed > timeout:
                raise _timeout_error()
            continue

        if await is_logged_in(page):
            logger.info("Manual login completed successfully")
            return

        elapsed = (loop.time() - start_time) * 1000
        if timeout and elapsed > timeout:
            raise _timeout_error()

        await asyncio.sleep(1)
