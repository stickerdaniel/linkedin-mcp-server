"""Browser-DOM tests for the contact-overlay read in ``scraping/capture.py``.

The unit suite mocks ``page.evaluate``, so whether a root matched is whatever
the double says. These cases run the production overlay read, the shared
reader's JavaScript and the noise and reference processing in headless
chromium, so the root decision is the browser's own.

Every document here is synthetic, so each case is a claim about the algorithm
and never about LinkedIn's markup. Requests other than the synthetic document
are aborted; nothing reaches LinkedIn. Skipped automatically when chromium is
not installed; run locally after ``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from patchright.async_api import async_playwright

from linkedin_mcp_server.scraping.capture import (
    OverlayRootNotFoundError,
    SectionCapture,
)
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

#: Keep every test that launches Chromium on one worker; see
#: ``test_root_content_dom.py``.
pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

OVERLAY_URL = "https://www.linkedin.com/in/ada-lovelace/overlay/contact-info/"
OVERLAY_WAIT = "dialog[open], .artdeco-modal__content"

MAIN = '<main>Ada Lovelace Analyst <a href="/in/main-only/">Main profile</a></main>'
DIALOG = (
    "<dialog open>Email ada@example.com "
    '<a href="/in/overlay-only/">Overlay profile</a></dialog>'
)
LEGACY = (
    '<div class="artdeco-modal__content">Email legacy@example.com '
    '<a href="/in/legacy-only/">Legacy profile</a></div>'
)


@pytest.fixture
async def dom_page():
    """Real chromium page in a fresh context, or skip when none is installed.

    Only launch/setup is guarded by the skip, so an assertion failure in a
    test body is never swallowed into one. The short default timeout bounds
    every overlay wait that is meant to time out.
    """
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        page.set_default_timeout(1500)
        try:
            yield page
        finally:
            await browser.close()


async def load(page: Any, body: str) -> SectionCapture:
    """Serve `body` at the overlay address and wire capture to that page.

    The address is a real origin so relative anchors resolve the way a caller
    would receive them. Every other request is aborted.
    """
    html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f"<title>Contact</title></head><body>{body}</body></html>"
    )

    async def handle(route: Any) -> None:
        if route.request.url == OVERLAY_URL:
            await route.fulfill(content_type="text/html", body=html)
        else:
            await route.abort()

    await page.route("**/*", handle)
    await page.goto(OVERLAY_URL)
    session = ScrapingSession(page)
    return SectionCapture(session, PageNavigator(session), PageContentReader(session))


def urls(result: Any) -> list[str]:
    return [reference["url"] for reference in result.references]


class TestAcceptedRoots:
    async def test_an_open_dialog_is_read_instead_of_main(self, dom_page):
        capture = await load(dom_page, MAIN + DIALOG)

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert result.text == "Email ada@example.com Overlay profile"
        assert urls(result) == ["/in/overlay-only/"]

    async def test_an_empty_open_dialog_is_empty_without_an_error(self, dom_page):
        capture = await load(dom_page, MAIN + "<dialog open></dialog>")

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert result.text == ""
        assert result.references == []
        assert result.error is None

    async def test_the_legacy_modal_root_is_still_read(self, dom_page):
        capture = await load(dom_page, MAIN + LEGACY)

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert result.text == "Email legacy@example.com Legacy profile"
        assert urls(result) == ["/in/legacy-only/"]

    @pytest.mark.parametrize(
        "body",
        [MAIN + LEGACY + DIALOG, MAIN + DIALOG + LEGACY],
        ids=["legacy-first", "dialog-first"],
    )
    async def test_an_open_dialog_wins_over_the_legacy_root(self, dom_page, body):
        capture = await load(dom_page, body)

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert result.text == "Email ada@example.com Overlay profile"
        assert urls(result) == ["/in/overlay-only/"]


class TestMissingRoot:
    @pytest.mark.parametrize(
        "body",
        [
            MAIN,
            '<p>Ada Lovelace Analyst</p><a href="/in/someone-else/">Someone</a>',
            "<dialog>Email hidden@example.com</dialog>" + MAIN,
        ],
        ids=["main-only", "body-only", "closed-dialog"],
    )
    async def test_no_overlay_root_is_an_error_and_never_page_content(
        self, dom_page, body
    ):
        capture = await load(dom_page, body)

        with pytest.raises(OverlayRootNotFoundError, match="contact_info"):
            await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

    async def test_navigation_ahead_of_a_noise_only_main_is_the_throttle_sentinel(
        self, dom_page
    ):
        capture = await load(
            dom_page,
            "<nav>Home Network</nav>"
            "<main>More profiles for you<br>About<br>Accessibility</main>",
        )

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert result.text == RATE_LIMITED_SECTION_TEXT
        assert result.references == []

    async def test_a_noise_only_page_without_main_is_the_throttle_sentinel(
        self, dom_page
    ):
        capture = await load(
            dom_page, "<p>More profiles for you<br>About<br>Accessibility</p>"
        )

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert result.text == RATE_LIMITED_SECTION_TEXT
        assert result.references == []

    async def test_noise_ahead_of_a_substantive_main_is_not_a_throttle(self, dom_page):
        capture = await load(
            dom_page,
            "<p>More profiles for you</p><main>Ada Lovelace<br>Analyst</main>",
        )

        with pytest.raises(OverlayRootNotFoundError):
            await capture._extract_overlay_content(OVERLAY_URL, "contact_info")


class TestWaitIsNotAuthority:
    async def test_a_dialog_that_opens_during_the_wait_is_read(
        self, dom_page, monkeypatch
    ):
        capture = await load(dom_page, MAIN)
        entered_wait = asyncio.Event()
        real_wait = dom_page.wait_for_selector

        async def observed_wait(selector, **kwargs):
            assert selector == OVERLAY_WAIT
            entered_wait.set()
            return await real_wait(selector, **kwargs)

        monkeypatch.setattr(dom_page, "wait_for_selector", observed_wait)
        read_task = asyncio.create_task(
            capture._extract_overlay_content(OVERLAY_URL, "contact_info")
        )
        entry_task = asyncio.create_task(entered_wait.wait())
        try:
            await asyncio.wait(
                {read_task, entry_task},
                timeout=3,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert entered_wait.is_set(), "extraction bypassed the overlay wait"
            await dom_page.evaluate(
                """() => document.body.insertAdjacentHTML(
                    'beforeend',
                    '<dialog open>Email ada@example.com '
                        + '<a href="/in/overlay-only/">Overlay profile</a></dialog>'
                )"""
            )
            result = await asyncio.wait_for(read_task, timeout=3)
            assert result.text == "Email ada@example.com Overlay profile"
            assert urls(result) == ["/in/overlay-only/"]
        finally:
            for task in (read_task, entry_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(read_task, entry_task, return_exceptions=True)

    async def test_a_dialog_gone_after_a_successful_wait_is_missing(
        self, dom_page, monkeypatch
    ):
        capture = await load(dom_page, MAIN + DIALOG)
        real_wait = dom_page.wait_for_selector

        async def wait_then_close(selector, **kwargs):
            found = await real_wait(selector, **kwargs)
            await dom_page.evaluate("() => document.querySelector('dialog').remove()")
            return found

        monkeypatch.setattr(dom_page, "wait_for_selector", wait_then_close)

        with pytest.raises(OverlayRootNotFoundError):
            await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

    async def test_a_dialog_that_opens_after_a_timed_out_wait_is_read(
        self, dom_page, monkeypatch
    ):
        capture = await load(dom_page, MAIN)
        real_wait = dom_page.wait_for_selector
        timeouts: list[PlaywrightTimeoutError] = []

        async def time_out_then_open(selector, **kwargs):
            try:
                return await real_wait(selector, timeout=100)
            except PlaywrightTimeoutError as exc:
                timeouts.append(exc)
                raise
            finally:
                await dom_page.evaluate(
                    "html => document.body.insertAdjacentHTML('beforeend', html)",
                    DIALOG,
                )

        monkeypatch.setattr(dom_page, "wait_for_selector", time_out_then_open)

        result = await capture._extract_overlay_content(OVERLAY_URL, "contact_info")

        assert len(timeouts) == 1
        assert result.text == "Email ada@example.com Overlay profile"
        assert urls(result) == ["/in/overlay-only/"]
