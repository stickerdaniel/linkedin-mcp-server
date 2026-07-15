"""Authentication functions for LinkedIn."""

import asyncio
import logging
import re
from enum import Enum
from urllib.parse import urlparse

from patchright.async_api import Page

from .exceptions import AuthenticationError
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
_LOGIN_TITLE_PATTERNS = (
    "linkedin login",
    "sign in | linkedin",
)
_AUTH_BARRIER_TEXT_MARKERS = (
    ("welcome back", "sign in using another account"),
    ("welcome back", "join now"),
    ("choose an account", "sign in using another account"),
    ("continue as", "sign in using another account"),
)
_REMEMBER_ME_CONTAINER_SELECTOR = "#rememberme-div"
_REMEMBER_ME_BUTTON_SELECTOR = "#rememberme-div button"


async def is_logged_in(page: Page) -> bool:
    """Check if currently logged in to LinkedIn.

    Uses a three-tier strategy:
    1. Fail-fast on auth blocker URLs
    2. Check for navigation elements (primary)
    3. URL-based fallback for authenticated-only pages
    """
    try:
        current_url = page.url

        # Step 1: Fail-fast on auth blockers
        if _is_auth_blocker_url(current_url):
            return False

        # Step 2: Selector check (PRIMARY)
        old_selectors = '.global-nav__primary-link, [data-control-name="nav.settings"]'
        old_count = await page.locator(old_selectors).count()

        new_selectors = 'nav a[href*="/feed"], nav button:has-text("Home"), nav a[href*="/mynetwork"]'
        new_count = await page.locator(new_selectors).count()

        has_nav_elements = old_count > 0 or new_count > 0

        # Step 3: URL fallback
        authenticated_only_pages = [
            "/feed",
            "/mynetwork",
            "/messaging",
            "/notifications",
        ]
        is_authenticated_page = any(
            pattern in current_url for pattern in authenticated_only_pages
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

        return bool(body_text.strip())
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

        try:
            title = (await page.title()).strip().lower()
        except Exception:
            title = ""
        if any(pattern in title for pattern in _LOGIN_TITLE_PATTERNS):
            return AuthBarrier(f"login title: {title}", AuthBarrierKind.BLOCK)

        if not include_body_text:
            return None

        try:
            body_text = await page.evaluate("() => document.body?.innerText || ''")
        except Exception:
            body_text = ""
        if not isinstance(body_text, str):
            body_text = ""

        normalized = re.sub(r"\s+", " ", body_text).strip().lower()
        for marker_group in _AUTH_BARRIER_TEXT_MARKERS:
            if all(marker in normalized for marker in marker_group):
                return AuthBarrier(
                    f"auth barrier text: {' + '.join(marker_group)}",
                    AuthBarrierKind.CHALLENGE,
                )

        return None
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


async def resolve_remember_me_prompt(page: Page) -> bool:
    """Click through LinkedIn's saved-account chooser when it appears."""
    try:
        logger.debug("Checking remember-me prompt on %s", page.url)
        try:
            await page.wait_for_selector(_REMEMBER_ME_CONTAINER_SELECTOR, timeout=3000)
            logger.debug("Remember-me container appeared")
        except TIMEOUT_ERRORS:
            logger.debug("Remember-me container did not appear in time")
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

        logger.info("Clicking LinkedIn saved-account chooser to resume session")
        try:
            await target.scroll_into_view_if_needed(timeout=3000)
        except TIMEOUT_ERRORS:
            logger.debug("Remember-me button did not scroll into view in time")

        try:
            await target.click(timeout=5000)
            logger.debug("Remember-me button click succeeded")
        except TIMEOUT_ERRORS:
            logger.debug("Retrying remember-me prompt click with force=True")
            await target.click(timeout=5000, force=True)
            logger.debug("Remember-me button force-click succeeded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except TIMEOUT_ERRORS:
            logger.debug("Remember-me prompt click did not finish loading in time")
        await asyncio.sleep(1)
        return True
    except TIMEOUT_ERRORS:
        logger.debug("Remember-me prompt was present but not clickable in time")
        return False
    except Exception:
        logger.debug("Failed to resolve remember-me prompt", exc_info=True)
        return False


def _matched_auth_blocker_pattern(url: str) -> str | None:
    """Return the matched auth-blocker URL pattern, or None.

    Matches real auth routes only, not arbitrary slug substrings.
    """
    path = urlparse(url).path or "/"

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
