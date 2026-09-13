"""Tests for the LinkedInExtractor scraping engine."""

from contextlib import ExitStack
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import asyncio
import logging

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import pytest

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.connection import (
    ActionSignals,
    detect_connection_state,
)
from linkedin_mcp_server.scraping import extractor as extractor_module
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.contracts import RATE_LIMITED_SECTION_TEXT
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    LinkedInExtractor,
    _MESSAGE_COMPOSER_OWNER_JS,
    _MESSAGE_CONFIRMATION_DISPOSE_JS,
    _MESSAGE_CONFIRMATION_PREPARE_JS,
    _MESSAGE_CONFIRMATION_READY_JS,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from scraping.support.navigation import navigate


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestExtractSearchPage:
    async def test_extract_search_page_raises_auth_error_for_login_barrier(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("Run with --login"),
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_the_search_redesign_redirect_is_not_a_replacement(self, mock_page):
        """LinkedIn's 302 to `/jobs/search-results` must not end the page.

        The route asked for is compared against the one the page ended on,
        and a mismatch is fatal on purpose: an account picker served in place
        of a search moves the route exactly this way. The redesign redirect
        moves it too, so a migrated account raised here, before any of the
        id extraction downstream could run, and the search returned nothing
        while reporting that it had navigated away.

        Driven through `_extract_search_page_once` rather than around it. A
        test that mocks the extraction layer places the landing address after
        this comparison has already happened and passes whatever it does.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=python"

        async def redirect_to_the_redesign(url, *args, **kwargs):
            navigate(
                mock_page,
                "https://www.linkedin.com/jobs/search-results/?keywords=python",
            )

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=redirect_to_the_redesign,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
            )

        assert result.text == "Sample page text"
        assert result.error is None

    async def test_a_route_change_off_the_search_still_ends_the_page(self, mock_page):
        """The loosening is between the two search routes and nowhere else.

        `/feed/` is deliberately not an auth route. A checkpoint would be
        rejected by the detector before the helper was tested, so a helper
        accepting every same-host path could pass that fixture unchanged.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=python"

        async def redirect_to_the_feed(url, *args, **kwargs):
            navigate(mock_page, "https://www.linkedin.com/feed/")

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=redirect_to_the_feed,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(RuntimeError, match="Page navigated to .*/feed/"),
        ):
            await extractor._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
            )

    async def test_the_redesign_redirect_keeps_the_full_auth_check(self, mock_page):
        """An account picker can be served at an otherwise allowed path.

        Route equivalence cannot classify the document, so the full detector
        must run before the helper suppresses the route-mismatch error.
        """
        requested = "https://www.linkedin.com/jobs/search/?keywords=python"
        mock_page.url = requested

        async def redirect_to_the_redesign(url, *args, **kwargs):
            navigate(
                mock_page,
                "https://www.linkedin.com/jobs/search-results/?keywords=python",
            )

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=redirect_to_the_redesign,
            ),
            patch.object(
                PageNavigator,
                "_raise_if_auth_barrier",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("Run with --login"),
            ) as check_auth,
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page_once(
                requested,
                section_name="search_results",
            )

        check_auth.assert_awaited_once_with(requested)

    async def test_a_checkpoint_while_scrolling_raises_an_auth_error(self, mock_page):
        """A checkpoint reached mid-scroll must not come back as job results.

        The scroll suppresses every error its evaluate raises, and a
        navigation destroying the execution context is one of them. The
        extraction that follows then reads the replacement document and hands
        its text back under `search_results` with no `section_errors` beside
        it, which no client can tell from a search that found those words.

        A diagnostic is not enough either. An expired session reaches this
        branch as often as a layout change does, and only the auth error
        starts the recovery the tool has: returning a section error leaves
        the dead browser registered and offers no re-login, so the next call
        walks into the same barrier.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Let's do a quick security check\nStart puzzle",
                "references": [],
            }
        )

        async def navigate_away(page, **kwargs):
            navigate(page, "https://www.linkedin.com/checkpoint/challenge/")
            # The real helper reports that its evaluate raised, which a
            # navigation destroying the execution context always makes it do.
            return True

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=navigate_away,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_plain_redirect_while_scrolling_stays_a_diagnostic(self, mock_page):
        """Only an auth barrier escalates; anything else is still diagnosed.

        The same branch catches a layout change and a link followed by
        accident, neither of which a re-login would repair. Raising the auth
        error for those would send the user through an interactive sign-in to
        fix a page that was never locked.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Some other page", "references": []}
        )

        async def navigate_away(page, **kwargs):
            navigate(page, "https://www.linkedin.com/feed/")
            return True

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=navigate_away,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert result.text == ""
        assert result.error is not None
        assert "Some other page" not in str(result.error)

    async def test_a_reload_onto_an_account_picker_is_an_auth_error(self, mock_page):
        """A reload keeps the address, so the route sees nothing to compare.

        LinkedIn can serve the account picker at the search URL itself. The
        route matches at both ends, and the replacement renders after it
        commits: an account picker was measured 200ms behind its own
        navigation, so a page judged on arrival is judged empty and the
        picker's text comes back under `search_results`. The barrier is read
        once the replacement document is ready, and the double answers the
        way that page does.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"

        async def reload_in_place(page, **kwargs):
            navigate(mock_page)
            return True

        async def barrier(page):
            if not mock_page.wait_for_load_state.await_count:
                return None
            return "auth barrier text: welcome back + sign in"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=reload_in_place,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_picker_without_main_is_an_auth_error(self, mock_page):
        """No `<main>` skips the scroll, and skipping it skipped the check.

        An account picker served at the search address has no `<main>`, so the
        scroll never runs and `moved` stays false, and the route matches at
        both ends because nothing navigated. Both signals the check waited for
        are absent on exactly the page it exists to catch, and the picker's
        text came back under `search_results`.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no main")
        )

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ) as scroll,
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="auth barrier text: welcome back + join now",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )
        scroll.assert_not_called()

    async def test_a_page_without_main_is_still_extracted(self, mock_page):
        """The check runs on every `<main>`-less page; only a barrier stops one.

        A search that has run out of results renders no `<main>` either, and
        that page is the ordinary end of pagination rather than a failure.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no main")
        )

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )
        assert result.error is None
        # The body fallback is what carries that page, so an empty section
        # here would discard the very text this branch exists to keep: the
        # no-results notice, or whatever diagnostic LinkedIn rendered instead.
        assert result.text == "Sample page text"

    async def test_a_reload_after_a_clean_scroll_is_still_a_reload(self, mock_page):
        """The scroll can finish and the document be replaced anyway.

        Nothing else notices: the scroll never raised, so it reports no
        movement, and a reload moves no route, so the comparison at both ends
        matches. The listener has already fired by then, and reading it costs
        a healthy page nothing.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"

        async def scroll_then_reload(page, **kwargs):
            navigate(mock_page)
            return False

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=scroll_then_reload,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="account picker: #rememberme-div",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_search_page_naming_its_own_job_is_not_navigating(self, mock_page):
        """The event fires on every healthy search page, and means nothing.

        LinkedIn appends `currentJobId` through `pushState`, which raises
        `framenavigated` on the main frame exactly as a reload does. Acting on
        it charges the ordinary page a quiet window, a document wait and the
        body read behind the barrier check, on all of the up to ten pages a
        search walks.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"

        async def scroll_then_name_a_job(page, **kwargs):
            navigate(
                mock_page,
                "https://www.linkedin.com/jobs/search/?keywords=test&currentJobId=1",
                same_document=True,
            )
            return False

        barrier = AsyncMock(return_value=None)
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=scroll_then_name_a_job,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                barrier,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert result.text
        assert mock_page.wait_for_load_state.await_count == 0
        assert barrier.await_count == 0

    async def test_the_scroll_gets_the_deadline_and_reports_what_it_spent(
        self, mock_page
    ):
        """Two links the budget rests on, and the budget test supplies both.

        Replacing `_extract_search_page` is what lets that test drive ten
        pages, and it means the deadline it observes and the seconds it
        charges are its own. A search that stopped handing the deadline down,
        or stopped charging what the scroll spent, leaves every page a fresh
        cap and the whole call running past its timeout with that test green.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        seen: list[float | None] = []

        async def scroll(page, **kwargs):
            seen.append(kwargs.get("deadline"))
            clock.now += 3.0
            return False

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor_module, "time", clock),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=scroll,
            ),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
                scroll_deadline=7.0,
            )

        assert seen == [7.0]
        assert extractor._scroll_seconds == 3.0

    async def test_a_reload_after_the_scroll_is_caught_by_the_read(self, mock_page):
        """The watcher comes off before the page is read.

        A reload committing in that gap, or during the extraction itself,
        moves no route and raises nothing: the scroll already returned, the
        listener is already gone, and the address is what it always was. The
        search then returns whatever the replacement holds.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        replaced = mock_page.time_origin

        async def reload_at_read(*args, **kwargs):
            navigate(mock_page)
            return {"source": "root", "text": "Welcome back", "references": []}

        async def barrier(page):
            if mock_page.time_origin == replaced:
                return None
            return "account picker: #rememberme-div"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                side_effect=reload_at_read,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_redirect_chain_is_judged_on_where_it_stops(self, mock_page):
        """The last hop decides, not the first one to appear.

        A chain passes through documents of its own. Judging the one that
        happens to be current calls a checkpoint healthy when it arrives a
        moment later, and the search returns a section diagnostic while the
        browser sits on a checkpoint with no relogin offered.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"

        async def hop_twice(page, **kwargs):
            async def hops() -> None:
                await asyncio.sleep(0.02)
                navigate(page, "https://www.linkedin.com/feed/")
                await asyncio.sleep(0.1)
                navigate(page, "https://www.linkedin.com/checkpoint/challenge/")

            asyncio.get_running_loop().create_task(hops())
            return True

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=hop_twice,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_chain_that_pauses_is_still_followed(self, mock_page):
        """A hop that takes its time is not the end of the chain.

        The quiet window decides when a route counts as settled, so a chain
        that stalls longer than the window is judged on the hop it stalled on.
        A checkpoint reached after a pause reads as a healthy feed page.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"

        async def hop_slowly(page, **kwargs):
            async def hops() -> None:
                await asyncio.sleep(0.02)
                navigate(page, "https://www.linkedin.com/feed/")
                await asyncio.sleep(0.3)
                navigate(page, "https://www.linkedin.com/checkpoint/challenge/")

            asyncio.get_running_loop().create_task(hops())
            return True

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=hop_slowly,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_chain_the_scroll_survived_is_still_followed(self, mock_page):
        """A redirect can move the route without the scroll ever raising.

        The scroll returning cleanly says its own context survived, and says
        nothing about a navigation that started before it or lands after it.
        Sampling the route once at that point stops the chain on its first hop.
        """
        mock_page.url = "https://www.linkedin.com/feed/"

        async def hop_late(page, **kwargs):
            async def hops() -> None:
                # Inside `_URL_SETTLE_LAG`, and not on it. Scheduled at the
                # boundary itself the test measures the scheduler: a hop due
                # at exactly 0.3s landed after the deadline in one local run
                # in ten. What the window covers is the question; where its
                # edge falls under load is not.
                await asyncio.sleep(0.05)
                navigate(page, "https://www.linkedin.com/checkpoint/challenge/")

            asyncio.get_running_loop().create_task(hops())
            return False

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=hop_late,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

    async def test_a_blank_foreign_page_is_diagnosed_not_reported_empty(
        self, mock_page
    ):
        """No ``<main>`` used to skip the route check with it.

        A landing page without one extracts to nothing, and an empty section
        with no error is what a search that found nothing looks like. The
        check now runs whether or not the page had a `<main>` to scroll.
        """
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        mock_page.url = "https://interstitial.example/blank"
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no main")
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert result.text == ""
        assert result.error is not None

    async def test_a_foreign_host_with_the_same_path_is_a_redirect(self, mock_page):
        """The path alone cannot tell a search page from an interstitial.

        A proxy or a captive portal serving its own `/jobs/search` keeps the
        path across the navigation, so comparing paths alone reads it as the
        page never having moved. Its text would then come back under
        `search_results` with no `section_errors`, which is the failure this
        whole check exists to prevent, arriving through the front door.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Proxy interstitial",
                "references": [],
            }
        )

        async def navigate_away(page, **kwargs):
            page.url = "https://interstitial.example/jobs/search?keywords=test"
            return True

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=navigate_away,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert result.text == ""
        assert result.error is not None
        assert "Proxy interstitial" not in str(result.error)

    async def test_currentjobid_alone_does_not_count_as_a_redirect(self, mock_page):
        """LinkedIn moves the query of a search page by itself, mid-scroll.

        The guard above compares paths for this reason. Comparing whole URLs
        would refuse every second search page and diagnose a healthy one.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Python Developer\nAcme\nBerlin",
                "references": [],
            }
        )

        async def add_current_job(page, **kwargs):
            page.url = (
                "https://www.linkedin.com/jobs/search?keywords=test&currentJobId=1"
            )

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=add_current_job,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert "Python Developer" in result.text
        assert result.error is None


class TestDetectConnectionState:
    """Tests for locale-independent connection-state detection.

    Every state is decided purely from the structural ActionSignals; no
    profile text is read for any state, including incoming_request (whose
    Accept/Ignore action row is fingerprinted by ``has_incoming_action_row``).
    """

    @staticmethod
    def _signals(
        invite: bool = False,
        compose_in_root: bool = False,
        edit: bool = False,
        labeled_action: bool = False,
        labeled_anchor: bool = False,
        incoming_row: bool = False,
    ) -> ActionSignals:
        return ActionSignals(
            has_invite_anchor=invite,
            has_compose_anchor_in_action_root=compose_in_root,
            has_edit_intro_anchor=edit,
            has_labeled_action_button=labeled_action,
            has_labeled_action_anchor=labeled_anchor,
            has_incoming_action_row=incoming_row,
        )

    def test_self_profile(self):
        assert detect_connection_state(self._signals(edit=True)) == "self_profile"

    def test_connectable(self):
        assert detect_connection_state(self._signals(invite=True)) == "connectable"

    def test_already_connected(self):
        # 1st-degree: Message anchor in action root, but no Follow/Connect/Pending
        # button (no aria-label on any action-root button).
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_action=False)
            )
            == "already_connected"
        )

    def test_follow_only(self):
        # No invite anchor anywhere, but a primary action <button> (Follow
        # / Save in Sales Navigator) is present alongside the Message
        # anchor.
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_action=True)
            )
            == "follow_only"
        )

    def test_pending_via_labeled_anchor(self):
        # Pending is rendered as <a aria-label="Pending, click to ..."> in
        # the action root — distinct from Follow's <button aria-label=...>.
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_anchor=True)
            )
            == "pending"
        )

    def test_pending_takes_priority_over_already_connected(self):
        # If the labeled anchor is present alongside compose-in-root with
        # no labeled button, pending wins over the already_connected
        # fallthrough that would otherwise apply.
        assert (
            detect_connection_state(
                self._signals(compose_in_root=True, labeled_anchor=True)
            )
            == "pending"
        )

    def test_incoming_request_via_structural_row(self):
        assert (
            detect_connection_state(self._signals(incoming_row=True))
            == "incoming_request"
        )

    def test_incoming_structural_beats_pending_misclassification(self):
        # Regression for the sidebar mis-anchor: on incoming profiles the
        # compose-anchor action-root walk lands on sidebar cards and
        # produces garbage signals (compose, labeled button, labeled
        # anchor all True). The structural incoming signal must win over
        # the pending check those garbage signals would trigger.
        assert (
            detect_connection_state(
                self._signals(
                    incoming_row=True,
                    compose_in_root=True,
                    labeled_action=True,
                    labeled_anchor=True,
                )
            )
            == "incoming_request"
        )

    def test_connectable_takes_priority_over_incoming_row(self):
        assert (
            detect_connection_state(self._signals(invite=True, incoming_row=True))
            == "connectable"
        )

    def test_self_profile_takes_priority_over_incoming_row(self):
        assert (
            detect_connection_state(self._signals(edit=True, incoming_row=True))
            == "self_profile"
        )

    def test_unavailable_when_no_signals(self):
        assert detect_connection_state(self._signals()) == "unavailable"

    def test_unavailable_when_compose_missing(self):
        # Restricted profile: no compose anchor, no labels, no invite.
        assert (
            detect_connection_state(self._signals(labeled_action=True)) == "unavailable"
        )


class TestScrapeJob:
    async def test_scrape_job(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Job: Software Engineer"),
        ):
            result = await extractor.scrape_job("12345")

        assert result["url"] == "https://www.linkedin.com/jobs/view/12345/"
        assert "job_posting" in result["sections"]
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_scrape_job_omits_rate_limited_sentinel(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await extractor.scrape_job("12345")

        assert result["sections"] == {}
        assert result["section_errors"]["job_posting"]["error_type"] == "rate_limit"

    async def test_scrape_job_omits_orphaned_references_when_text_empty(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(
                "",
                [{"kind": "job", "url": "/jobs/view/12345/", "text": "Engineer"}],
            ),
        ):
            result = await extractor.scrape_job("12345")

        assert result["sections"] == {}
        assert "references" not in result


class TestSearchJobs:
    """Tests for search_jobs with job ID extraction and pagination."""

    @pytest.fixture(autouse=True)
    def _set_search_url(self, mock_page):
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=python"

    @staticmethod
    def _navigating(mock_page, texts, *, lands_on=None, clock=None, cost=0.0):
        """A page double that moves `page.url` the way a navigation does.

        Left fixed, `page.url` keeps the offset of whichever page the test set
        up last, so the loop reads its own `start` back unchanged and every
        multi-page assertion holds for a reason the browser does not supply.
        `lands_on` is the address LinkedIn answers with, for a navigation that
        does not keep the offset.
        """
        supply = iter(texts) if not callable(texts) else None

        async def navigate_page(url, *args, **kwargs):
            navigate(mock_page, lands_on or url)
            if clock is not None:
                clock.now += cost
            return texts(url) if supply is None else next(supply)

        return navigate_page

    async def test_returns_job_ids(self, mock_page):
        """search_jobs should return a job_ids list extracted from hrefs."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("Job 1\nJob 2\nJob 3"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222", "333"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111", "222", "333"]
        assert "search_results" in result["sections"]

    async def test_returns_references(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "Job 1",
                    [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}],
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["references"] == {
            "search_results": [
                {"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}
            ]
        }

    async def test_componentkey_jobs_without_anchors_get_fallback_references(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        page = extracted(
            "Redesigned job cards",
            [
                {
                    "kind": "company",
                    "url": "/company/acme/",
                    "text": "Acme",
                    "context": "search result",
                }
            ],
        )

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [page]),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["222", "111"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["222", "111"]
        assert result["references"]["search_results"] == [
            {
                "kind": "company",
                "url": "/company/acme/",
                "text": "Acme",
                "context": "search result",
            },
            {"kind": "job", "url": "/jobs/view/222/"},
            {"kind": "job", "url": "/jobs/view/111/"},
        ]

    async def test_reconciles_uncapped_raw_references_in_dom_order(self, mock_page):
        """Rail jobs survive the page cap without losing DOM interleaving."""
        extractor = LinkedInExtractor(mock_page)
        ancillary = [
            {
                "href": f"https://www.linkedin.com/company/company-{index}/",
                "text": f"Company {index}",
            }
            for index in range(13)
        ]
        raw_references = [
            {
                "href": "https://www.linkedin.com/jobs/view/999/",
                "text": "Detail pane job",
            },
            ancillary[0],
            {
                "href": "https://www.linkedin.com/jobs/view/111/",
                "text": "Rail job 111",
            },
            ancillary[1],
            ancillary[2],
            {
                "href": "https://www.linkedin.com/jobs/view/222/",
                "text": "Rail job 222",
            },
            *ancillary[3:12],
            {
                "href": "https://www.linkedin.com/jobs/view/333/",
                "text": "Rail job 333 after the old cap",
            },
            ancillary[12],
        ]
        raw_page = {
            "source": "root",
            "text": "Job results",
            "references": raw_references,
        }

        async def navigate_page(url, *args, **kwargs):
            navigate(mock_page, url)

        with (
            patch.object(PageNavigator, "_navigate_to_page", side_effect=navigate_page),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=raw_page,
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222", "111", "333"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111", "222", "333"]
        assert result["references"]["search_results"] == [
            {
                "kind": "company",
                "url": "/company/company-0/",
                "text": "Company 0",
                "context": "search result",
            },
            {
                "kind": "job",
                "url": "/jobs/view/111/",
                "text": "Rail job 111",
                "context": "job result",
            },
            {
                "kind": "company",
                "url": "/company/company-1/",
                "text": "Company 1",
                "context": "search result",
            },
            {
                "kind": "company",
                "url": "/company/company-2/",
                "text": "Company 2",
                "context": "search result",
            },
            {
                "kind": "job",
                "url": "/jobs/view/222/",
                "text": "Rail job 222",
                "context": "job result",
            },
            *[
                {
                    "kind": "company",
                    "url": f"/company/company-{index}/",
                    "text": f"Company {index}",
                    "context": "search result",
                }
                for index in range(3, 12)
            ],
            {
                "kind": "job",
                "url": "/jobs/view/333/",
                "text": "Rail job 333 after the old cap",
                "context": "job result",
            },
        ]

    async def test_a_slashless_search_url_still_yields_job_ids(self, mock_page):
        """`/jobs/search?keywords=x` is the same route as `/jobs/search/`.

        The `?` sits where a prefix test wants the slash, so the guard read a
        healthy page as a redirect: it kept the page text, skipped extraction
        and ended pagination, and the search came back with `job_ids: []` and
        no `section_errors` to say why. The redirect check a few lines above
        already compares parsed paths and calls the same URL healthy, so the
        two disagreed about exactly one address.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search?keywords=python"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("Job 1"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111"]

    async def test_a_foreign_host_still_skips_job_ids(self, mock_page):
        """Only the path is normalized; the host still has to be LinkedIn.

        Comparing paths alone would accept any origin serving a
        `/jobs/search` path, which is what an interstitial or a proxied error
        page can look like.
        """
        mock_page.url = "https://example.com/jobs/search?keywords=python"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("Job 1"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ) as ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        ids.assert_not_called()

    async def test_pagination_follows_what_the_page_rendered(self, mock_page):
        """&start= advances by the cards found, not by LinkedIn's stride.

        A live search rendered 11 cards per navigation while advertising 25
        per page, so a fixed stride skipped 13 of every 24 jobs.
        """
        extractor = LinkedInExtractor(mock_page)
        page1_ids = ["100", "200", "300"]
        page2_ids = ["400", "500"]
        id_pages = iter([page1_ids, page2_ids])
        text_pages = iter(["Page 1 text", "Page 2 text"])
        urls_visited: list[str] = []

        navigate_page = self._navigating(
            mock_page, lambda _url: extracted(next(text_pages))
        )

        async def mock_extract(url, *args, **kwargs):
            urls_visited.append(url)
            return await navigate_page(url)

        with (
            patch.object(extractor, "_extract_search_page", side_effect=mock_extract),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["100", "200", "300", "400", "500"]
        assert len(urls_visited) == 2
        # The offset advances by what this returns, so an unscoped read counts
        # the detail pane's own permalink and whatever it has loaded as
        # rendered results and skips jobs the rail never showed. The double
        # answers every call the same, so only the argument says which one
        # the search asked for.
        assert all(c.kwargs.get("scoped") is True for c in mock_ids.await_args_list)
        # Parsed, not matched as a substring: "&start=3" also passes for
        # start=30, which is exactly what a stride regression would produce.
        page2 = parse_qs(urlparse(urls_visited[1]).query)
        assert page2["start"] == ["3"]  # page 1 rendered three cards

    async def test_references_keep_all_jobs_beyond_the_per_section_cap(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        id_pages = [
            [str(1000 + index) for index in range(11)],
            [str(2000 + index) for index in range(11)],
        ]
        raw_pages = [
            {
                "source": "root",
                "text": f"Page {page_number}",
                "references": [
                    {
                        "href": f"https://www.linkedin.com/jobs/view/{job_id}/",
                        "text": f"Job {job_id}",
                    }
                    for job_id in page_ids
                ]
                + [
                    {
                        "href": (
                            "https://www.linkedin.com/company/"
                            f"page-{page_number}-{index}/"
                        ),
                        "text": f"Company {page_number}-{index}",
                    }
                    for index in range(6)
                ],
            }
            for page_number, page_ids in enumerate(id_pages, start=1)
        ]

        async def navigate_page(url, *args, **kwargs):
            navigate(mock_page, url)

        with (
            patch.object(PageNavigator, "_navigate_to_page", side_effect=navigate_page),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                side_effect=raw_pages,
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=id_pages,
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        expected_ids = [job_id for page_ids in id_pages for job_id in page_ids]
        references = result["references"]["search_results"]
        job_references = [ref for ref in references if ref["kind"] == "job"]
        ancillary = [ref for ref in references if ref["kind"] != "job"]

        assert result["job_ids"] == expected_ids
        assert [ref["url"] for ref in job_references] == [
            f"/jobs/view/{job_id}/" for job_id in expected_ids
        ]
        assert len(job_references) == 22
        assert len(ancillary) == 8
        assert len(references) == 30

    async def test_deduplication_across_pages(self, mock_page):
        """Duplicate job IDs across pages should be deduplicated."""
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100", "200"], ["200", "300"]])
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [extracted("text")] * 2),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["100", "200", "300"]
        assert mock_extract.await_count == 2

    async def test_a_missing_rail_is_reported_not_silent(self, mock_page, caplog):
        """Reading the document is the fallback, and it has to be audible.

        With no rail there is nothing to separate results from the detail
        pane, so this is the one path where the offset can count something
        the search never rendered. Live a search page has two scrollable
        candidates, so it has not been observed.
        """
        mock_page.evaluate = AsyncMock(
            return_value={"ids": ["101", "999"], "scoped": False}
        )
        extractor = LinkedInExtractor(mock_page)

        with caplog.at_level("WARNING"):
            assert await extractor._extract_job_ids(scoped=True) == ["101", "999"]

        assert "No results rail" in caplog.text

    async def test_a_dropped_location_is_reported_and_the_results_kept(self, mock_page):
        """A filter LinkedIn drops costs relevance, not correctness.

        The results are still about the keywords that were asked for, only
        broader, so stopping would return nothing where something useful is
        in hand. Saying nothing is the part that cannot be defended: a search
        for Python in Berlin comes back as Python anywhere and reads as
        though Berlin had none.
        """
        extractor = LinkedInExtractor(mock_page)

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("python jobs")],
                    lands_on=("https://www.linkedin.com/jobs/search/?keywords=python"),
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["901"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs(
                "python", location="Berlin", max_pages=1
            )

        assert result["job_ids"] == ["901"]
        error = result["section_errors"]["search_results"]
        assert error["error_type"] == "filters_dropped"
        assert "location" in error["error_message"]

    async def test_a_dropped_filter_survives_whatever_stops_the_loop(self, mock_page):
        """The warning describes the results, and the results are returned.

        One slot holds both, so a rate limit on page two used to replace the
        note saying page one had come back unfiltered. Those results are
        still in the response, and a caller reading only the stop reason acts
        on Berlin jobs that are not from Berlin.
        """
        extractor = LinkedInExtractor(mock_page)
        pages = iter(
            [
                extracted("python jobs"),
                extracted(RATE_LIMITED_SECTION_TEXT),
            ]
        )
        urls = iter(
            [
                "https://www.linkedin.com/jobs/search/?keywords=python",
                "https://www.linkedin.com/jobs/search/?keywords=python&start=1",
            ]
        )

        async def land(url, *args, **kwargs):
            mock_page.url = next(urls)
            return next(pages)

        with (
            patch.object(extractor, "_extract_search_page", side_effect=land),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["901"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs(
                "python", location="Berlin", max_pages=2
            )

        assert result["job_ids"] == ["901"]
        message = result["section_errors"]["search_results"]["error_message"]
        assert "location" in message
        assert RATE_LIMITED_SECTION_TEXT in message

    async def test_a_search_answered_for_something_else_stops_the_loop(self, mock_page):
        """The route can be right and the offset right while the query is gone.

        A redirect to the bare search page keeps host, path and `start=0`, so
        the first navigation passes every check and generic recommendations
        come back as a search for Python in Berlin. The keywords are compared
        by value and not by presence, because the same shape covers LinkedIn
        answering a different question rather than none.
        """
        extractor = LinkedInExtractor(mock_page)

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("recommended for you")],
                    lands_on="https://www.linkedin.com/jobs/search/",
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["901"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", location="Berlin")

        assert result["job_ids"] == []
        assert mock_ids.await_count == 0
        error = result["section_errors"]["search_results"]
        assert error["error_type"] == "search_replaced"
        # Both sides named, so a LinkedIn re-encoding rather than a different
        # search is diagnosable from the response itself.
        assert "python" in error["error_message"]

    async def test_the_redesigned_search_route_still_yields_ids(self, mock_page):
        """LinkedIn 302s `/jobs/search/` to its redesigned results route.

        The guard accepted only the route the URL builder produces, so every
        account already moved over ended the search on the first page with
        `job_ids: []` while `search_results` listed real jobs. The redirect
        keeps the query and honours `start`, so the destination is the search
        and not a replacement of it.
        """
        extractor = LinkedInExtractor(mock_page)

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("Job 1\nJob 2")],
                    lands_on=(
                        "https://www.linkedin.com/jobs/search-results/?keywords=python"
                    ),
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111", "222"]
        assert mock_ids.await_count == 1
        assert "search_results" not in result.get("section_errors", {})

    async def test_the_redesigned_route_still_reports_a_dropped_filter(self, mock_page):
        """Reaching the guard is what lets the filter check run at all.

        The redesigned route drops `location`, and the results then come back
        for whatever place the account defaults to. That is reported rather
        than retried, the way every other dropped filter is; before the guard
        accepted this route the search raised first and said nothing about
        the location.
        """
        extractor = LinkedInExtractor(mock_page)

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("Job 1")],
                    lands_on=(
                        "https://www.linkedin.com/jobs/search-results/?keywords=python"
                    ),
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs(
                "python", location="Berlin", max_pages=1
            )

        assert result["job_ids"] == ["111"]
        error = result["section_errors"]["search_results"]
        assert error["error_type"] == "filters_dropped"
        assert "location" in error["error_message"]

    async def test_a_pane_job_is_not_a_search_result(self, mock_page):
        """The ids come from the rail and the references from the whole page.

        A job the detail pane had loaded was emitted as a search result while
        `job_ids` correctly left it out, so a caller following the references
        acts on a job this search never returned.
        """
        extractor = LinkedInExtractor(mock_page)
        page = extracted(
            "Job results",
            [
                {"kind": "job", "url": "/jobs/view/111/", "text": "In the rail"},
                {"kind": "job", "url": "/jobs/view/999/", "text": "In the pane"},
                {"kind": "company", "url": "/company/acme/", "text": "Acme"},
            ],
        )

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [page]),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        urls = [r["url"] for r in result["references"]["search_results"]]
        assert "/jobs/view/111/" in urls
        assert "/jobs/view/999/" not in urls
        # Everything that is not a job is untouched by which rail was picked.
        assert "/company/acme/" in urls

    async def test_a_dropped_search_offset_stops_the_loop(self, mock_page):
        """The route can be right while the offset is gone.

        A navigation canonicalised back to the bare search URL serves the
        first page again. Host and path both pass, so the loop reads it a
        second time, appends its text to itself under `search_results`, and
        stops on the repeated ids with nothing to say why. The saved list
        does exactly this since LinkedIn moved it.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("the first page")] * 3,
                    lands_on="https://www.linkedin.com/jobs/search/?keywords=python",
                ),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["101", "102"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=3)

        assert result["job_ids"] == ["101", "102"]
        assert result["sections"]["search_results"] == "the first page"
        assert mock_extract.await_count == 2
        # Stopping quietly is what an exhausted search does too, so a caller
        # reading a short list has no way to tell the two apart.
        assert (
            result["section_errors"]["search_results"]["error_type"]
            == "pagination_stopped"
        )

    async def test_no_new_id_page_can_upgrade_duplicate_metadata(self, mock_page):
        """The stopping page still contributes richer duplicate metadata."""
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100", "200"], ["100", "200"]])
        extract_call_count = 0

        navigate_page = self._navigating(mock_page, lambda _url: None)

        async def mock_extract(url, *args, **kwargs):
            nonlocal extract_call_count
            await navigate_page(url)
            extract_call_count += 1
            if extract_call_count == 1:
                return extracted(
                    "text",
                    [
                        {
                            "kind": "job",
                            "url": "/jobs/view/100/",
                            "text": "Job 100",
                        },
                        {
                            "kind": "job",
                            "url": "/jobs/view/200/",
                            "text": "Job",
                        },
                    ],
                )
            return extracted(
                "text",
                [
                    {
                        "kind": "job",
                        "url": "/jobs/view/200/",
                        "text": "Senior Software Engineer",
                        "context": "job result",
                    }
                ],
            )

        with (
            patch.object(extractor, "_extract_search_page", side_effect=mock_extract),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=5)

        assert result["job_ids"] == ["100", "200"]
        assert extract_call_count == 2
        assert result["references"] == {
            "search_results": [
                {"kind": "job", "url": "/jobs/view/100/", "text": "Job 100"},
                {
                    "kind": "job",
                    "url": "/jobs/view/200/",
                    "text": "Senior Software Engineer",
                    "context": "job result",
                },
            ]
        }

    async def test_stops_once_past_the_advertised_results(self, mock_page):
        """Stop when the offset passes the last result LinkedIn advertises.

        The bound is a result count, not a page count: the offset advances by
        rendered cards, so comparing it to a page index would never trigger.
        """
        extractor = LinkedInExtractor(mock_page)
        # One advertised page is 25 results and the first navigation renders
        # exactly 25, which is the boundary: the offset reaches the end
        # without passing it. Rendering more would clear `>=` and `>` alike
        # and leave the comparison untested.
        id_pages = iter([[str(i) for i in range(25)], ["900"]])
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_total_pages,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=10)

        # One navigation despite max_pages=10
        assert mock_extract.await_count == 1
        assert mock_total_pages.await_count == 1
        assert result["job_ids"] == [str(i) for i in range(25)]

    async def test_the_scroll_budget_is_spent_and_not_divided(self, mock_page):
        """Asking for more pages must not shorten the first one.

        Divided up front, ten navigations got 6s each and a page whose first
        card takes 4.5s had nothing left for the batch behind it, so the
        larger request came back with fewer jobs than the smaller one. Each
        page now takes the per-page cap or the remainder, whichever is
        smaller, and the total is unchanged.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        extractor = LinkedInExtractor(mock_page)
        seen: list[float | None] = []

        async def capture(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            # A real page reports what its scroll spent, and only that. Twelve
            # seconds of navigation with no scrolling would leave the budget
            # untouched, which is the case this replaced.
            clock.now += 12.0
            extractor._scroll_seconds += 12.0
            return extracted("Job results")

        async def sleep(seconds: float) -> None:
            clock.now += seconds

        # Fresh ids every call, or the search stops after two navigations and
        # the budget is never spent over the ten this is named for.
        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(extractor_module, "time", clock),
            patch.object(extractor, "_extract_search_page", side_effect=capture),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            await extractor.search_jobs("python", max_pages=10, tool_timeout=100000)

        assert len(seen) == 10
        assert seen[0] == 12.0  # the per-page cap, whatever max_pages says
        assert seen == [12.0] * 5 + [0.0] * 5  # 60s, spent five pages in
        assert sum(seen) <= 60.0

    async def test_a_slow_navigation_does_not_spend_the_scroll_budget(self, mock_page):
        """The budget bounds scrolling, so only scrolling may spend it.

        Charging the page charged navigation and waiting for `<main>` too, so
        five slow navigations whose rails scrolled instantly still left every
        page behind them with nothing.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        extractor = LinkedInExtractor(mock_page)
        seen: list[float | None] = []

        async def capture(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            # All navigation, no scrolling.
            clock.now += 12.0
            return extracted("Job results")

        async def sleep(seconds: float) -> None:
            clock.now += seconds

        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(extractor_module, "time", clock),
            patch.object(extractor, "_extract_search_page", side_effect=capture),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            await extractor.search_jobs("python", max_pages=10, tool_timeout=100000)

        assert seen == [12.0] * 10

    async def test_a_slow_search_stops_before_the_tool_timeout(self, mock_page):
        """A cancelled tool returns nothing, so the loop has to stop itself.

        Measured live, ten navigations of a Paris developer search take 83s
        against a 180s default, so the guard never fires on a healthy run and
        this drives it with navigations slow enough to reach the budget.

        The page cost is chosen to land between the two arithmetics. Against a
        144s budget, six pages of 18.7s plus five delays of 2s reach 122.2s, and
        a seventh costs 20.7s and finishes at 142.9s. Charging the delay once
        admits it; charging it twice predicts 144.9s and drops a page the run
        had time for. The fake sleep therefore has to move the clock, or the
        delay never enters the sum at all and neither does the defect.
        """

        class Clock:
            """A monotonic clock the navigations move, so the guard is testable."""

            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        extractor = LinkedInExtractor(mock_page)
        seen: list[float | None] = []

        async def capture(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            clock.now += 18.7
            return extracted("Job results")

        async def sleep(seconds: float) -> None:
            """The inter-page delay costs wall clock, the same as a navigation."""
            clock.now += seconds

        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(extractor_module, "time", clock),
            patch.object(extractor, "_extract_search_page", side_effect=capture),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=10)

        # Seven pages end at 142.9s; an eighth would need 163.6s.
        assert len(seen) == 7
        assert result["job_ids"] == [jid for page in pages[:7] for jid in page]

    async def test_the_next_navigation_delay_is_part_of_the_prediction(self, mock_page):
        """The guard budgets the delay before a page, not just the page.

        The test above cannot see this: at a 144s budget the run stops after
        seven pages whether or not the prediction counts ``_NAV_DELAY``, so
        dropping it from the sum stays green. This budget is chosen to sit
        between the two arithmetics instead. Six pages reach 122.2s; a seventh
        costs 2s of delay plus 18.7s of navigation and would end at 142.9s,
        past the 141s budget, while the same sum without the delay predicts
        140.9s and admits a page the run cannot pay for.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        extractor = LinkedInExtractor(mock_page)
        seen: list[float | None] = []

        async def capture(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            clock.now += 18.7
            return extracted("Job results")

        async def sleep(seconds: float) -> None:
            clock.now += seconds

        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(extractor_module, "time", clock),
            patch.object(extractor, "_extract_search_page", side_effect=capture),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            # 176.25 * _SEARCH_TIMEOUT_FRACTION is a 141s budget.
            result = await extractor.search_jobs(
                "python", max_pages=10, tool_timeout=176.25
            )

        assert len(seen) == 6
        assert result["job_ids"] == [jid for page in pages[:6] for jid in page]

    async def test_zero_max_pages_fetches_nothing(self, mock_page):
        """max_pages=0 should fetch zero pages (validation at tool boundary)."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=0)

        assert result["job_ids"] == []
        assert mock_extract.await_count == 0

    async def test_single_page(self, mock_page):
        """max_pages=1 should only visit one page; filters appear in URL."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted("Job posting text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["42"],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs(
                "python",
                "Remote",
                max_pages=1,
                date_posted="past_week",
                work_type="remote",
                easy_apply=True,
            )

        assert result["job_ids"] == ["42"]
        assert "keywords=python" in result["url"]
        assert "location=Remote" in result["url"]
        assert "f_TPR=r604800" in result["url"]
        assert "f_WT=2" in result["url"]
        assert "f_EA=true" in result["url"]
        assert mock_extract.await_count == 1

    async def test_page_texts_joined_with_separator(self, mock_page):
        """Multiple pages should join text with --- separator."""
        extractor = LinkedInExtractor(mock_page)
        text_pages = iter(["Page 1 content", "Page 2 content"])
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                side_effect=self._navigating(
                    mock_page, lambda _url: extracted(next(text_pages))
                ),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert "\n---\n" in result["sections"]["search_results"]
        assert "Page 1 content" in result["sections"]["search_results"]
        assert "Page 2 content" in result["sections"]["search_results"]
        assert mock_extract.await_count == 2

    async def test_empty_results(self, mock_page):
        """Should handle empty results gracefully and skip ID extraction."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [extracted("")]),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("nonexistent_xyz")

        assert result["job_ids"] == []
        assert result["sections"] == {}
        # Empty text should skip ID extraction to avoid stale DOM
        mock_ids.assert_not_awaited()

    async def test_empty_redesign_page_reports_dropped_keywords(self, mock_page):
        """An empty destination must still prove it answered the question.

        `/jobs/search-results/` without the query can be a blank replacement
        page. Accepting its empty text before comparing keywords reports a
        successful search with no jobs, although LinkedIn answered no search
        at all.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("")],
                    lands_on="https://www.linkedin.com/jobs/search-results/",
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"] == {}
        assert result["section_errors"]["search_results"]["error_type"] == (
            "search_replaced"
        )
        mock_ids.assert_not_awaited()

    async def test_empty_redesign_page_reports_a_dropped_filter(self, mock_page):
        """A clean empty result cannot hide a location the redirect dropped."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("")],
                    lands_on=(
                        "https://www.linkedin.com/jobs/search-results/?keywords=python"
                    ),
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs(
                "python", location="Berlin", max_pages=1
            )

        error = result["section_errors"]["search_results"]
        assert error["error_type"] == "filters_dropped"
        assert "location" in error["error_message"]
        mock_ids.assert_not_awaited()

    async def test_empty_later_page_reports_a_dropped_offset(self, mock_page):
        """A blank first page repeated later must not truncate pagination.

        The first navigation yields one job. The second lands on a bare
        redesign URL with no `start`; without validating before the empty
        short-circuit, the search silently stops and presents page one as the
        complete answer.
        """
        extractor = LinkedInExtractor(mock_page)
        pages = iter([extracted("Page 1"), extracted("")])
        calls = 0

        async def navigate_page(url, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                navigate(mock_page, url)
            else:
                navigate(
                    mock_page,
                    "https://www.linkedin.com/jobs/search-results/?keywords=python",
                )
            return next(pages)

        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=navigate_page,
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["111"]
        assert result["sections"]["search_results"] == "Page 1"
        error = result["section_errors"]["search_results"]
        assert error["error_type"] == "pagination_stopped"
        assert mock_ids.await_count == 1

    async def test_no_ids_on_first_page_captures_text(self, mock_page):
        """Non-empty text with zero job IDs should be returned in sections."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                # Navigating, or the page keeps the fixture's `keywords=python`
                # while the search asks for something else, and the check that
                # the answer is about the question stops the loop.
                side_effect=self._navigating(
                    mock_page, [extracted("No matching jobs found")]
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("xyzzy123", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"]["search_results"] == "No matching jobs found"

    async def test_a_redirect_that_beat_the_scroll_is_still_caught(self, mock_page):
        """The baseline is the URL that was asked for, not the one that arrived.

        A redirect completing during the navigation, before any scrolling,
        leaves the landing page as both ends of the comparison, so it reads
        as a page that never moved and its text is returned as the search.
        """
        mock_page.url = "https://www.linkedin.com/feed/"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
            )

        assert result.text == ""
        assert result.error is not None

    async def test_a_lagging_url_still_shows_the_redirect(self, mock_page):
        """`page.url` reports the address it left, briefly, after a navigation.

        A navigation during the scroll destroys the execution context, the
        evaluate raises, and Patchright publishes the new URL about 6ms
        later, measured over ten runs. Sampling it the moment the scroll
        returns therefore compares two copies of the old address, and the
        redirect the guard exists for passes unseen. Awaiting the load state
        does not help: the previous document is loaded already.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=python"

        async def scroll_then_publish(page, **kwargs):
            async def publish() -> None:
                await asyncio.sleep(0.03)
                navigate(page, "https://www.linkedin.com/checkpoint/challenge/")

            asyncio.get_running_loop().create_task(publish())
            return True

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_job_sidebar",
                side_effect=scroll_then_publish,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
            )

    async def test_a_login_redirect_raises_an_auth_error(self, mock_page):
        """A login wall reached mid-search is an expired session.

        Its text used to come back under `search_results`, with the login
        page's own references beside it, so the caller could not tell it from
        a search that found those words. A section error is not enough
        either: only the auth error starts the relogin the tool has.
        """
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/uas/login"
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "Login page content",
                    [{"kind": "person", "url": "/in/testuser/", "text": "Test User"}],
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(AuthenticationError, match="--login"):
                await extractor.search_jobs("python", max_pages=2)

        mock_ids.assert_not_awaited()

    async def test_a_plain_redirect_is_reported_not_returned(self, mock_page):
        """Anything else that is not the search page is dropped and diagnosed.

        Keeping the landing page's text and references handed a page that is
        not the search back under `search_results`, carrying whatever links
        it held. An empty result with nothing beside it is not an option
        either: that is what an exhausted search looks like.
        """
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/feed/"
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "Feed content",
                    [{"kind": "person", "url": "/in/testuser/", "text": "Test User"}],
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=2)

        mock_ids.assert_not_awaited()
        assert result["job_ids"] == []
        assert "search_results" not in result["sections"]
        assert "references" not in result
        assert "search_results" in result["section_errors"]

    async def test_rate_limited_skips_ids_and_text(self, mock_page):
        """Rate-limited pages should yield no IDs or text."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=extracted(RATE_LIMITED_SECTION_TEXT),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"] == {}
        assert result["section_errors"]["search_results"]["error_type"] == "rate_limit"
        mock_ids.assert_not_awaited()

    async def test_rate_limit_wins_over_an_unexpected_landing(self, mock_page):
        """The specific diagnosis survives a simultaneous route failure."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted(RATE_LIMITED_SECTION_TEXT)],
                    lands_on="https://www.linkedin.com/feed/",
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["section_errors"]["search_results"]["error_type"] == (
            "rate_limit"
        )
        mock_ids.assert_not_awaited()

    async def test_extraction_error_wins_over_a_dropped_query(self, mock_page):
        """A classified extraction failure must not become a route warning."""
        failure = {
            "error_type": "navigation_error",
            "error_message": "the search page did not load",
        }
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("", error=failure)],
                    lands_on="https://www.linkedin.com/jobs/search-results/",
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.search_jobs("python", max_pages=1)

        assert result["section_errors"]["search_results"] == failure
        mock_ids.assert_not_awaited()


