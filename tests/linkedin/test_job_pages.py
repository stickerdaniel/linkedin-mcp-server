"""Tests for the job-page reader behind the job list workflows."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio
import copy
import re

import pytest
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidAnswerError,
)
from linkedin_mcp_server.linkedin import job_pages as job_pages_module
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.linkedin.job_pages import (
    EasyApplyDraftLeftError,
    JobPageReader,
    _ScrollCharge,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_APPLY_EN_US
from linkedin.support.navigation import navigate


def _reader(page) -> JobPageReader:
    """Wire the page reader the way the facade does."""
    session = PageSession(page)
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
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
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
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
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
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                side_effect=reload_in_place,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ) as scroll,
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                side_effect=scroll_then_reload,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                side_effect=scroll_then_name_a_job,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                reader._content,
                "_extract_root_content",
                side_effect=reload_at_read,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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

    async def test_navigation_and_scroll_answer_to_the_page_deadline(self, mock_page):
        """The deadline reaches `goto`, and the scroll gets what is left of it.

        The scroll deadline is worked out before navigating. A `goto` that
        spends 2.5s of the 4s left, and a `<main>` wait that spends another
        second, would otherwise leave the scroll the whole twelve seconds it
        was handed, on a budget that has already run out. Capped after `goto`
        and not after the wait, the scroll still got 1.5s with 0.5s left.
        """

        class Clock:
            def __init__(self) -> None:
                self.now = 10.0

            def monotonic(self) -> float:
                return self.now

        clock = Clock()
        mock_page.url = "https://www.linkedin.com/jobs/search/?keywords=test"
        navigated: list[float | None] = []
        scrolled: list[float | None] = []

        async def goto(url, *, deadline=None):
            navigated.append(deadline)
            clock.now += 2.5

        async def scroll(page, **kwargs):
            scrolled.append(kwargs.get("deadline"))
            return False

        async def wait_for_main(selector):
            clock.now += 1.0

        mock_page.wait_for_selector = AsyncMock(side_effect=wait_for_main)
        reader = _reader(mock_page)
        with (
            patch.object(job_pages_module, "time", clock),
            patch.object(PageNavigator, "_navigate_to_page", side_effect=goto),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
                side_effect=scroll,
            ),
            patch.object(
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value={
                    "source": "root",
                    "text": "Python Developer",
                    "references": [],
                },
            ),
        ):
            await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
                scroll_deadline=12.0,
                page_deadline=14.0,
            )

        assert navigated == [14.0]
        assert scrolled == [0.5]

    @pytest.mark.parametrize(("left", "attempts"), [(11.0, 1), (12.0, 2)])
    async def test_the_retry_starts_only_when_backoff_and_a_page_fit(
        self, mock_page, left, attempts
    ):
        """A throttled page is retried only when the retry can finish.

        The backoff alone is 5s and a search page 6.5s on average. Retrying
        with less than both left sleeps the budget away and then times out in
        navigation, replacing the rate-limit answer the first attempt already
        had with a timeout that says nothing, or cancels the whole call.
        """

        class Clock:
            now = 0.0

            def monotonic(self) -> float:
                return self.now

        once = AsyncMock(
            side_effect=[
                ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[]),
                ExtractedSection(text="Python Developer", references=[]),
            ]
        )
        reader = _reader(mock_page)
        with (
            patch.object(job_pages_module, "time", Clock()),
            patch.object(reader, "_extract_search_page_once", once),
            patch.object(
                job_pages_module.asyncio, "sleep", new_callable=AsyncMock
            ) as backoff,
        ):
            capture = await reader._extract_search_page(
                "https://www.linkedin.com/jobs/search/?keywords=test",
                section_name="search_results",
                page_deadline=left,
            )

        assert [call.kwargs["page_deadline"] for call in once.await_args_list] == [
            left
        ] * attempts
        assert backoff.await_count == attempts - 1
        assert capture.section.text == (
            RATE_LIMITED_SECTION_TEXT if attempts == 1 else "Python Developer"
        )

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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_job_sidebar",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_to_bottom",
                new_callable=AsyncMock,
            ),
            patch.object(
                reader._content,
                "_extract_root_content",
                side_effect=reload_at_read,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_to_bottom",
                side_effect=reload_in_place,
            ),
            patch(
                "linkedin_mcp_server.linkedin.navigation.detect_auth_barrier",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_to_bottom",
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
                "linkedin_mcp_server.linkedin.job_pages.detect_rate_limit",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.handle_modal_close",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.linkedin.job_pages.scroll_to_bottom",
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


class TestReadEasyApplyForm:
    URL = "https://www.linkedin.com/jobs/view/123/"

    async def test_a_failed_walk_that_leaves_a_draft_raises_its_own_type(
        self, mock_page
    ):
        """The draft is what the caller must act on, so it names the error."""
        reader = _reader(mock_page)
        failure = RuntimeError("The Easy Apply dialog closed while it was read")
        with (
            patch.object(
                reader,
                "_read_apply_type",
                new_callable=AsyncMock,
                return_value=("easy_apply", None),
            ),
            patch.object(
                reader, "_walk_easy_apply", new_callable=AsyncMock, side_effect=failure
            ),
            patch.object(
                reader,
                "_discard_easy_apply",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            with pytest.raises(EasyApplyDraftLeftError) as raised:
                await reader.read_easy_apply_form(self.URL, "123", JOB_APPLY_EN_US)

        assert raised.value.__cause__ is failure

    async def test_a_failed_answering_walk_that_leaves_a_draft_raises_its_type(
        self, mock_page
    ):
        reader = _reader(mock_page)
        failure = RuntimeError("The Easy Apply dialog closed while it was read")
        with (
            patch.object(
                reader,
                "_read_apply_type",
                new_callable=AsyncMock,
                return_value=("easy_apply", None),
            ),
            patch.object(
                reader,
                "_walk_easy_apply_answers",
                new_callable=AsyncMock,
                side_effect=failure,
            ),
            patch.object(
                reader,
                "_discard_easy_apply",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            with pytest.raises(EasyApplyDraftLeftError) as raised:
                await reader.submit_easy_apply(
                    self.URL, "123", JOB_APPLY_EN_US, {}, confirm_submit=False
                )

        assert raised.value.__cause__ is failure


class TestAnswerEasyApplyStep:
    """Filling one step of the Easy Apply dialog from label-keyed answers."""

    CONTACT: dict[str, Any] = {
        "heading": "Contact info",
        "progress": 0,
        "fields": [
            {
                "label": "First name",
                "type": "text",
                "required": True,
                "value": "Ada",
                "ids": ["first"],
            },
            {
                "label": "Email address",
                "type": "select",
                "required": True,
                "options": ["Select an option", "me@example.com"],
                "value": "Select an option",
                "ids": ["email"],
            },
            {
                "label": "Authorized to work in France?",
                "type": "radio",
                "required": True,
                "options": ["Yes", "No"],
                "selected": [],
                "ids": ["yes", "no"],
            },
            {
                "label": "Why Acme?",
                "type": "textarea",
                "required": False,
                "max_length": 20,
                "ids": ["why"],
            },
            {
                "label": "Upload resume",
                "type": "file",
                "required": False,
                "ids": ["cv"],
            },
        ],
    }

    CITY = {
        "label": "Location (city)",
        "type": "typeahead",
        "required": True,
        "ids": ["city"],
    }

    @staticmethod
    def _dialog(mock_page) -> list[tuple[str, str, str]]:
        """Route locators to per-control doubles that record what reached them."""
        done: list[tuple[str, str, str]] = []

        def locator_for(selector: str):
            match = re.search(r'\[id="(.*?)"\]', selector)
            assert match is not None
            control_id = match.group(1)
            control = MagicMock()
            control.first = control
            control.fill = AsyncMock(
                side_effect=lambda value, **_: done.append((control_id, "fill", value))
            )
            control.select_option = AsyncMock(
                side_effect=lambda label, **_: done.append(
                    (control_id, "select", label)
                )
            )
            control.check = AsyncMock(
                side_effect=lambda **_: done.append((control_id, "check", ""))
            )
            control.uncheck = AsyncMock(
                side_effect=lambda **_: done.append((control_id, "uncheck", ""))
            )
            control.set_input_files = AsyncMock(
                side_effect=lambda files, **_: done.append(
                    (control_id, "upload", files)
                )
            )
            return control

        mock_page.locator.side_effect = locator_for
        return done

    async def test_every_control_type_is_set_the_way_it_is_answered(
        self, mock_page, tmp_path
    ):
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.7")
        done = self._dialog(mock_page)

        used, _ = await _reader(mock_page)._answer_easy_apply_step(
            self.CONTACT,
            {
                "First name": "Noor",
                "Email address": "me@example.com",
                "Authorized to work in France?": "Yes",
                "Why Acme?": "Agents.",
                "Upload resume": str(resume),
            },
        )

        assert done == [
            ("first", "fill", "Noor"),
            ("email", "select", "me@example.com"),
            ("yes", "check", ""),
            ("why", "fill", "Agents."),
            ("cv", "upload", str(resume)),
        ]
        assert used == {
            "First name",
            "Email address",
            "Authorized to work in France?",
            "Why Acme?",
            "Upload resume",
        }

    async def test_a_field_no_answer_names_is_left_alone(self, mock_page):
        done = self._dialog(mock_page)

        used, _ = await _reader(mock_page)._answer_easy_apply_step(
            self.CONTACT, {"First name": "Noor"}
        )

        assert done == [("first", "fill", "Noor")]
        assert used == {"First name"}

    async def test_an_answer_the_step_does_not_ask_for_is_left_for_another(
        self, mock_page
    ):
        done = self._dialog(mock_page)

        used, _ = await _reader(mock_page)._answer_easy_apply_step(
            self.CONTACT, {"Years of Python": "5"}
        )

        assert done == []
        assert used == set()

    def test_an_answer_no_step_asked_for_refuses(self):
        with pytest.raises(InvalidAnswerError, match="'Years of Python'"):
            job_pages_module.refuse_unused_easy_apply_answers(
                {"First name": "Noor", "Years of Python": "5"}, {"First name"}
            )

    async def test_two_fields_sharing_a_label_refuse_rather_than_pick_one(
        self, mock_page
    ):
        twice = dict(
            self.CONTACT,
            fields=[
                dict(self.CONTACT["fields"][0]),
                dict(self.CONTACT["fields"][0], ids=["second"]),
            ],
        )
        done = self._dialog(mock_page)

        with pytest.raises(InvalidAnswerError, match="both labelled 'First name'"):
            await _reader(mock_page)._answer_easy_apply_step(twice, {"First name": "S"})
        assert done == []

    async def test_an_empty_typeahead_no_answer_names_still_refuses(self, mock_page):
        step = dict(self.CONTACT, fields=[*self.CONTACT["fields"], self.CITY])
        done = self._dialog(mock_page)

        with pytest.raises(InvalidAnswerError, match="needs an answer naming it"):
            await _reader(mock_page)._answer_easy_apply_step(
                step, {"First name": "Noor"}
            )
        assert done == []

    def test_a_named_typeahead_is_picked_rather_than_typed(self):
        step = dict(self.CONTACT, fields=[self.CITY])

        edits, used = job_pages_module.plan_easy_apply_step(
            step, {"Location (city)": "Paris, \u00cele-de-France, France"}
        )

        assert edits == [
            job_pages_module.EasyApplyEdit(
                "city", "pick", "Paris, \u00cele-de-France, France"
            )
        ]
        assert used == {"Location (city)"}

    def test_a_prefilled_typeahead_no_answer_names_is_kept(self):
        step = dict(self.CONTACT, fields=[dict(self.CITY, value="Paris, France")])

        assert job_pages_module.plan_easy_apply_step(step, {}) == ([], set())

    def test_a_prefilled_typeahead_an_answer_names_is_replaced(self):
        step = dict(self.CONTACT, fields=[dict(self.CITY, value="Paris, France")])

        edits, _ = job_pages_module.plan_easy_apply_step(
            step, {"Location (city)": "Lyon, France"}
        )

        assert edits == [job_pages_module.EasyApplyEdit("city", "pick", "Lyon, France")]

    def test_a_prefill_that_already_is_the_answer_is_left_where_it_is(self):
        # Retyping it would throw away LinkedIn's binding of that text to an
        # entity for the chance of picking the same one back.
        step = dict(
            self.CONTACT,
            fields=[dict(self.CITY, value="Paris, I\u0302le-de-France, France")],
        )

        edits, used = job_pages_module.plan_easy_apply_step(
            step, {"Location (city)": "Paris, \u00cele-de-France, France"}
        )

        assert edits == []
        assert used == {"Location (city)"}

    def test_a_typeahead_takes_one_value_rather_than_a_list(self):
        step = dict(self.CONTACT, fields=[self.CITY])

        with pytest.raises(InvalidAnswerError, match="one value, written out"):
            job_pages_module.plan_easy_apply_step(
                step, {"Location (city)": ["Paris", "Lyon"]}
            )

    def test_picks_are_carried_out_before_every_other_edit(self):
        # A pick only learns whether it matches after its value is typed into
        # the page, so a failing one must find the rest of the step untouched.
        step = dict(self.CONTACT, fields=[*self.CONTACT["fields"], self.CITY])

        edits, _ = job_pages_module.plan_easy_apply_step(
            step,
            {
                "First name": "Noor",
                "Location (city)": "Lyon, France",
                "Why Acme?": "Agents.",
            },
        )

        assert [edit.action for edit in edits] == ["pick", "fill", "fill"]

    def test_a_refusal_names_the_field_and_what_it_takes(self):
        email = self.CONTACT["fields"][1]

        with pytest.raises(InvalidAnswerError) as refusal:
            job_pages_module.plan_easy_apply_answer(email, "me@acme.test")

        message = str(refusal.value)
        assert "No option is labelled 'me@acme.test'" in message
        assert "Email address (select)" in message
        assert "'me@example.com'" in message

    def test_an_option_list_the_read_cut_is_left_for_the_browser_to_match(self):
        # The 250-entry country list the read caps at 50. Refusing on the
        # options that survived would refuse every country past the cut.
        country = {
            "label": "Phone country code",
            "type": "select",
            "required": True,
            "options": ["Select an option", "Afghanistan (+93)"],
            "options_total": 250,
            "ids": ["country"],
        }

        assert job_pages_module.plan_easy_apply_answer(country, "France (+33)") == [
            job_pages_module.EasyApplyEdit("country", "select", "France (+33)")
        ]
        whole = {key: value for key, value in country.items() if key != "options_total"}
        with pytest.raises(InvalidAnswerError, match="No option is labelled"):
            job_pages_module.plan_easy_apply_answer(whole, "France (+33)")

    def test_a_choice_list_the_read_cut_refuses(self):
        field = {
            "label": "Country of residence",
            "type": "radio",
            "required": True,
            "options": ["Afghanistan", "Albania"],
            "options_total": 60,
            "selected": [],
            "ids": [f"r{index}" for index in range(60)],
        }

        with pytest.raises(InvalidAnswerError, match="It lists 60 options"):
            job_pages_module.plan_easy_apply_answer(field, "Albania")

    def test_an_option_no_radio_carries_refuses(self):
        with pytest.raises(InvalidAnswerError, match="No option is labelled 'Maybe'"):
            job_pages_module.plan_easy_apply_answer(self.CONTACT["fields"][2], "Maybe")

    def test_a_checkbox_group_is_set_to_exactly_what_the_answer_names(self):
        field = {
            "label": "Which apply?",
            "type": "checkbox",
            "required": False,
            "options": ["Python", "Go", "Rust"],
            "selected": ["Go", "Rust"],
            "ids": ["py", "go", "rs"],
        }

        edits = job_pages_module.plan_easy_apply_answer(field, ["Python", "Go"])

        assert edits == [
            job_pages_module.EasyApplyEdit("py", "check", "Python"),
            job_pages_module.EasyApplyEdit("rs", "uncheck", "Rust"),
        ]

    def test_a_lone_checkbox_takes_yes_or_no(self):
        field = {
            "label": "I agree",
            "type": "checkbox",
            "required": True,
            "options": ["I agree"],
            "selected": [],
            "ids": ["agree"],
        }

        assert job_pages_module.plan_easy_apply_answer(field, True) == [
            job_pages_module.EasyApplyEdit("agree", "check", "I agree")
        ]
        assert job_pages_module.plan_easy_apply_answer(field, False) == []

    def test_an_answer_longer_than_the_field_holds_refuses(self):
        with pytest.raises(InvalidAnswerError, match="LinkedIn would cut it"):
            job_pages_module.plan_easy_apply_answer(self.CONTACT["fields"][3], "x" * 21)

    def test_a_resume_that_is_not_there_refuses(self, tmp_path):
        missing = str(tmp_path / "nope.pdf")

        with pytest.raises(InvalidAnswerError, match="There is no file at"):
            job_pages_module.plan_easy_apply_answer(self.CONTACT["fields"][4], missing)

    def test_a_control_linkedin_gave_no_id_refuses(self):
        field = dict(self.CONTACT["fields"][0], ids=[""])

        with pytest.raises(InvalidAnswerError, match="no id"):
            job_pages_module.plan_easy_apply_answer(field, "Noor")

    def test_a_control_is_addressed_inside_the_dialog_only(self):
        selector = job_pages_module.easy_apply_control_selector("first")

        assert selector.count('[id="first"]') == 3
        assert all(part.startswith('[role="dialog"]') for part in selector.split(", "))


class _Dialog:
    """A scripted Easy Apply dialog, driven the way the walk drives LinkedIn's.

    Holds the steps it is showing and rewrites them as answers arrive, so that
    a re-read sees what a real dialog would: a checked radio, a filled input,
    and the new controls an upload re-renders the step around. Every click,
    keystroke and value that reaches it is recorded on `log`.
    """

    def __init__(
        self, page, steps: list[dict[str, Any]], *, follow: bool | None = True
    ):
        self.steps = copy.deepcopy(steps)
        self.index = 0
        self.open = False
        self.follow = follow
        self.confirmation = "Your application was sent to Acme"
        #: The posting's own applied line, which is what LinkedIn leaves behind
        #: when it answers a sent application with no dialog at all.
        self.applied_line = ""
        self.refuse: dict[int, dict[str, str]] = {}
        self.stuck: set[int] = set()
        self.cards: list[str] = []
        self.pending: str | None = None
        self.rebuild_in = 0
        #: How many settled reads the torn-down list survives before the file
        #: arrives. 0 rebuilds it before the first read back.
        self.tear_down_reads = 0
        self.follow_resets = 0
        #: How LinkedIn renders the follow checkbox. Measured live, the input
        #: itself takes no click and only its label does.
        self.follow_id = "follow"
        self.follow_shown = True
        self.follow_labelled = True
        self.log: list[Any] = []
        page.evaluate = AsyncMock(side_effect=self._evaluate)
        page.keyboard.press = AsyncMock(side_effect=lambda key: self.log.append(key))
        page.locator = MagicMock(side_effect=self._locator)

    async def _evaluate(self, script, *args, **kwargs):
        if script is job_pages_module.EASY_APPLY_STEP_JS:
            if not self.open:
                return None
            if self.pending is not None:
                if self.rebuild_in <= 0:
                    self.cards.append(self.pending)
                    self.pending = None
                else:
                    self.rebuild_in -= 1
            step = copy.deepcopy(self.steps[self.index])
            step["fields"] += [
                {
                    "label": f"Deselect resume {name}",
                    "type": "radio",
                    "required": False,
                    "options": [f"Deselect resume {name}"],
                    "selected": [f"Deselect resume {name}"],
                    "ids": [f"card-{name}"],
                }
                for name in self.cards
            ]
            step["signature"] = repr(
                [step["heading"], step["progress"]]
                + [i for field in step["fields"] for i in field.get("ids", [])]
            )
            return step
        if script is job_pages_module.EASY_APPLY_FOLLOW_JS:
            if self.follow is None:
                return None
            return {
                "id": self.follow_id,
                "checked": self.follow,
                "shown": self.follow_shown,
                "labelled": bool(self.follow_id) and self.follow_labelled,
            }
        if script is job_pages_module.EASY_APPLY_CONFIRMATION_JS:
            return self.confirmation
        if script is job_pages_module.APPLIED_LINE_JS:
            return self.applied_line
        return self.open

    def _locator(self, selector: str):
        control = MagicMock()
        control.first = control
        if "/apply" in selector:
            # The anchor-shaped Easy Apply, the only one the walk opens.
            control.count = AsyncMock(return_value=1)
            control.click = AsyncMock(side_effect=self._open_dialog)
        elif selector == job_pages_module.EASY_APPLY_NEXT_SELECTOR:
            control.click = AsyncMock(side_effect=self._next)
        elif selector == job_pages_module.EASY_APPLY_SUBMIT_SELECTOR:
            control.click = AsyncMock(side_effect=self._submit)
        elif selector == job_pages_module.EASY_APPLY_DISCARD_SELECTOR:
            control.click = AsyncMock(side_effect=self._discard)
        elif "footer" in selector:
            control.check = AsyncMock(
                side_effect=lambda **_: self._set("follow", "check", "")
            )
            control.uncheck = AsyncMock(
                side_effect=lambda **_: self._set("follow", "uncheck", "")
            )
        elif selector.startswith(
            '[role="dialog"]:has([data-easy-apply-next-button]) label'
        ):
            label = re.search(r'label\[for="(.*?)"\]', selector)
            assert label is not None
            for_id = label.group(1)
            control.click = AsyncMock(
                side_effect=lambda **_: self._set(for_id, "label-click", "")
            )
        else:
            match = re.search(r'\[id="(.*?)"\]', selector)
            assert match is not None
            control_id = match.group(1)
            control.fill = AsyncMock(
                side_effect=lambda value, **_: self._set(control_id, "fill", value)
            )
            control.select_option = AsyncMock(
                side_effect=lambda label, **_: self._set(control_id, "select", label)
            )
            control.check = AsyncMock(
                side_effect=lambda **_: self._set(control_id, "check", "")
            )
            control.uncheck = AsyncMock(
                side_effect=lambda **_: self._set(control_id, "uncheck", "")
            )
            control.set_input_files = AsyncMock(
                side_effect=lambda files, **_: self._set(control_id, "upload", files)
            )
        return control

    def _open_dialog(self, **_):
        self.open = True
        self.index = 0
        self.log.append("open")

    def _next(self, **_):
        self.log.append("next")
        if self.index in self.stuck:
            # Standing still and re-rendering: the step's controls change and
            # the step does not, which is the shape a signature reads as
            # progress.
            for field in self.steps[self.index]["fields"]:
                field["ids"] = [f"{i}-again" for i in field.get("ids", [])]
            return
        refused = self.refuse.get(self.index)
        if refused:
            for field in self.steps[self.index]["fields"]:
                if field["label"] in refused:
                    field["error"] = refused[field["label"]]
            return
        self.index += 1

    def _submit(self, **_):
        self.log.append("submit")
        self.open = False

    def _discard(self, **_):
        self.log.append("discard")
        self.open = False

    def _set(self, control_id: str, action: str, value: str):
        self.log.append((control_id, action, value))
        if control_id == "follow":
            if action == "label-click":
                self.follow = not self.follow
            elif not self.follow_shown:
                raise AssertionError("the follow input takes no click")
            else:
                self.follow = action == "check"
            if self.follow_resets:
                # A re-render restoring LinkedIn's default under the answer.
                self.follow_resets -= 1
                self.follow = True
            return
        for field in self.steps[self.index]["fields"]:
            ids = field.get("ids") or []
            if control_id not in ids:
                continue
            if action in ("fill", "select"):
                field["value"] = value
            elif action == "upload":
                # What the walk has to survive, measured live: LinkedIn tears
                # the resume list down when the upload starts and rebuilds it
                # around the new file only once that file has arrived. The
                # torn-down list holds still in between, so reads agree on a
                # step with no resume on it at all.
                field["ids"] = [f"{control_id}-again"]
                self.cards.clear()
                self.rebuild_in = self.tear_down_reads
                self.pending = value.rsplit("/", 1)[-1]
            else:
                option = field["options"][ids.index(control_id)]
                selected = set(field.get("selected") or [])
                field["selected"] = sorted(
                    selected | {option} if action == "check" else selected - {option}
                )


class TestSubmitEasyApply:
    """Walking the dialog from Apply to a submitted application."""

    URL = "https://www.linkedin.com/jobs/view/123/"

    QUESTIONS: list[dict[str, Any]] = [
        {
            "heading": "Contact info",
            "progress": 0,
            "next": True,
            "fields": [
                {
                    "label": "First name",
                    "type": "text",
                    "required": True,
                    "value": "Ada",
                    "ids": ["first"],
                }
            ],
        },
        {
            "heading": "Additional questions",
            "progress": 50,
            "next": True,
            "fields": [
                {
                    "label": "Authorized to work in France?",
                    "type": "radio",
                    "required": True,
                    "options": ["Yes", "No"],
                    "selected": [],
                    "ids": ["yes", "no"],
                }
            ],
        },
        {
            "heading": "Resume",
            "progress": 100,
            "next": False,
            "fields": [
                {
                    "label": "Upload resume",
                    "type": "file",
                    "required": False,
                    "ids": ["cv"],
                }
            ],
        },
    ]

    ANSWERS: dict[str, Any] = {"Authorized to work in France?": "Yes"}

    @pytest.fixture(autouse=True)
    def _no_polling(self, monkeypatch):
        monkeypatch.setattr(job_pages_module, "_APPLY_POLL", 0)

    def _run(self, mock_page, steps=None, **kwargs):
        """Walk ``steps`` with the apply-type read stubbed, as the facade would."""
        dialog = _Dialog(mock_page, steps if steps is not None else self.QUESTIONS)
        reader = _reader(mock_page)
        answers = kwargs.pop("answers", self.ANSWERS)
        run = patch.object(
            reader, "_read_apply_type", new=AsyncMock(return_value=("easy_apply", None))
        )
        return dialog, run, reader, answers, kwargs

    async def test_without_the_flag_the_form_is_filled_and_the_draft_discarded(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=False
            )

        assert result.submitted is False
        assert result.complete is True
        assert result.discarded is True
        assert "submit" not in dialog.log
        assert ("yes", "check", "") in dialog.log
        assert dialog.log[-2:] == ["Escape", "discard"]
        assert [step["heading"] for step in result.steps] == [
            "Contact info",
            "Additional questions",
            "Resume",
        ]
        assert result.steps[1]["fields"][0]["selected"] == ["Yes"]

    async def test_the_flag_is_the_only_thing_that_clicks_submit(self, mock_page):
        dialog, run, reader, answers, _ = self._run(mock_page)

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.submitted is True
        assert result.complete is True
        assert result.confirmation == "Your application was sent to Acme"
        assert "submit" in dialog.log and "discard" not in dialog.log
        assert result.discarded is True

    async def test_the_postings_applied_line_answers_a_submit_no_dialog_follows(
        self, mock_page
    ):
        """A sent application LinkedIn narrates with nothing at all.

        Measured on 2026-09-23: the form closed, no modal rendered, and the
        posting behind it carried the applied line. The dialog closing already
        proved the click landed, so what is being read here is the account of
        it, and the posting is the one surface still giving one.
        """
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.confirmation = ""
        dialog.applied_line = "Application submitted"

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.submitted is True
        assert result.confirmation == "Application submitted"

    async def test_linkedins_own_dialog_is_taken_over_the_postings_line(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.applied_line = "Application submitted"

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.confirmation == "Your application was sent to Acme"

    async def test_neither_surface_speaking_is_empty_and_not_a_failed_submit(
        self, mock_page, monkeypatch
    ):
        monkeypatch.setattr(job_pages_module, "_EASY_APPLY_CONFIRMATION_TIMEOUT", 0)
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.confirmation = ""

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.submitted is True
        assert result.confirmation == ""
        assert "submit" in dialog.log

    async def test_a_required_field_nothing_answers_stops_before_any_click(
        self, mock_page
    ):
        dialog, run, reader, _, _ = self._run(mock_page)

        with run:
            with pytest.raises(InvalidAnswerError, match="Authorized to work"):
                await reader.submit_easy_apply(
                    self.URL, "123", JOB_APPLY_EN_US, {}, confirm_submit=True
                )

        assert dialog.log.count("next") == 1  # onto the step, never off it
        assert "submit" not in dialog.log
        assert "discard" in dialog.log

    async def test_a_step_linkedin_refuses_ends_the_run_carrying_its_errors(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.refuse[1] = {"Authorized to work in France?": "Select an answer"}

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.submitted is False
        assert result.complete is False
        assert result.steps[-1]["fields"][0]["error"] == "Select an answer"
        assert "submit" not in dialog.log
        assert result.discarded is True

    async def test_the_step_an_upload_re_renders_is_not_read_as_a_refusal(
        self, mock_page, tmp_path
    ):
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.7")
        steps = copy.deepcopy(self.QUESTIONS)
        steps[0]["fields"].append(
            {"label": "Upload resume", "type": "file", "required": True, "ids": ["cv"]}
        )
        dialog, run, reader, _, _ = self._run(mock_page, steps)

        with run:
            result = await reader.submit_easy_apply(
                self.URL,
                "123",
                JOB_APPLY_EN_US,
                {**self.ANSWERS, "Upload resume": str(resume)},
                confirm_submit=True,
            )

        assert ("cv", "upload", str(resume)) in dialog.log
        assert result.complete is True
        assert result.submitted is True
        # Read back after the upload, not before it: the card LinkedIn renders
        # for the new file only exists in the step as it stands afterwards.
        assert [field["label"] for field in result.steps[0]["fields"]] == [
            "First name",
            "Upload resume",
            "Deselect resume resume.pdf",
        ]

    async def test_a_resume_list_still_rebuilding_is_waited_out(
        self, mock_page, tmp_path
    ):
        # The live failure: the upload empties the list, the empty list holds
        # still, and two reads agreeing on it is not the file having arrived.
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.7")
        steps = copy.deepcopy(self.QUESTIONS)
        steps[0]["fields"].append(
            {"label": "Upload resume", "type": "file", "required": False, "ids": ["cv"]}
        )
        dialog, run, reader, _, _ = self._run(mock_page, steps)
        dialog.tear_down_reads = 4

        with run:
            result = await reader.submit_easy_apply(
                self.URL,
                "123",
                JOB_APPLY_EN_US,
                {**self.ANSWERS, "Upload resume": str(resume)},
                confirm_submit=True,
            )

        assert result.submitted is True
        assert "Deselect resume resume.pdf" in [
            field["label"] for field in result.steps[0]["fields"]
        ]

    async def test_a_file_linkedin_never_shows_back_is_not_submitted(
        self, mock_page, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(job_pages_module, "_EASY_APPLY_UPLOAD_TIMEOUT", 0)
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.7")
        steps = copy.deepcopy(self.QUESTIONS)
        steps[0]["fields"].append(
            {"label": "Upload resume", "type": "file", "required": False, "ids": ["cv"]}
        )
        dialog, run, reader, _, _ = self._run(mock_page, steps)
        dialog.tear_down_reads = 99

        with run:
            with pytest.raises(InvalidAnswerError, match="did not show 'resume.pdf'"):
                await reader.submit_easy_apply(
                    self.URL,
                    "123",
                    JOB_APPLY_EN_US,
                    {**self.ANSWERS, "Upload resume": str(resume)},
                    confirm_submit=True,
                )

        assert "submit" not in dialog.log
        assert "discard" in dialog.log

    async def test_a_follow_checkbox_a_re_render_resets_is_set_again(self, mock_page):
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.follow_resets = 1

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        # The box itself first, then the label, which is what a person clicks.
        assert [entry for entry in dialog.log if entry[0] == "follow"] == [
            ("follow", "uncheck", ""),
            ("follow", "label-click", ""),
        ]
        assert result.follow is False

    async def test_a_follow_checkbox_with_nothing_to_click_is_set_by_its_label(
        self, mock_page
    ):
        # How LinkedIn renders it, measured live: the input takes no click and
        # only the label it is styled with does.
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.follow_shown = False

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert [entry for entry in dialog.log if entry[0] == "follow"] == [
            ("follow", "label-click", "")
        ]
        assert result.follow is False

    async def test_a_follow_checkbox_linkedin_left_anonymous_is_still_reached(
        self, mock_page
    ):
        # No id, so no way to key a selector on one and no label to point at
        # it. The footer's own checkbox is what is left.
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.follow_id = ""

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert ("follow", "uncheck", "") in dialog.log
        assert result.follow is False

    async def test_a_follow_checkbox_that_takes_nothing_is_reported_as_it_is(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.follow_shown = False
        dialog.follow_labelled = False

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        # Never worth failing an application over, and never claimed either.
        assert result.follow is True
        assert result.submitted is True

    async def test_a_step_that_neither_moves_nor_complains_ends_the_run(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)
        dialog.stuck.add(1)

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.complete is False
        assert result.submitted is False
        assert len(result.steps) == 2
        assert "submit" not in dialog.log
        assert "discard" in dialog.log

    async def test_the_follow_checkbox_ends_unchecked_unless_it_is_asked_for(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert ("follow", "uncheck", "") in dialog.log
        assert result.follow is False

    async def test_the_follow_checkbox_is_left_alone_when_it_is_asked_for(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)

        with run:
            result = await reader.submit_easy_apply(
                self.URL,
                "123",
                JOB_APPLY_EN_US,
                answers,
                confirm_submit=True,
                follow_company=True,
            )

        assert not [entry for entry in dialog.log if entry[0] == "follow"]
        assert result.follow is True

    async def test_an_answer_no_step_asked_for_refuses_before_anything_is_sent(
        self, mock_page
    ):
        dialog, run, reader, _, _ = self._run(mock_page)

        with run:
            with pytest.raises(InvalidAnswerError, match="Years of Rust"):
                await reader.submit_easy_apply(
                    self.URL,
                    "123",
                    JOB_APPLY_EN_US,
                    {**self.ANSWERS, "Years of Rust": "5"},
                    confirm_submit=True,
                )

        assert "submit" not in dialog.log
        assert "discard" in dialog.log

    async def test_a_posting_that_is_not_easy_apply_answers_with_its_type_alone(
        self, mock_page
    ):
        dialog, _, reader, answers, _ = self._run(mock_page)

        with patch.object(
            reader, "_read_apply_type", new=AsyncMock(return_value=("applied", None))
        ):
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result == job_pages_module.EasyApplySubmitRead("applied")
        assert dialog.log == []

    async def test_a_dialog_still_open_after_submit_is_not_reported_as_sent(
        self, mock_page
    ):
        dialog, run, reader, answers, _ = self._run(mock_page)

        def stall(script, **kwargs):
            if script == job_pages_module.EASY_APPLY_CLOSED_JS:
                raise PlaywrightTimeoutError("still open")

        mock_page.wait_for_function = AsyncMock(side_effect=stall)

        with run:
            with pytest.raises(RuntimeError, match="probably not sent"):
                await reader.submit_easy_apply(
                    self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
                )

        assert "submit" in dialog.log

    async def test_a_form_longer_than_the_cap_stops_short_of_submitting(
        self, mock_page, monkeypatch
    ):
        monkeypatch.setattr(job_pages_module, "_EASY_APPLY_MAX_STEPS", 2)
        dialog, run, reader, answers, _ = self._run(mock_page)

        with run:
            result = await reader.submit_easy_apply(
                self.URL, "123", JOB_APPLY_EN_US, answers, confirm_submit=True
            )

        assert result.complete is False
        assert result.submitted is False
        assert len(result.steps) == 2
        assert "submit" not in dialog.log
