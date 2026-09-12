"""Tests for the job-page reader behind the job list workflows."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import asyncio

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping import job_pages as job_pages_module
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import ExtractedSection
from linkedin_mcp_server.scraping.job_pages import JobPageReader, _ScrollCharge
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from scraping.support.navigation import navigate


def _reader(page) -> JobPageReader:
    """Wire the page reader the way the facade does."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    return JobPageReader(session, navigator, PageContentReader(session))


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
        reader = _reader(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=AuthenticationError("Run with --login"),
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
                charge=_ScrollCharge(),
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

        reader = _reader(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=redirect_to_the_redesign,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await reader._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
                charge=_ScrollCharge(),
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

        reader = _reader(mock_page)
        with (
            patch.object(
                PageNavigator,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=redirect_to_the_feed,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(RuntimeError, match="Page navigated to .*/feed/"),
        ):
            await reader._extract_search_page_once(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
                charge=_ScrollCharge(),
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

        reader = _reader(mock_page)
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
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page_once(
                requested,
                section_name="search_results",
                charge=_ScrollCharge(),
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=navigate_away,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=navigate_away,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert capture.section.text == ""
        assert capture.section.error is not None
        assert "Some other page" not in str(capture.section.error)

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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=reload_in_place,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
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
            await reader._extract_search_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )
        assert capture.section.error is None
        # The body fallback is what carries that page, so an empty section
        # here would discard the very text this branch exists to keep: the
        # no-results notice, or whatever diagnostic LinkedIn rendered instead.
        assert capture.section.text == "Sample page text"

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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=scroll_then_reload,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="account picker: #rememberme-div",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=scroll_then_name_a_job,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                barrier,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert capture.section.text
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

        The charge reaches the workflow on the capture now rather than on an
        ambient field, so this asserts the value the caller is actually
        handed.
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

        reader = _reader(mock_page)
        with (
            patch.object(job_pages_module, "time", clock),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=scroll,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
                scroll_deadline=7.0,
            )

        assert seen == [7.0]
        assert capture.scroll_seconds == 3.0

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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                reader._content,
                "_extract_root_content",
                side_effect=reload_at_read,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=hop_twice,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=hop_slowly,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=hop_late,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
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
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert capture.section.text == ""
        assert capture.section.error is not None

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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=navigate_away,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert capture.section.text == ""
        assert capture.section.error is not None
        assert "Proxy interstitial" not in str(capture.section.error)

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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                side_effect=add_current_job,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert "Python Developer" in capture.section.text
        assert capture.section.error is None

    async def test_a_redirect_that_beat_the_scroll_is_still_caught(self, mock_page):
        """The baseline is the URL that was asked for, not the one that arrived.

        A redirect completing during the navigation, before any scrolling,
        leaves the landing page as both ends of the comparison, so it reads
        as a page that never moved and its text is returned as the search.
        """
        mock_page.url = "https://www.linkedin.com/feed/"
        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
            )

        assert capture.section.text == ""
        assert capture.section.error is not None

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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=scroll_then_publish,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=python",
                section_name="search_results",
            )

    async def test_the_retry_charges_both_scrolls_to_one_capture(self, mock_page):
        """The rate-limit retry scrolls a second time, and both are charged.

        The charge used to live on the extractor and accumulate across the
        two attempts; the capture has to reproduce that or a throttled page
        bills the search-wide budget for half of what it spent. The retry's
        halved deadline is asserted beside it, because a capture that reset
        between attempts and a retry handed the full cap look the same from
        the total alone.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        # Only LinkedIn chrome the first time, which is the soft rate limit,
        # then real text: one retry and no more. Driven through the content
        # reader rather than `page.evaluate`, which the document-identity
        # read shares.
        reads = [
            {
                "source": "root",
                "text": (
                    "More profiles for you\n\nAbout\nAccessibility\nTalent Solutions"
                ),
                "references": [],
            },
            {"source": "root", "text": "Python Developer", "references": []},
        ]
        deadlines: list[float | None] = []

        async def scroll(page, **kwargs):
            deadlines.append(kwargs.get("deadline"))
            clock.now += 2.0
            return False

        reader = _reader(mock_page)
        with (
            patch.object(job_pages_module, "time", clock),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                job_pages_module.asyncio, "sleep", new_callable=AsyncMock
            ) as backoff,
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=scroll,
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                side_effect=reads,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
                scroll_deadline=8.0,
            )

        assert backoff.await_count == 1
        assert deadlines == [8.0, 4.0]
        assert capture.scroll_seconds == 4.0
        assert capture.section.text == "Python Developer"

    async def test_a_failed_read_still_charges_the_scroll_it_paid_for(self, mock_page):
        """An attempt that raises after scrolling is charged all the same.

        The scroll books its time in a ``finally``, and the caller charges
        its budget from the capture the error path answers with. Dropping
        that lets a page whose extraction failed after a full scroll cost the
        search nothing, and five of them exhaust the wall clock while the
        budget still reads sixty seconds.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"

        async def scroll(page, **kwargs):
            clock.now += 5.0
            return False

        reader = _reader(mock_page)
        with (
            patch.object(job_pages_module, "time", clock),
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                side_effect=scroll,
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                side_effect=RuntimeError("the read blew up"),
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert capture.section.text == ""
        assert capture.section.error is not None
        assert capture.scroll_seconds == 5.0

    async def test_the_search_page_keeps_every_reference_it_found(self, mock_page):
        """No per-page cap here: the workflow reconciles against the rail.

        Capping at the section default would drop rail jobs before
        `reconcile_search_references` ever saw them, and the id would come
        back with a synthesized reference instead of the label the DOM had.
        """
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Job results",
                "references": [
                    {
                        "href": f"https://www.linkedin.com/company/acme-{index}/",
                        "text": f"Acme {index}",
                    }
                    for index in range(20)
                ],
            }
        )

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
            )

        assert len(capture.section.references) == 20