class TestGetSavedJobs:
    """Tests for get_saved_jobs with job ID extraction and pagination."""

    async def test_a_reload_at_the_read_is_caught_too(self, mock_page):
        """The check follows the read, so the gap between them is covered.

        Asked before the extraction, it judges a document the returned text
        did not come from: a picker committing in between is extracted and
        returned while the check that just passed says the list is intact.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"
        replaced = mock_page.time_origin

        async def reload_at_read(*args, **kwargs):
            navigate(mock_page)
            return {"source": "root", "text": "Welcome back", "references": []}

        async def barrier(page):
            if mock_page.time_origin == replaced:
                return None
            return "account picker: #rememberme-div"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                side_effect=reload_at_read,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_saved_jobs_page(
                "https://www.linkedin.com/jobs-tracker/",
                section_name="saved_jobs",
            )

    async def test_a_reload_onto_a_picker_while_scrolling_is_an_auth_error(
        self, mock_page
    ):
        """The list is scrolled in rounds, with half a second between them.

        A document replaced in that gap leaves no evaluation to raise, so the
        extraction that follows succeeds against the replacement. The address
        cannot say so, a reload keeping it exactly, and neither can the title,
        the picker carrying this page's own. The browser is then left on a
        barrier while the picker's text is returned as the saved list.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"

        replaced = mock_page.time_origin

        async def reload_in_place(page, **kwargs):
            navigate(mock_page)

        async def barrier(page):
            # The page that was navigated to is healthy; the picker arrives
            # with the replacement. A double that shows it from the start
            # passes wherever the check is placed, including before the
            # scroll, which is the one position that cannot see this.
            if mock_page.time_origin == replaced:
                return None
            return "account picker: #rememberme-div"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                side_effect=reload_in_place,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor._extract_saved_jobs_page(
                "https://www.linkedin.com/jobs-tracker/",
                section_name="saved_jobs",
            )

    @pytest.fixture(autouse=True)
    def _set_saved_jobs_url(self, mock_page):
        mock_page.url = "https://www.linkedin.com/my-items/saved-jobs/"

    @staticmethod
    def _navigating(mock_page, texts, *, lands_on=None):
        """A page double that moves `page.url` the way a navigation does.

        Leaving it fixed makes every page look like the first one, which is
        the very thing the offset check reads. `lands_on` is the address
        LinkedIn answers with, for a redirect that does not keep the offset.
        """
        supply = iter(texts)

        async def navigate(url, *args, **kwargs):
            mock_page.url = lands_on or url
            return next(supply)

        return navigate

    async def test_returns_job_ids(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted("Saved Job 1\nSaved Job 2"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222"],
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == ["111", "222"]
        assert "saved_jobs" in result["sections"]
        assert result["url"] == "https://www.linkedin.com/my-items/saved-jobs/"

    async def test_a_foreign_host_is_not_the_saved_jobs_list(self, mock_page):
        """A substring test accepts any origin serving this path.

        An interstitial or captive portal carrying a single `/jobs/view/`
        anchor would then come back as the account's saved jobs, with no
        `section_errors` to say otherwise, which is a stranger's page
        presented as the user's own list.
        """
        mock_page.url = "https://interstitial.example/my-items/saved-jobs/"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted("Captive portal"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["999"],
            ) as ids,
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == []
        assert "saved_jobs" not in result["sections"]
        assert "references" not in result
        assert "saved_jobs" in result["section_errors"]
        ids.assert_not_called()

    async def test_returns_references(self, mock_page):
        """References are keyed by the section name, per the return contract."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted(
                    "Job 1",
                    [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}],
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=1)

        assert result["references"] == {
            "saved_jobs": [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}]
        }

    async def test_page_texts_joined_with_separator(self, mock_page):
        """Multi-page text is joined so the caller can tell pages apart."""
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(
                    mock_page, [extracted("page one"), extracted("page two")]
                ),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=2)

        assert result["sections"]["saved_jobs"] == "page one\n---\npage two"

    async def test_pagination_uses_start_offset(self, mock_page):
        """The my-items list pages in 10s, not the 25 used by job search."""
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100", "200"], ["300"], ["400"]])
        urls_visited: list[str] = []

        navigate = self._navigating(mock_page, [extracted("page text")] * 3)

        async def mock_extract(url, *args, **kwargs):
            urls_visited.append(url)
            return await navigate(url)

        with (
            patch.object(
                extractor, "_extract_saved_jobs_page", side_effect=mock_extract
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=3)

        assert result["job_ids"] == ["100", "200", "300", "400"]
        assert urls_visited == [
            "https://www.linkedin.com/my-items/saved-jobs/",
            "https://www.linkedin.com/my-items/saved-jobs/?start=10",
            "https://www.linkedin.com/my-items/saved-jobs/?start=20",
        ]

    async def test_early_stop_no_new_ids(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100"], ["100"]])
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(mock_page, [extracted("text")] * 2),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=5)

        assert result["job_ids"] == ["100"]
        # Stops on the repeat page rather than exhausting max_pages
        assert mock_extract.await_count == 2

    async def test_a_picker_without_main_is_an_auth_error(self, mock_page):
        """The picker keeps the list's address, so the route guard clears it.

        Served in place of the list it carries that page's URL and its title,
        and the guard below compares exactly those. Missing `<main>` is what
        is left, and an emptied list has none either, so the barrier check has
        to decide it.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("no main")
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="account picker: #rememberme-div",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor.get_saved_jobs(max_pages=1)

    async def test_a_redirect_while_scrolling_the_list_is_an_auth_error(
        self, mock_page
    ):
        """A navigation destroys the scroll's context, and that error is generic.

        Turned straight into a section diagnostic it hands the caller an empty
        list, leaves the browser registered and offers no relogin, so the next
        call meets the same checkpoint.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"

        async def redirect(page, **kwargs):
            # The address lands after the raise, which is what `page.url` does:
            # measured 20 times out of 20, the URL sampled the moment an
            # evaluate is destroyed is still the page that was left.
            async def land() -> None:
                await asyncio.sleep(0.05)
                navigate(mock_page, "https://www.linkedin.com/checkpoint/challenge/")

            asyncio.get_running_loop().create_task(land())
            # The class patchright raises for this, measured: an `Error`,
            # not a `RuntimeError`. Keeping the double on the real one stops
            # a handler from being narrowed to a class that never arrives.
            raise PatchrightError(
                "Page.evaluate: Execution context was destroyed, "
                "most likely because of a navigation."
            )

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                side_effect=redirect,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor.get_saved_jobs(max_pages=1)

    async def test_a_blank_foreign_page_is_not_an_empty_list(self, mock_page):
        """An empty page returned before the route is judged says nothing.

        A captive portal or interstitial that renders no text broke the loop
        ahead of the guard, so the call came back with no sections, no ids and
        no `section_errors`, which is exactly what an account with nothing
        saved looks like.
        """
        mock_page.url = "https://interstitial.example/blank"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == []
        assert "saved_jobs" in result["section_errors"]

    async def test_an_empty_list_is_still_an_empty_list(self, mock_page):
        """An account with nothing saved renders nothing, and that is not an error."""
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == []
        assert "section_errors" not in result

    async def test_a_dropped_offset_stops_the_list(self, mock_page):
        """The redirect keeps the path and loses the query.

        Measured on 2026-08-21: `/jobs-tracker/?start=10` lands on
        `/jobs-tracker/`, so the second request is served the first page.
        Reading it appends the whole list to itself under `saved_jobs` before
        the no-new-ids branch stops the loop, and every further offset costs
        another navigation for the same page.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("the list")] * 3,
                    lands_on="https://www.linkedin.com/jobs-tracker/",
                ),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100", "200"],
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=3)

        assert result["job_ids"] == ["100", "200"]
        assert result["sections"]["saved_jobs"] == "the list"
        assert mock_extract.await_count == 2
        # An account with eleven saved jobs gets ten and no sign of the rest,
        # which is exactly what an account with ten saved jobs gets.
        assert (
            result["section_errors"]["saved_jobs"]["error_type"] == "pagination_stopped"
        )

    async def test_stops_at_total_pages(self, mock_page):
        """The pager's page count caps pagination below max_pages."""
        extractor = LinkedInExtractor(mock_page)
        id_pages = iter([["100"], ["200"], ["300"]])
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(mock_page, [extracted("text")] * 3),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_total_pages,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=10)

        # Both pages the pager reports, and no more.
        assert mock_extract.await_count == 2
        assert mock_total_pages.await_count == 1
        assert result["job_ids"] == ["100", "200"]

    async def test_rate_limited_page_keeps_earlier_pages(self, mock_page):
        """A rate-limited later page stops pagination without losing page 1.

        Matches the sibling behaviour of ``search_jobs``: the sentinel page
        contributes no text, and the reason pagination stopped is reported so
        the caller can tell "LinkedIn asked us to slow down" apart from "there
        were no more pages" — which look identical otherwise.
        """
        extractor = LinkedInExtractor(mock_page)
        pages = iter([extracted("first page"), extracted(RATE_LIMITED_SECTION_TEXT)])
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                side_effect=lambda *a, **kw: next(pages),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=3)

        assert result["job_ids"] == ["100"]
        # The blocked page contributes nothing; page 1 survives intact.
        assert result["sections"]["saved_jobs"] == "first page"
        assert result["section_errors"]["saved_jobs"]["error_type"] == "rate_limit"

    async def test_the_jobs_tracker_redirect_is_the_list(self, mock_page):
        """LinkedIn answers the saved-jobs URL with a redirect now.

        Measured on 2026-08-21 against an authenticated profile:
        ``/my-items/saved-jobs/`` lands on ``/jobs-tracker/``, and the query
        is dropped on the way, for ``?start=10`` as well. Refusing that
        destination makes every call return an empty list for every account,
        which is indistinguishable from having nothing saved.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted("Saved Job 1"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == ["111"]
        assert result["sections"]["saved_jobs"] == "Saved Job 1"

    async def test_a_login_redirect_raises_an_auth_error(self, mock_page):
        """A redirect to the login wall is an expired session, not a result.

        Mirrors ``search_jobs``. Returning the login page's text under
        `saved_jobs` left the dead browser registered and offered no
        relogin, so the next call walked into the same wall.
        """
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/uas/login"
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted("Login page content"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["999"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(AuthenticationError, match="--login"):
                await extractor.get_saved_jobs(max_pages=2)

        # Never mine IDs off a page that is not the saved-jobs list.
        mock_ids.assert_not_awaited()

    async def test_a_plain_redirect_is_reported_not_returned(self, mock_page):
        """Anything else that is not the list is dropped and diagnosed.

        Keeping the landing page's text and its references handed a
        stranger's page back under `saved_jobs`, carrying whatever job links
        it happened to hold. An empty result with nothing beside it is not
        an option either: that is what an account with nothing saved looks
        like.
        """
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/feed/"
        with (
            patch.object(
                extractor,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=extracted("Some other page"),
            ),
            patch.object(
                extractor,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["999"],
            ) as mock_ids,
            patch.object(
                extractor,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_saved_jobs(max_pages=2)

        mock_ids.assert_not_awaited()
        assert result["job_ids"] == []
        assert "saved_jobs" not in result["sections"]
        assert "references" not in result
        assert "saved_jobs" in result["section_errors"]


class TestSingleSectionRateLimits:
    """The single-page tools report the reason too, not just an empty result.

    Without these, three of the nine repaired call sites would be unbound: the
    branch could be deleted and the suite would stay green, because the older
    tests only assert the sentinel does not reach ``sections``.
    """

    @pytest.mark.parametrize(
        ("method", "args", "section"),
        [
            ("get_company_employees", ("testcorp",), "employees"),
            ("search_people", ("python",), "search_results"),
            ("search_companies", ("fintech",), "search_results"),
        ],
    )
    async def test_the_reason_is_reported(self, mock_page, method, args, section):
        extractor = LinkedInExtractor(mock_page)
        # Patched on the capture owner rather than on the facade: one of the
        # three methods reads through its own collaborator now, and a facade
        # patch would intercept nothing for it while still passing.
        with patch.object(
            SectionCapture,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await getattr(extractor, method)(*args)

        assert result["sections"] == {}
        assert result["section_errors"][section]["error_type"] == "rate_limit"


@pytest.mark.asyncio
class TestSearchPosts:
    async def test_returns_results_and_url(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("We're hiring a Unity dev"),
        ) as mock_extract:
            result = await extractor.search_posts("Buscamos Unity")

        assert "/search/results/content/" in result["url"]
        assert "origin=FACETED_SEARCH" in result["url"]
        assert result["sections"]["search_results"] == "We're hiring a Unity dev"
        # max_pages default (3) -> 15 scrolls
        mock_extract.assert_awaited_once_with(
            ANY, section_name="search_results", max_scrolls=15
        )

    async def test_date_posted_in_url(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ):
            result = await extractor.search_posts(
                "Buscamos Unity", date_posted="past-week"
            )

        assert "datePosted=%5B%22past-week%22%5D" in result["url"]

    async def test_max_pages_controls_scroll_depth(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("post"),
        ) as mock_extract:
            await extractor.search_posts("python", max_pages=2)

        mock_extract.assert_awaited_once_with(
            ANY, section_name="search_results", max_scrolls=10
        )

    async def test_invalid_date_posted_raises(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with pytest.raises(ValueError, match="Invalid date_posted"):
            await extractor.search_posts("python", date_posted="last-year")

        mock_page.goto.assert_not_awaited()

    async def test_empty_results_omit_optional_keys(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(""),
        ):
            result = await extractor.search_posts("nothing matches this query")

        assert result["sections"] == {}
        assert "references" not in result
        assert "section_errors" not in result

    async def test_rate_limited_surfaces_section_error(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await extractor.search_posts("python")

        assert result["sections"] == {}
        assert result["section_errors"]["search_results"]["error_type"] == "rate_limit"

    async def test_navigation_error_surfaces_section_error(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(
                "", error={"error_type": "navigation_error", "error_message": "timeout"}
            ),
        ):
            result = await extractor.search_posts("python")

        assert result["sections"] == {}
        assert result["section_errors"]["search_results"] == {
            "error_type": "navigation_error",
            "error_message": "timeout",
        }


class TestMessageTargetUrls:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
                "ACoAAB",
            ),
            (
                "https://de.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                "ACoAAB",
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=ACoAAB",
                "ACoAAB",
            ),
            ("http://www.linkedin.com/messaging/compose/?recipient=ACoAAB", None),
            ("https://evil.example/messaging/compose/?recipient=ACoAAB", None),
            ("//evil.example/messaging/compose/?recipient=ACoAAB", None),
            ("https://user@www.linkedin.com/messaging/compose/?recipient=ACoAAB", None),
            ("https://www.linkedin.com:444/messaging/compose/?recipient=ACoAAB", None),
            ("https://www.linkedin.com/jobs/?recipient=ACoAAB", None),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB#draft",
                None,
            ),
            ("https://www.linkedin.com/messaging/compose/?recipient=ACoAAB\n", None),
            ("https://www.linkedin.com/messaging/compose/?recipient=", None),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER",
                None,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER",
                None,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?profileUrn=malformed%3Aurn",
                None,
            ),
        ],
    )
    def test_compose_url_requires_one_linkedin_recipient(self, url, expected):
        assert extractor_module._profile_urn_from_compose_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.linkedin.com/in/testuser/", "/in/testuser/"),
            ("https://de.linkedin.com/in/testuser/", "/in/testuser/"),
            ("http://www.linkedin.com/in/testuser/", None),
            ("https://evil.example/in/testuser/", None),
            ("https://user@www.linkedin.com/in/testuser/", None),
            ("https://www.linkedin.com:444/in/testuser/", None),
            ("https://www.linkedin.com/in/testuser/edit/intro/", None),
            ("https://www.linkedin.com/in/testuser%2Fedit/", None),
            ("https://www.linkedin.com/in/testuser/?trk=profile", None),
            ("https://www.linkedin.com/in/testuser/#details", None),
        ],
    )
    def test_profile_url_requires_exact_linkedin_profile(self, url, expected):
        assert extractor_module._profile_path_from_url(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.linkedin.com/messaging/compose/", True),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
                True,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=ACoAAB&"
                "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                True,
            ),
            ("https://de.linkedin.com/messaging/thread/2-abc/", True),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB",
                True,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/compose/?profileUrn=",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/"
                "?recipient=ACoAAB&recipient=OTHER",
                False,
            ),
            (
                "https://www.linkedin.com/messaging/thread/2-abc/?profileUrn=",
                False,
            ),
            ("http://www.linkedin.com/messaging/compose/", False),
            ("https://evil.example/messaging/compose/", False),
            ("https://user@www.linkedin.com/messaging/thread/2-abc/", False),
            ("https://www.linkedin.com:444/messaging/compose/", False),
            ("https://www.linkedin.com/messaging/compose/#draft", False),
            ("https://www.linkedin.com/messaging/thread/2-abc%2Fother/", False),
            # Measured live: LinkedIn redirects an existing conversation to a
            # padded base64url id, and the padding reaches the path unescaped.
            (
                "https://www.linkedin.com/messaging/thread/"
                "2-ZDBkMjZiY2UtNjQwYi00NzczLWIxYWYtNTczZTZhZDkzMzQ4XzEwMA==/",
                True,
            ),
            ("https://www.linkedin.com/feed/", False),
        ],
    )
    def test_final_url_requires_safe_messaging_path(self, url, expected):
        assert extractor_module._message_page_url_is_safe(url, "ACoAAB") is expected


class TestReadProfileMessageTarget:
    async def test_accepts_safe_final_vanity_redirect(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "status": "resolved",
                "pageUrl": "https://www.linkedin.com/in/canonical-user/",
                "displayName": "Test User",
                "composeHrefs": [
                    "/messaging/compose/?recipient=ACoAAB&"
                    "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
                ],
            }
        )

        resolution = await LinkedInExtractor(mock_page)._read_profile_message_target()

        assert resolution.status == "resolved"
        assert resolution.target is not None
        assert resolution.target.profile_path == "/in/canonical-user/"
        assert resolution.target.profile_urn == "ACoAAB"


class TestGetInbox:
    async def test_returns_inbox_section(self, mock_page):
        """get_inbox returns sections with inbox key."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_wait_for_main_text",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_scroll_main_scrollable_region",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={
                    "text": "Conversation A\nConversation B",
                    "references": [],
                },
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Conversation A\nConversation B",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.get_inbox(limit=10)

        assert "sections" in result
        assert "inbox" in result["sections"]
        assert "Conversation A" in result["sections"]["inbox"]

    async def test_empty_inbox(self, mock_page):
        """get_inbox returns empty sections when page has no content."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="",
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.get_inbox(limit=5)

        assert result["sections"] == {}

    async def test_includes_conversation_thread_refs(self, mock_page):
        """get_inbox prepends conversation thread references from click extraction."""
        extractor = LinkedInExtractor(mock_page)
        thread_refs = [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-abc123/",
                "text": "Tony Chan",
                "context": "inbox",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-def456/",
                "text": "Paul Jasper",
                "context": "inbox",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={
                    "text": "Tony Chan\nPaul Jasper",
                    "references": [],
                },
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Tony Chan\nPaul Jasper",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            result = await extractor.get_inbox(limit=10)

        assert "references" in result
        refs = result["references"]["inbox"]
        assert len(refs) == 2
        assert refs[0]["kind"] == "conversation"
        assert refs[0]["url"] == "/messaging/thread/2-abc123/"
        assert refs[0]["text"] == "Tony Chan"


class TestGetConversation:
    async def test_returns_conversation_by_thread_id(self, mock_page):
        """get_conversation with thread_id navigates directly to thread URL."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Hello!\nHi there!", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Hello!\nHi there!",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            result = await extractor.get_conversation(thread_id="abc123")

        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/thread/abc123/"
        )
        assert result["sections"]["conversation"] == "Hello!\nHi there!"

    async def test_strips_conversation_page_chrome(self, mock_page):
        """get_conversation trims sidebar and composer chrome from the thread."""
        raw = (
            "Ada: Preview belonging to a different conversation\n"
            "Open the options list in your conversation with Ada and Grace\n"
            "Hello!\n"
            "Maximize compose field\n"
            "Open send options"
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": raw, "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            result = await extractor.get_conversation(thread_id="abc123")

        assert result["sections"]["conversation"] == "Hello!"

    async def test_raises_when_no_identifier(self, mock_page):
        """get_conversation raises LinkedInScraperException with no args."""
        extractor = LinkedInExtractor(mock_page)
        with pytest.raises(LinkedInScraperException):
            await extractor.get_conversation()

    async def test_by_username_default_index_picks_first_thread(self, mock_page):
        """get_conversation by username opens the 0th matching thread by default."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-newer/",
                    "https://www.linkedin.com/messaging/thread/2-older/",
                ],
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "msg", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="msg",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            await extractor.get_conversation(linkedin_username="jacki-old")

        target_calls = [
            c.args[0]
            for c in nav_mock.call_args_list
            if c.args and "/messaging/thread/" in c.args[0]
        ]
        assert target_calls == ["https://www.linkedin.com/messaging/thread/2-newer/"]

    async def test_by_username_index_picks_specified_thread(self, mock_page):
        """get_conversation by username + index opens the i-th matching thread."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-newer/",
                    "https://www.linkedin.com/messaging/thread/2-older/",
                ],
            ),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "msg", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="msg",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
        ):
            await extractor.get_conversation(linkedin_username="jacki-old", index=1)

        target_calls = [
            c.args[0]
            for c in nav_mock.call_args_list
            if c.args and "/messaging/thread/" in c.args[0]
        ]
        assert target_calls == ["https://www.linkedin.com/messaging/thread/2-older/"]

    async def test_by_username_index_out_of_range_raises(self, mock_page):
        """get_conversation raises when index exceeds the number of threads."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[
                    "https://www.linkedin.com/messaging/thread/2-only/",
                ],
            ),
        ):
            with pytest.raises(LinkedInScraperException, match="out of range"):
                await extractor.get_conversation(linkedin_username="jacki-old", index=5)

    async def test_by_username_no_threads_raises_could_not_find(self, mock_page):
        """get_conversation raises 'Could not find a conversation' when none exist."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_selector = AsyncMock()
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor._profile_page,
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Jacki McMahan",
            ),
            patch.object(
                extractor,
                "_resolve_conversation_thread_urls",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            with pytest.raises(
                LinkedInScraperException, match="Could not find a conversation"
            ):
                await extractor.get_conversation(linkedin_username="jacki-old")


class TestStripSelectConversationPrefix:
    def test_strips_en_us_prefix(self):
        """Best-effort strip removes the en-US 'Select conversation with ' prefix."""
        assert (
            LinkedInExtractor._strip_select_conversation_prefix(
                "Select conversation with Jacki McMahan"
            )
            == "Jacki McMahan"
        )

    def test_case_insensitive(self):
        assert (
            LinkedInExtractor._strip_select_conversation_prefix(
                "select conversation with jacki mcmahan"
            )
            == "jacki mcmahan"
        )

    def test_returns_full_aria_when_prefix_absent(self):
        """In a non-en-US locale the verb prefix won't match; return as-is so
        downstream matching can endsWith / endswith on the participant name."""
        assert (
            LinkedInExtractor._strip_select_conversation_prefix(
                "Konversation auswählen mit Jacki McMahan"
            )
            == "Konversation auswählen mit Jacki McMahan"
        )

    def test_empty_input(self):
        assert LinkedInExtractor._strip_select_conversation_prefix("") == ""


class TestResolveConversationThreadUrls:
    async def test_inbox_enumeration_and_exact_aria_match(self, mock_page):
        """_resolve_conversation_thread_urls enumerates the plain inbox and
        matches participant by exact aria-label rather than substring."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        thread_refs = [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-aaa/",
                "text": "Jacki McMahan",  # exact match
                "context": "search",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-bbb/",
                "text": "Jacki McMahan-Group",  # extra suffix → not exact
                "context": "search",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-ccc/",
                "text": "Jacki McMahan",  # second exact match (multi-thread case)
                "context": "search",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ),
        ):
            urls = await extractor._resolve_conversation_thread_urls("Jacki McMahan")

        nav_mock.assert_awaited_once_with("https://www.linkedin.com/messaging/")
        assert urls == [
            "https://www.linkedin.com/messaging/thread/2-aaa/",
            "https://www.linkedin.com/messaging/thread/2-ccc/",
        ]

    async def test_resolver_passes_name_filter_to_enumerator(self, mock_page):
        """_resolve_conversation_thread_urls scopes the click side effect by
        forwarding name_filter so only the participant's row is clicked."""
        extractor = LinkedInExtractor(mock_page)
        refs_mock = AsyncMock(
            return_value=[
                {
                    "kind": "conversation",
                    "url": "/messaging/thread/2-aaa/",
                    "text": "Jacki McMahan",
                    "context": "inbox",
                },
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(extractor, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await extractor._resolve_conversation_thread_urls("Jacki McMahan")

        refs_mock.assert_awaited_once_with(
            limit=ANY, context="inbox", name_filter="Jacki McMahan"
        )
        assert urls == ["https://www.linkedin.com/messaging/thread/2-aaa/"]

    async def test_resolver_falls_back_to_search_when_inbox_empty(self, mock_page):
        """When the inbox scan finds no match, resolution falls back to the
        messaging search for threads buried below the inbox window."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()
        # First call (inbox) finds nothing; second call (search) finds the thread.
        refs_mock = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "kind": "conversation",
                        "url": "/messaging/thread/2-ddd/",
                        "text": "Jacki McMahan",
                        "context": "search",
                    },
                ],
            ]
        )
        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor, "_scroll_main_scrollable_region", new_callable=AsyncMock
            ),
            patch.object(extractor, "_extract_conversation_thread_refs", refs_mock),
        ):
            urls = await extractor._resolve_conversation_thread_urls("Jacki McMahan")

        assert nav_mock.await_args_list[0].args == (
            "https://www.linkedin.com/messaging/",
        )
        assert nav_mock.await_args_list[1].args == (
            "https://www.linkedin.com/messaging/?searchTerm=Jacki+McMahan",
        )
        assert refs_mock.await_count == 2
        assert urls == ["https://www.linkedin.com/messaging/thread/2-ddd/"]

    async def test_extract_refs_threads_name_filter_into_evaluate(self, mock_page):
        """_extract_conversation_thread_refs forwards name_filter into the
        in-browser click loop so non-matching rows are never clicked."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_selector = AsyncMock()
        captured: dict[str, object] = {}

        async def fake_evaluate(_js: str, arg: dict | None = None) -> list:
            captured["arg"] = arg
            return []

        mock_page.evaluate = fake_evaluate

        await extractor._extract_conversation_thread_refs(
            limit=50, context="inbox", name_filter="Jacki McMahan"
        )

        assert captured["arg"] == {"limit": 50, "nameFilter": "Jacki McMahan"}


class TestSearchConversations:
    async def test_returns_search_results(self, mock_page):
        """search_conversations returns search_results section."""
        extractor = LinkedInExtractor(mock_page)
        nav_mock = AsyncMock()

        with (
            patch.object(PageNavigator, "_navigate_to_page", nav_mock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Result 1\nResult 2", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Result 1\nResult 2",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await extractor.search_conversations("hello world")

        assert "search_results" in result["sections"]
        assert "Result 1" in result["sections"]["search_results"]
        # Search must be driven by the searchTerm URL parameter, not by typing
        # into the searchbox -- the URL form is reliable across SPA mounts and
        # preserves the search filter across click-to-capture navigations.
        nav_mock.assert_awaited_once_with(
            "https://www.linkedin.com/messaging/?searchTerm=hello+world"
        )

    async def test_includes_conversation_thread_refs(self, mock_page):
        """search_conversations exposes per-result thread URLs as references."""
        extractor = LinkedInExtractor(mock_page)
        thread_refs = [
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-abc/",
                "text": "Jacki McMahan",
                "context": "search_results",
            },
            {
                "kind": "conversation",
                "url": "/messaging/thread/2-def/",
                "text": "Jacki McMahan",
                "context": "search_results",
            },
        ]
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.handle_modal_close",
                new_callable=AsyncMock,
            ),
            patch.object(extractor, "_wait_for_main_text", new_callable=AsyncMock),
            patch.object(
                extractor._content,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={"text": "Jacki McMahan\nJacki McMahan", "references": []},
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.strip_linkedin_noise",
                return_value="Jacki McMahan\nJacki McMahan",
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_references",
                return_value=[],
            ),
            patch.object(
                extractor,
                "_extract_conversation_thread_refs",
                new_callable=AsyncMock,
                return_value=thread_refs,
            ) as mock_refs,
        ):
            result = await extractor.search_conversations("Jacki")

        mock_refs.assert_awaited_once_with(limit=20, context="search_results")
        refs = result["references"]["search_results"]
        assert len(refs) == 2
        assert {ref["url"] for ref in refs} == {
            "/messaging/thread/2-abc/",
            "/messaging/thread/2-def/",
        }


class TestSendMessage:
    @pytest.mark.parametrize("message", ["", " \t\n"], ids=["empty", "whitespace"])
    async def test_blank_message_is_rejected_before_browser_interaction(
        self, mock_page, message
    ):
        extractor = LinkedInExtractor(mock_page)
        keyboard = MagicMock()
        mock_page.keyboard = keyboard

        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate:
            result = await extractor.send_message(
                "testuser", message, confirm_send=True
            )

        # Not `message_unavailable`: that status is about the recipient and
        # tells a caller to move on, while this one is about their own input.
        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "status": "invalid_message",
            "message": "Message must contain non-whitespace characters.",
            "recipient_selected": False,
            "sent": False,
            "retry_safe": True,
        }
        navigate.assert_not_awaited()
        mock_page.evaluate.assert_not_awaited()
        keyboard.type.assert_not_called()
        keyboard.press.assert_not_called()

    @pytest.mark.parametrize(
        "message",
        ["First\nSecond", "First\rSecond", "First\tSecond", "First\x7fSecond"],
        ids=["newline", "carriage-return", "tab", "del"],
    )
    async def test_control_message_is_rejected_before_browser_interaction(
        self, mock_page, message
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())

        with patch.object(
            PageNavigator, "_navigate_to_page", new_callable=AsyncMock
        ) as navigate:
            result = await extractor.send_message(
                "testuser", message, confirm_send=True
            )

        assert result["status"] == "invalid_message"
        assert result["message"] == (
            "Message must not contain control characters or line breaks."
        )
        assert result["retry_safe"] is True
        navigate.assert_not_awaited()
        mock_page.evaluate.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_unavailable_message_action_returns_connection_handoff(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())

        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution(
                    "unavailable"
                ),
            ),
            patch.object(
                extractor, "_wait_for_message_surface", new_callable=AsyncMock
            ) as surface,
            patch.object(
                extractor, "_read_message_composer_state", new_callable=AsyncMock
            ) as state,
            patch.object(
                extractor,
                "_focus_verified_message_editor",
                new_callable=AsyncMock,
            ) as focus,
            patch.object(
                extractor, "_submit_verified_message", new_callable=AsyncMock
            ) as submit,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "status": "message_unavailable",
            "message": (
                "LinkedIn did not expose a normal Message action for this profile. "
                "Use connect_with_person first, then retry only after the connection "
                "request is accepted."
            ),
            "recipient_selected": False,
            "sent": False,
            "retry_safe": True,
        }
        navigate.assert_awaited_once_with("https://www.linkedin.com/in/testuser/")
        surface.assert_not_awaited()
        state.assert_not_awaited()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_unresolved_profile_target_is_not_connection_handoff(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution("failed"),
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert "connect_with_person" not in result["message"]
        assert result["retry_safe"] is True

    @staticmethod
    def _target():
        return extractor_module._ProfileMessageTarget(
            profile_path="/in/testuser/",
            profile_urn="ACoAAB",
            compose_url=(
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB"
            ),
            display_name="Test User",
        )

    @staticmethod
    def _patch_to_composer(
        extractor,
        mock_page,
        *,
        states=None,
        submission="clicked",
        write_result="written",
    ):
        target = TestSendMessage._target()
        mock_page.url = "https://www.linkedin.com/messaging/compose/?recipient=ACoAAB"
        mock_page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())
        owner = MagicMock()
        owner.as_element.return_value = owner
        owner.evaluate = AsyncMock(return_value="ready")
        owner.dispose = AsyncMock()
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        # An empty composer is the ordinary precondition for sending, so a
        # state that says nothing about it means empty. A case about a draft
        # still standing in the editor says `"empty": False` and gets it.
        def with_empty(state):
            if not isinstance(state, dict):
                return state
            return {
                "empty": True,
                "submitCount": 1,
                "submitUsable": True,
                **state,
            }

        if callable(states):
            inner = states

            async def states(*args, **kwargs):
                return with_empty(await inner(*args, **kwargs))
        elif states is not None:
            states = [with_empty(state) for state in states]
        return (
            target,
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution(
                    "resolved", target
                ),
            ),
            patch.object(
                extractor,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                extractor,
                "_read_message_composer_state",
                new_callable=AsyncMock,
                side_effect=states or None,
                return_value={
                    "status": "valid",
                    "active": False,
                    "empty": True,
                    "submitCount": 1,
                    "submitUsable": True,
                },
            ),
            patch.object(
                extractor,
                "_write_verified_message",
                new_callable=AsyncMock,
                return_value=write_result,
            ),
            patch.object(
                extractor,
                "_submit_verified_message",
                new_callable=AsyncMock,
                return_value=submission,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value="confirmation-token",
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        )

    async def test_dry_run_returns_before_focus_or_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=False
            )

        assert result["status"] == "confirmation_required"
        assert result["recipient_selected"] is True
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_supplied_urn_before_compose_navigation(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target = self._target()
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch.object(
                extractor,
                "_read_profile_message_target",
                new_callable=AsyncMock,
                return_value=extractor_module._ProfileMessageTargetResolution(
                    "resolved", target
                ),
            ),
        ):
            result = await extractor.send_message(
                "testuser",
                "Hello!",
                confirm_send=True,
                profile_urn="OTHER",
            )

        assert result["status"] == "recipient_resolution_failed"
        navigate.assert_awaited_once_with("https://www.linkedin.com/in/testuser/")

    async def test_rejects_foreign_url_recipient_after_navigation(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        mock_page.url = "https://www.linkedin.com/messaging/compose/?recipient=OTHER"
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4] as surface,
            patches[5] as state,
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        surface.assert_not_awaited()
        state.assert_not_awaited()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_contradictory_url_before_focus(self, mock_page):
        extractor = LinkedInExtractor(mock_page)

        async def change_url_after_initial_state(_target):
            mock_page.url = (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&recipient=OTHER"
            )
            return {"status": "valid", "active": False}

        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=change_url_after_initial_state,
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5] as state,
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        state.assert_awaited_once()
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_foreign_url_recipient_before_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        state_calls = 0

        async def change_url_during_prefocus_state(_target):
            nonlocal state_calls
            state_calls += 1
            if state_calls == 2:
                mock_page.url = (
                    "https://www.linkedin.com/messaging/compose/?recipient=OTHER"
                )
            return {"status": "valid", "active": state_calls > 1}

        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=change_url_during_prefocus_state,
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_queryless_route_switch_during_surface_wait_fails_closed(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        mock_page.url = alice_route

        async def switch_route(_target):
            mock_page.url = bob_route
            return "composer"

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4] as surface,
            patches[5] as state,
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            surface.side_effect = switch_route
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert result["retry_safe"] is True
        assert result["url"] == bob_route
        state.assert_not_awaited()
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_queryless_route_is_captured_before_owner_resolution(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        alice_route = "https://www.linkedin.com/messaging/thread/ALICE/"
        bob_route = "https://www.linkedin.com/messaging/thread/BOB/"
        mock_page.url = alice_route

        async def switch_route(target, *, expected_route):
            assert target == self._target()
            assert expected_route == alice_route
            mock_page.url = bob_route
            return None

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
            patch.object(
                extractor,
                "_resolve_message_owner",
                new_callable=AsyncMock,
                side_effect=switch_route,
            ) as resolve_owner,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert result["retry_safe"] is True
        resolve_owner.assert_awaited_once_with(
            self._target(), expected_route=alice_route
        )
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_rejects_contradictory_url_before_submission(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        async def change_url_during_write(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            mock_page.url = (
                "https://www.linkedin.com/messaging/compose/"
                "?recipient=ACoAAB&profileUrn=urn%3Ali%3Afsd_profile%3AOTHER"
            )
            return "invalid"

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            write.side_effect = change_url_during_write
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        write.assert_awaited_once()
        mock_page.keyboard.type.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_refuses_a_composer_that_already_holds_a_draft(self, mock_page):
        """A draft in the editor is not ours to send, and not ours to clear."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            # The recipient check first, then the read taken immediately
            # before focus: that one still finds the author's draft.
            states=[
                {"status": "valid", "active": False},
                {"status": "valid", "active": False, "empty": False},
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "composer_occupied"
        assert result["sent"] is False
        # Nothing is typed, nothing is submitted, and the draft is left where
        # its author put it.
        focus.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_rejects_recipient_change_before_focus(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=[
                {"status": "valid", "active": False},
                {"status": "recipient_mismatch", "active": False},
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as focus,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "compose_interact_failed"
        focus.assert_not_awaited()
        mock_page.keyboard.type.assert_not_awaited()

    async def test_rejects_editor_change_before_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=[
                {"status": "valid", "active": False},
                {"status": "valid", "active": False},
            ],
            write_result="invalid",
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "compose_interact_failed"
        mock_page.keyboard.type.assert_not_awaited()

    async def test_missing_owner_is_retryable_before_dispatch(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        owner = mock_page.evaluate_handle.return_value
        owner.as_element.return_value = None
        with ExitStack() as stack:
            entered = [stack.enter_context(item) for item in patches[1:]]
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "recipient_resolution_failed"
        assert result["sent"] is False
        assert result["retry_safe"] is True
        entered[6].assert_not_awaited()
        owner.dispose.assert_awaited_once_with()

    async def test_rejects_ambiguous_submit_after_text_entry(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page, submission="invalid")
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_awaited_once()
        mock_page.keyboard.type.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_disabled_pinned_submit_cleans_before_retryable_failure(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        owner = mock_page.evaluate_handle.return_value
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9] as prepare,
            patches[10],
            patch.object(
                extractor,
                "_wait_for_verified_submit",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                extractor, "_cleanup_owned_message", new_callable=AsyncMock
            ) as cleanup,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_awaited_once()
        prepare.assert_not_awaited()
        submit.assert_not_awaited()
        cleanup.assert_awaited_once_with("Hello!", owner)

    @pytest.mark.parametrize(
        ("submit_count", "submit_usable"),
        [(0, False), (2, False)],
        ids=["missing", "ambiguous"],
    )
    async def test_only_one_active_submit_path_can_send(
        self, mock_page, submit_count, submit_usable
    ):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(
            extractor,
            mock_page,
            states=[
                {"status": "valid"},
                {
                    "status": "valid",
                    "submitCount": submit_count,
                    "submitUsable": submit_usable,
                },
            ],
        )
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["retry_safe"] is True
        write.assert_not_awaited()
        submit.assert_not_awaited()
        mock_page.keyboard.press.assert_not_awaited()

    async def test_observer_is_prepared_after_typing_and_before_submission(
        self, mock_page
    ):
        """The mutation observer starts immediately before the only submit."""
        extractor = LinkedInExtractor(mock_page)
        steps: list[str] = []
        patches = self._patch_to_composer(extractor, mock_page)

        async def write(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("write")
            return "written"

        async def prepare(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("prepare")
            return "confirmation-token"

        async def submit(message, *, target, owner):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append("submit")
            return "clicked"

        async def confirmed(message, *, target, owner, confirmation):
            assert message == "Hello!"
            assert target == self._target()
            assert owner is mock_page.evaluate_handle.return_value
            steps.append(f"confirm:{confirmation}")
            return True

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patch.object(
                extractor,
                "_write_verified_message",
                new_callable=AsyncMock,
                side_effect=write,
            ),
            patch.object(
                extractor,
                "_submit_verified_message",
                new_callable=AsyncMock,
                side_effect=submit,
            ),
            patches[8],
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                side_effect=prepare,
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                side_effect=confirmed,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "sent"
        assert steps == [
            "write",
            "prepare",
            "submit",
            "confirm:confirmation-token",
        ]

    async def test_interrupted_submission_is_not_a_failure(self, mock_page):
        """A click round trip can fail after dispatching the local event."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        visible = AsyncMock()

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as submit,
            patches[8],
            patches[9],
            patch.object(extractor, "_message_send_confirmed", visible),
        ):
            submit.side_effect = PatchrightError("execution context was destroyed")
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        visible.assert_not_awaited()

    @pytest.mark.parametrize(
        "stage",
        ["dispatch", "confirmation", "owner-cleanup"],
    )
    async def test_cancellation_after_dispatch_is_logged(
        self, mock_page, caplog, stage
    ):
        """Cancellation in the destructive window leaves a warning behind."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        if stage == "owner-cleanup":
            mock_page.evaluate_handle.return_value.dispose = AsyncMock(
                side_effect=asyncio.CancelledError()
            )

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7] as submit,
            patches[8],
            patches[9],
            patches[10] as confirmed,
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.extractor"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            if stage == "dispatch":
                submit.side_effect = asyncio.CancelledError()
            elif stage == "confirmation":
                confirmed.side_effect = asyncio.CancelledError()
            await extractor.send_message("testuser", "Hello!", confirm_send=True)

        # Cancellation has to keep propagating, or the surrounding scope
        # never unwinds. The warning names the duplicate-delivery risk that
        # the discarded result can no longer report.
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("retry may deliver the message twice" in w for w in warnings), (
            warnings
        )

    async def test_cancellation_while_writing_does_not_warn(self, mock_page, caplog):
        """Validated text cannot submit before the explicit submit path."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            caplog.at_level(
                logging.WARNING, logger="linkedin_mcp_server.scraping.extractor"
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            write.side_effect = asyncio.CancelledError()
            await extractor.send_message("testuser", "Hello there!", confirm_send=True)

        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("retry may deliver the message twice" in w for w in warnings)

    async def test_ordinary_error_after_dispatch_still_answers(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10] as confirmed,
        ):
            confirmed.side_effect = RuntimeError("context destroyed")
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False

    async def test_owner_cleanup_runs_when_confirmation_cleanup_fails(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        owner = mock_page.evaluate_handle.return_value

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            patch.object(
                extractor,
                "_dispose_message_confirmation",
                new_callable=AsyncMock,
                side_effect=RuntimeError("cleanup failed"),
            ),
            patch.object(
                extractor, "_dispose_message_owner", new_callable=AsyncMock
            ) as dispose_owner,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unconfirmed"
        assert result["retry_safe"] is False
        dispose_owner.assert_awaited_once_with(owner)

    async def test_an_error_before_anything_can_submit_is_raised(self, mock_page):
        """Without a newline nothing has submitted yet, so the error is the answer.

        The pair to the case above. Reporting `send_unconfirmed` here would
        claim a duplicate-delivery risk that cannot exist and take the real
        error away from a caller who can simply retry.
        """
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)

        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as write,
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            pytest.raises(RuntimeError, match="page closed"),
        ):
            write.side_effect = RuntimeError("page closed")
            await extractor.send_message("testuser", "Single line", confirm_send=True)

    async def test_send_unconfirmed_when_click_adds_nothing(self, mock_page):
        """A clicked Send button that changes nothing is not a sent message."""
        extractor = LinkedInExtractor(mock_page)
        patches = self._patch_to_composer(extractor, mock_page)
        with (
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patch.object(
                extractor,
                "_prepare_message_confirmation",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch.object(
                extractor,
                "_message_send_confirmed",
                new_callable=AsyncMock,
                return_value=False,
            ) as visible,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        # The click happened, so nothing here proves the message did not go
        # out. Answering "not sent" would invite a retry that delivers twice,
        # which is what `retry_safe` says and `sent` cannot.
        assert result["status"] == "send_unconfirmed"
        assert result["sent"] is False
        assert result["retry_safe"] is False
        visible.assert_awaited_once_with(
            "Hello!",
            target=self._target(),
            owner=mock_page.evaluate_handle.return_value,
            confirmation=1,
        )


class TestResolveMessageComposeBox:
    async def test_requires_exactly_one_visible_editor(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        locator = MagicMock(count=AsyncMock(return_value=2))
        locator.first = MagicMock()
        mock_page.locator.return_value = locator

        assert await extractor._resolve_message_compose_box() is None

        mock_page.locator.assert_called_once_with(
            f"{extractor_module._MESSAGING_COMPOSE_SELECTOR}:visible"
        )


class TestMessageConfirmation:
    """Tests for the owner-pinned message-list mutation contract."""

    @staticmethod
    def _arguments():
        target = TestSendMessage._target()
        owner = MagicMock()
        return target, owner

    async def test_owner_handle_uses_the_shared_recipient_inspection(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        owner.as_element.return_value = owner
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        expected_route = "https://www.linkedin.com/messaging/thread/ALICE/"

        assert (
            await extractor._resolve_message_owner(
                target, expected_route=expected_route
            )
            is owner
        )

        mock_page.evaluate_handle.assert_awaited_once_with(
            _MESSAGE_COMPOSER_OWNER_JS,
            arg={
                "target": {
                    "profilePath": target.profile_path,
                    "profileUrn": target.profile_urn,
                },
                "expectedRoute": expected_route,
            },
        )

    async def test_invalid_owner_handle_is_released(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        owner.as_element.return_value = None
        owner.dispose = AsyncMock()
        mock_page.evaluate_handle = AsyncMock(return_value=owner)

        assert (
            await extractor._resolve_message_owner(
                target,
                expected_route="https://www.linkedin.com/messaging/thread/ALICE/",
            )
            is None
        )
        owner.dispose.assert_awaited_once_with()

    async def test_owner_disposal_error_is_suppressed(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        owner = MagicMock(dispose=AsyncMock(side_effect=RuntimeError("closed")))

        await extractor._dispose_message_owner(owner)

        owner.dispose.assert_awaited_once_with()

    async def test_prepare_installs_observer_in_the_target_owner(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.evaluate = AsyncMock(return_value="confirmation-token")

        assert (
            await extractor._prepare_message_confirmation(
                "Hello!", target=target, owner=owner
            )
            == "confirmation-token"
        )
        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_PREPARE_JS,
            {
                "profilePath": target.profile_path,
                "profileUrn": target.profile_urn,
                "expected": "Hello!",
                "owner": owner,
            },
        )

    @pytest.mark.parametrize("result", [None, "", 0, {"token": "wrong"}])
    async def test_invalid_prepare_result_fails_closed(self, mock_page, result):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.evaluate = AsyncMock(return_value=result)

        assert (
            await extractor._prepare_message_confirmation(
                "Hello!", target=target, owner=owner
            )
            is None
        )

    async def test_confirmation_waits_for_the_exact_token(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.wait_for_function = AsyncMock(return_value=None)

        assert (
            await extractor._message_send_confirmed(
                "Hello!",
                target=target,
                owner=owner,
                confirmation="confirmation-token",
            )
            is True
        )
        mock_page.wait_for_function.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_READY_JS,
            arg={
                "profilePath": target.profile_path,
                "profileUrn": target.profile_urn,
                "expected": "Hello!",
                "owner": owner,
                "token": "confirmation-token",
            },
        )

    @pytest.mark.parametrize(
        "error",
        [
            PlaywrightTimeoutError("timeout"),
            PatchrightError("execution context destroyed"),
        ],
        ids=["timeout", "context-destroyed"],
    )
    async def test_confirmation_errors_do_not_confirm(self, mock_page, error):
        extractor = LinkedInExtractor(mock_page)
        target, owner = self._arguments()
        mock_page.wait_for_function = AsyncMock(side_effect=error)

        assert (
            await extractor._message_send_confirmed(
                "Hello!",
                target=target,
                owner=owner,
                confirmation="confirmation-token",
            )
            is False
        )

    async def test_dispose_disconnects_the_owner_token(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        _target, owner = self._arguments()
        mock_page.evaluate = AsyncMock()

        await extractor._dispose_message_confirmation(owner, "confirmation-token")

        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_CONFIRMATION_DISPOSE_JS,
            {"owner": owner, "token": "confirmation-token"},
        )


class TestEveryNormalizedEntryPoint:
    """Each method that was rewired, refusing a value that redirects the path.

    Without this, removing normalization from one method leaves every other test
    untouched: the bare-identifier assertions build the same URL either way. The
    traversal value is the one input whose result differs, and it has to fail
    before any navigation rather than after one.
    """

    @staticmethod
    def _calls(extractor: LinkedInExtractor):
        # Both patches land on the owner class rather than on the facade
        # instance, because two of the parameterized methods now reach the
        # capture through a collaborator of their own. A facade patch would
        # intercept nothing for those and the assertion would hold whatever
        # the code did.
        return (
            patch.object(SectionCapture, "extract_page", new_callable=AsyncMock),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
        )

    @pytest.mark.parametrize(
        "method,args,kwargs",
        [
            ("scrape_person", ("../../feed", {"main_profile"}), {}),
            ("connect_with_person", ("../../feed",), {}),
            ("get_sidebar_profiles", ("../../feed",), {}),
            ("_open_conversation_by_username", ("../../feed",), {}),
            ("send_message", ("../../feed", "hi"), {"confirm_send": False}),
            ("scrape_company", ("../../feed", {"about"}), {}),
            ("get_company_employees", ("../../feed",), {}),
            ("scrape_job", ("../../feed",), {}),
            ("get_conversation", (), {"thread_id": "../../feed"}),
        ],
    )
    async def test_refuses_a_traversal_value_before_navigating(
        self, mock_page, method: str, args: tuple, kwargs: dict
    ):
        extractor = LinkedInExtractor(mock_page)
        extract_patch, navigate_patch = self._calls(extractor)
        with extract_patch as mock_extract, navigate_patch as mock_navigate:
            with pytest.raises(InvalidReferenceError):
                await getattr(extractor, method)(*args, **kwargs)
        mock_extract.assert_not_called()
        mock_navigate.assert_not_called()
