"""Home-feed scraping with SDUI permalink capture."""

from __future__ import annotations

from typing import Any

import asyncio
import logging

import anyio
import anyio.lowlevel
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.feed_payload import (
    POST_SLUG_URL_RE,
    build_feed_references,
    is_feed_payload_response,
)
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from linkedin_mcp_server.scraping.text import (
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)


class FeedScraper:
    """Scrape the home feed and the post permalinks its SDUI payloads carry."""

    def __init__(
        self,
        session: ScrapingSession,
        navigator: PageNavigator,
        content: PageContentReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content

    @staticmethod
    async def _drain_listener_tasks(pending: list[asyncio.Task[None]]) -> None:
        """Bounded teardown for fire-and-forget response listener tasks.

        The feed scroll loop appends a read task per matching response;
        those tasks must finish (or be cancelled) before we leave the
        extractor or the event loop's "Task exception was never retrieved"
        warnings will surface unrelated errors. The caps below let a stuck
        ``resp.body()`` call burn at most three seconds of teardown budget.
        """
        if not pending:
            return
        try:
            await asyncio.wait(pending, timeout=2.0)
        finally:
            # Cancel on *every* exit of that wait, the caller's own cancellation
            # included. The response listener is unsubscribed before we get here,
            # so no one else will ever ask these reads to stop; returning through
            # the cancelled path without asking left a real ``resp.body()``
            # running with no cancellation requested at all.
            for task in pending:
                if not task.done():
                    task.cancel()
            # FastMCP wraps each tool call in ``anyio.fail_after``, whose scope
            # re-delivers its cancellation on every loop iteration until the task
            # leaves it. Unshielded, the wait below would be cancelled before the
            # reads it watches can act on the cancel above, which is the case the
            # budget exists for. The shield covers a bounded wait only, and the
            # outer cancellation resumes as soon as the scope closes.
            with anyio.CancelScope(shield=True):
                try:
                    await asyncio.wait(pending, timeout=1.0)
                finally:
                    # A shield only holds off AnyIO's own delivery, so a second
                    # plain ``Task.cancel()`` still cuts that wait short. Read
                    # and report the reads as they actually stand, or a failure
                    # that arrived before the cancel is left for the loop to
                    # report and a task still running is left unmentioned.
                    leftover = [task for task in pending if not task.done()]
                    for task in pending:
                        if task.done() and not task.cancelled():
                            # Consume the failure; unretrieved, it reaches the
                            # loop's handler long after the feed call returned.
                            task.exception()
                    if leftover:
                        logger.warning(
                            "SDUI feed listener tasks did not drain after cancel; leaking %d task(s)",
                            len(leftover),
                        )
        # A deadline that first comes due inside the shield has nowhere to land:
        # AnyIO skips a shielded scope while delivering, and the restart on the
        # way out runs in this very task, so it can only schedule delivery for the
        # next turn. get_feed's next step is report_progress, which never suspends
        # when the client sent no progress token, and the expired call would then
        # return a result. This unshielded checkpoint is that next turn. It is
        # outside the block above so that a cancellation already on its way keeps
        # propagating without waiting on anything.
        await anyio.lowlevel.checkpoint()

    async def extract_feed(
        self,
        num_posts: int = 10,
    ) -> ExtractedSection:
        """Scrape the LinkedIn home feed, scrolling until *num_posts* are loaded."""
        try:
            return await self._extract_feed_once(num_posts)
        except LinkedInScraperException:
            raise
        except Exception as e:
            logger.warning("Failed to extract feed: %s", e)
            return ExtractedSection(
                text="",
                references=[],
                error=build_issue_diagnostics(e, context="extract_feed"),
            )

    async def _extract_feed_once(
        self,
        num_posts: int,
    ) -> ExtractedSection:
        """Single attempt: navigate, scroll until post count, extract."""
        url = "https://www.linkedin.com/feed/"
        page = self._session.page

        # Post permalinks live in the SDUI pagination response (field:
        # "postSlugUrl"). The initial /feed/ HTML embeds the same data in
        # an RSC flight payload. Listen for both during the whole scroll
        # loop. ``seen_urls`` doubles as the locale-independent scroll
        # progress signal, replacing the previous "Feed post" innerText
        # marker that broke on non-English UIs.
        captured_urls: list[str] = []
        seen_urls: set[str] = set()
        pending_reads: list[asyncio.Task[None]] = []

        def _handle_response(resp: Any) -> None:
            if not is_feed_payload_response(resp.url):
                return

            async def _read() -> None:
                try:
                    body = await resp.body()
                except Exception:
                    return
                if not body:
                    return
                text = body.decode("utf-8", errors="replace")
                for match in POST_SLUG_URL_RE.finditer(text):
                    post_url = f"https://www.linkedin.com/posts/{match.group('slug')}"
                    if post_url not in seen_urls:
                        seen_urls.add(post_url)
                        captured_urls.append(post_url)

            pending_reads.append(asyncio.create_task(_read()))

        page.on("response", _handle_response)
        try:
            return await self._extract_feed_body(
                url, num_posts, captured_urls, pending_reads
            )
        finally:
            try:
                # The very object that was registered, never a fresh equivalent:
                # Playwright matches a listener by identity, so a re-created
                # closure removes nothing and leaves the read subscribed for the
                # rest of the page's life. The drain below runs either way,
                # because a removal that raised is exactly the case where the
                # reads still need stopping.
                page.remove_listener("response", _handle_response)
            except Exception:
                pass
            await self._drain_listener_tasks(pending_reads)

    async def _extract_feed_body(
        self,
        url: str,
        num_posts: int,
        captured_urls: list[str],
        pending_reads: list[asyncio.Task[None]],
    ) -> ExtractedSection:
        page = self._session.page
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()

        try:
            await page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)

        await self._session.dismiss_modal()

        try:
            await page.wait_for_function(
                """() => {
                    const main = document.querySelector('main');
                    if (!main) return false;
                    return main.innerText.length > 200;
                }""",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            logger.debug("Feed content did not appear on %s", url)

        # The feed has its own scroll container — window.scrollTo is a no-op.
        # mouse.wheel over the viewport center triggers the real scroll.
        _MAX_SCROLLS = 12
        _MAX_STALE = 3
        _BATCH_WAIT = 6.0
        _WHEEL_DELTA = 2000
        _IN_LOOP_DRAIN_TIMEOUT = 1.0
        stale_count = 0

        viewport = page.viewport_size or {"width": 1280, "height": 720}
        cx, cy = viewport["width"] // 2, viewport["height"] // 2
        await page.mouse.move(cx, cy)

        for i in range(_MAX_SCROLLS):
            count = len(captured_urls)
            logger.debug("Feed scroll %d: %d permalinks captured", i, count)
            if count >= num_posts:
                break

            await page.mouse.wheel(0, _WHEEL_DELTA)

            new_count = count
            for _ in range(int(_BATCH_WAIT)):
                await self._session.delay(1.0)
                # Drain in-flight response reads so captured_urls reflects
                # everything Playwright already delivered. Without this,
                # the count comparison races: the wheel fires a network
                # response, the listener creates a read task, and the loop
                # sleeps and re-checks before _read() finishes appending —
                # producing false-stale verdicts.
                if pending_reads:
                    done, _still = await asyncio.wait(
                        pending_reads, timeout=_IN_LOOP_DRAIN_TIMEOUT
                    )
                    if done:
                        # Surface unexpected exceptions. _read() catches
                        # expected playwright errors, but a parser bug
                        # would otherwise vanish into the loop. Log them
                        # rather than raising so a single bad response
                        # doesn't abort the whole scroll session.
                        for result in await asyncio.gather(
                            *done, return_exceptions=True
                        ):
                            if isinstance(result, BaseException):
                                logger.warning(
                                    "Unhandled error in feed _read task: %r",
                                    result,
                                )
                    pending_reads[:] = [t for t in pending_reads if not t.done()]
                new_count = len(captured_urls)
                if new_count > count:
                    break

            if new_count > count:
                stale_count = 0
            else:
                stale_count += 1
                logger.debug(
                    "Feed stale scroll %d/%d (still at %d permalinks)",
                    stale_count,
                    _MAX_STALE,
                    new_count,
                )
                if stale_count >= _MAX_STALE:
                    logger.debug("Feed stopped producing new posts")
                    break

        # Give any in-flight response reads a beat to finish recording URLs.
        await self._session.delay(0.2)

        raw_result = await self._content._extract_root_content(["main"])
        raw = raw_result["text"]

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Page %s returned only LinkedIn chrome (likely rate-limited)", url
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_feed_references(raw_result["references"], captured_urls),
        )