class TestExtractSavedJobsPage:
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch.object(
                reader._content,
                "_extract_root_content",
                side_effect=reload_at_read,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_saved_jobs_page(
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

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_to_bottom",
                side_effect=reload_in_place,
            ),
            patch(
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                side_effect=barrier,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await reader._extract_saved_jobs_page(
                "https://www.linkedin.com/jobs-tracker/",
                section_name="saved_jobs",
            )

    async def test_the_list_is_visited_once_and_scrolled_within_its_ceiling(
        self, mock_page
    ):
        """One navigation, one bounded scroll, and no click anywhere.

        The ceiling is the whole of what bounds this page: the list has no
        deadline handed down the way the search rail does, so five rounds of
        half a second is the only thing between a lazily growing list and an
        unbounded wait. A second navigation would be a second page read into
        the same section.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"

        reader = _reader(mock_page)
        with (
            patch.object(
                PageNavigator, "_navigate_to_page", new_callable=AsyncMock
            ) as navigate_to,
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as scroll,
        ):
            await reader._extract_saved_jobs_page(
                "https://www.linkedin.com/jobs-tracker/",
                section_name="saved_jobs",
            )

        navigate_to.assert_awaited_once_with("https://www.linkedin.com/jobs-tracker/")
        scroll.assert_awaited_once_with(mock_page, pause_time=0.5, max_scrolls=5)
        mock_page.click.assert_not_called()

    async def test_a_saved_page_caps_its_own_references_at_twelve(self, mock_page):
        """The list page carries the section default, unlike the search rail.

        Thirteen candidates against a cap of twelve, so removing the cap
        shows up as a thirteenth reference rather than as an equal list. The
        search page above is the other half of this pair: the two pages take
        deliberately different answers from `build_references`.
        """
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Saved jobs",
                "references": [
                    {
                        "href": f"https://www.linkedin.com/jobs/view/{700 + index}/",
                        "text": f"Saved job {index}",
                    }
                    for index in range(13)
                ],
            }
        )

        reader = _reader(mock_page)
        with (
            patch.object(PageNavigator, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.job_pages.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
        ):
            capture = await reader._extract_saved_jobs_page(
                "https://www.linkedin.com/jobs-tracker/",
                section_name="saved_jobs",
            )

        assert len(capture.section.references) == 12
        assert capture.section.references[-1]["url"] == "/jobs/view/711/"


class TestExtractJobIds:
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
        reader = _reader(mock_page)

        with caplog.at_level("WARNING"):
            assert await reader._extract_job_ids(scoped=True) == ["101", "999"]

        assert "No results rail" in caplog.text
