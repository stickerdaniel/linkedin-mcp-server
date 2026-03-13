"""Authentication functions for LinkedIn."""

import asyncio
import logging
import re
from urllib.parse import urlparse

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)

_AUTH_BLOCKER_URL_PATTERNS = (
    "/login",
    "/authwall",
    "/checkpoint",
    "/challenge",
    "/uas/login",
    "/uas/consumer-email-challenge",
)
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
_REMEMBER_ME_BUTTON_SELECTOR = (
    "#rememberme-div > div.memberList-container > div > div > "
    "div.member-profile-container.list-box > div.member-profile-block > button"
)


async def warm_up_browser(page: Page) -> None:
    """Visit normal sites to appear more human-like before LinkedIn access."""
    sites = [
        "https://www.google.com",
        "https://www.wikipedia.org",
        "https://www.github.com",
    ]

    logger.info("Warming up browser by visiting normal sites...")

    failures = 0
    for site in sites:
        try:
            await page.goto(site, wait_until="domcontentloaded", timeout=10000)
            await asyncio.sleep(1)
            logger.debug("Visited %s", site)
        except Exception as e:
            failures += 1
            logger.debug("Could not visit %s: %s", site, e)
            continue

    if failures == len(sites):
        logger.warning("Browser warm-up failed: none of %d sites reachable", len(sites))
    else:
        logger.info("Browser warm-up complete")


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
    except PlaywrightTimeoutError:
        logger.warning(
            "Timeout checking login status on %s — treating as not logged in",
            page.url,
        )
        return False
    except Exception:
        logger.error("Unexpected error checking login status", exc_info=True)
        raise


async def detect_auth_barrier(page: Page) -> str | None:
    """Detect LinkedIn auth/account-picker barriers on the current page."""
    return await _detect_auth_barrier(page, include_body_text=True)


async def _detect_auth_barrier(
    page: Page,
    *,
    include_body_text: bool,
) -> str | None:
    """Detect LinkedIn auth/account-picker barriers on the current page."""
    try:
        current_url = page.url
        if _is_auth_blocker_url(current_url):
            return f"auth blocker URL: {current_url}"

        try:
            title = (await page.title()).strip().lower()
        except Exception:
            title = ""
        if any(pattern in title for pattern in _LOGIN_TITLE_PATTERNS):
            return f"login title: {title}"

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
                return f"auth barrier text: {' + '.join(marker_group)}"

        return None
    except PlaywrightTimeoutError:
        logger.warning(
            "Timeout checking auth barrier on %s — continuing without barrier detection",
            page.url,
        )
        return None
    except Exception:
        logger.error("Unexpected error checking auth barrier", exc_info=True)
        return None


async def detect_auth_barrier_quick(page: Page) -> str | None:
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
        except PlaywrightTimeoutError:
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
        try:
            await target.wait_for(state="visible", timeout=3000)
            logger.debug("Remember-me button became visible")
        except PlaywrightTimeoutError:
            logger.debug(
                "Remember-me prompt container appeared without a visible login button"
            )
            return False

        logger.info("Clicking LinkedIn saved-account chooser to resume session")
        try:
            await target.scroll_into_view_if_needed(timeout=3000)
        except PlaywrightTimeoutError:
            logger.debug("Remember-me button did not scroll into view in time")

        try:
            await target.click(timeout=5000)
            logger.debug("Remember-me button click succeeded")
        except PlaywrightTimeoutError:
            logger.debug("Retrying remember-me prompt click with force=True")
            await target.click(timeout=5000, force=True)
            logger.debug("Remember-me button force-click succeeded")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeoutError:
            logger.debug("Remember-me prompt click did not finish loading in time")
        await asyncio.sleep(1)
        return True
    except PlaywrightTimeoutError:
        logger.debug("Remember-me prompt was present but not clickable in time")
        return False
    except Exception:
        logger.debug("Failed to resolve remember-me prompt", exc_info=True)
        return False


def _is_auth_blocker_url(url: str) -> bool:
    """Return True only for real auth routes, not arbitrary slug substrings."""
    path = urlparse(url).path or "/"

    if path in _AUTH_BLOCKER_URL_PATTERNS:
        return True

    return any(
        path == f"{pattern}/" or path.startswith(f"{pattern}/")
        for pattern in _AUTH_BLOCKER_URL_PATTERNS
    )


async def wait_for_manual_login(page: Page, timeout: int = 300000) -> None:
    """Wait for user to manually complete login.

    Args:
        page: Patchright page object
        timeout: Timeout in milliseconds (default: 5 minutes)

    Raises:
        AuthenticationError: If timeout or login not completed
    """
    logger.info(
        "Please complete the login process manually in the browser. "
        "Waiting up to 5 minutes..."
    )

    loop = asyncio.get_running_loop()
    start_time = loop.time()

    while True:
        if await resolve_remember_me_prompt(page):
            logger.info("Resolved saved-account chooser during manual login flow")
            elapsed = (loop.time() - start_time) * 1000
            if elapsed > timeout:
                raise AuthenticationError(
                    "Manual login timeout. Please try again and complete login faster."
                )
            continue

        if await is_logged_in(page):
            logger.info("Manual login completed successfully")
            return

        elapsed = (loop.time() - start_time) * 1000
        if elapsed > timeout:
            raise AuthenticationError(
                "Manual login timeout. Please try again and complete login faster."
            )

        await asyncio.sleep(1)
