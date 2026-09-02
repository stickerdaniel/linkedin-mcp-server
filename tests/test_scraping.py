"""Tests for the LinkedInExtractor scraping engine."""

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import asyncio

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import pytest

from linkedin_mcp_server.callbacks import ProgressCallback
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    InvalidReferenceError,
    LinkedInScraperException,
    ProxyConnectionError,
)
from linkedin_mcp_server.scraping.connection import (
    ActionSignals,
    detect_connection_state,
)
from linkedin_mcp_server.scraping import extractor as extractor_module
from linkedin_mcp_server.scraping.extractor import (
    ExtractedSection,
    LinkedInExtractor,
    _CONTENT_DATE_POSTED_MAP,
    _MESSAGE_OCCURRENCES_INCREASED_JS,
    _MESSAGE_OCCURRENCES_JS,
    _RATE_LIMITED_MSG,
    _build_feed_references,
    _truncate_linkedin_noise,
    strip_conversation_chrome,
    strip_linkedin_noise,
)
from linkedin_mcp_server.scraping.link_metadata import Reference


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


class TestBuildJobSearchUrl:
    """Tests for _build_job_search_url URL construction."""

    def test_keywords_only(self):
        url = LinkedInExtractor._build_job_search_url("python developer")
        assert url == "https://www.linkedin.com/jobs/search/?keywords=python+developer"

    def test_with_location(self):
        url = LinkedInExtractor._build_job_search_url("python", location="Remote")
        assert "keywords=python" in url
        assert "location=Remote" in url

    def test_date_posted_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", date_posted="past_week")
        assert "f_TPR=r604800" in url

    def test_date_posted_passthrough(self):
        url = LinkedInExtractor._build_job_search_url("python", date_posted="r3600")
        assert "f_TPR=r3600" in url

    def test_experience_level_normalization(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", experience_level="entry"
        )
        assert "f_E=2" in url

    def test_experience_level_csv(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", experience_level="entry,director"
        )
        assert "f_E=2,5" in url

    def test_work_type_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", work_type="remote")
        assert "f_WT=2" in url

    def test_work_type_csv(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", work_type="on_site,hybrid"
        )
        assert "f_WT=1,3" in url

    def test_easy_apply(self):
        url = LinkedInExtractor._build_job_search_url("python", easy_apply=True)
        assert "f_EA=true" in url

    def test_easy_apply_false_omitted(self):
        url = LinkedInExtractor._build_job_search_url("python", easy_apply=False)
        assert "f_EA" not in url

    def test_sort_by_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", sort_by="date")
        assert "sortBy=DD" in url

    def test_job_type_normalization(self):
        url = LinkedInExtractor._build_job_search_url("python", job_type="full_time")
        assert "f_JT=F" in url

    def test_job_type_csv(self):
        url = LinkedInExtractor._build_job_search_url(
            "python", job_type="full_time,contract"
        )
        assert "f_JT=F,C" in url

    def test_job_type_passthrough(self):
        url = LinkedInExtractor._build_job_search_url("python", job_type="F")
        assert "f_JT=F" in url

    def test_all_filters_combined(self):
        url = LinkedInExtractor._build_job_search_url(
            "python",
            location="Berlin",
            date_posted="past_week",
            experience_level="entry,mid_senior",
            work_type="remote",
            easy_apply=True,
            sort_by="date",
        )
        assert "keywords=python" in url
        assert "location=Berlin" in url
        assert "f_TPR=r604800" in url
        assert "f_E=2,4" in url
        assert "f_WT=2" in url
        assert "f_EA=true" in url
        assert "sortBy=DD" in url


@pytest.fixture
def mock_page():
    """Create a mock Patchright page."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="LinkedIn")
    page.wait_for_selector = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.url = "https://www.linkedin.com/in/testuser/"
    page.locator = MagicMock()
    # Default: no modals, no CAPTCHA
    mock_locator = MagicMock()
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.is_visible = AsyncMock(return_value=False)
    mock_locator.first = mock_locator
    mock_locator.inner_text = AsyncMock(return_value="normal page content")
    mock_locator.filter = MagicMock(return_value=mock_locator)
    page.locator.return_value = mock_locator
    # A real `Frame` carries the address it navigated to, and production
    # reads it off the `framenavigated` argument rather than off the page.
    # A bare object answers every attribute with nothing, which left hop
    # recording looking correct here however broken it was.
    page.main_frame = SimpleNamespace(url=page.url)
    page.wait_for_load_state = AsyncMock()
    # Real listeners, so that a double can navigate the way the browser does.
    # A reload leaves `page.url` untouched, so the event is the only thing that
    # says the document was replaced, and a double that only assigns the URL
    # cannot express one.
    listeners: dict[str, list] = {}
    page.on = MagicMock(
        side_effect=lambda event, cb: listeners.setdefault(event, []).append(cb)
    )
    page.remove_listener = MagicMock(
        side_effect=lambda event, cb: (
            listeners.get(event, []).remove(cb)
            if cb in listeners.get(event, [])
            else None
        )
    )
    page.listeners = listeners
    # The document's own identity, which `performance.timeOrigin` reports and
    # `navigate()` moves. A double answering every script with one object
    # claims a document that is never replaced, and the code under test reads
    # that as a page rewriting its own address.
    page.time_origin = 1_000.0
    page.evaluate = with_document_identity(
        page,
        AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample page text",
                "references": [],
            }
        ),
    )
    return page


def with_document_identity(page, evaluate):
    """Answer the document-identity read from `page.time_origin`.

    Every other script keeps going to `evaluate`. A test that replaces
    `page.evaluate` outright loses this and reads no identity at all, which
    leaves the production code where it was before there was one: it settles
    on the event alone.
    """

    async def dispatch(script, *args, **kwargs):
        if "timeOrigin" in script:
            return page.time_origin
        return await evaluate(script, *args, **kwargs)

    return AsyncMock(side_effect=dispatch)


def navigate(page, url: str | None = None, same_document: bool = False) -> None:
    """Move a mock page the way a navigation does: the address and the event.

    `url` is omitted for a reload, which replaces the document and leaves the
    address exactly as it was.

    `same_document` is a `pushState`, a `replaceState` or a hash change. Each
    raises `framenavigated` on the main frame exactly as a replacement does
    (measured), and LinkedIn appends `currentJobId` to a search URL that way
    on every healthy page. What separates them is the document surviving.
    """
    if url is not None:
        page.url = url
    if not same_document:
        page.time_origin += 1.0
    # The frame carries the address too, and production reads the hop off the
    # frame rather than off the page. Leaving it behind is what let the hop
    # recording look correct here however broken it was.
    page.main_frame.url = page.url
    for callback in list(page.listeners.get("framenavigated", [])):
        callback(page.main_frame)


class TestExtractPage:
    async def test_extract_page_returns_text(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample profile text",
                "references": [],
            }
        )
        extractor = LinkedInExtractor(mock_page)
        # Patch scroll_to_bottom and detect_rate_limit to avoid complex mock chains
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

        assert result.text == "Sample profile text"
        assert result.references == []
        mock_page.goto.assert_awaited_once()

    async def test_root_content_filters_empty_href_before_resolution(self, mock_page):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Sample profile text",
                "references": [],
            }
        )
        extractor = LinkedInExtractor(mock_page)

        await extractor._extract_root_content(["main"])

        await_args = mock_page.evaluate.await_args
        assert await_args is not None
        script = await_args.args[0]
        assert "MAX_HEADING_CONTAINERS = 300" in script
        assert "MAX_REFERENCE_ANCHORS = 500" in script
        assert "const getPreviousHeading = node =>" in script
        assert "index < 3" in script
        assert "if (!rawHref || rawHref === '#')" in script
        assert ".slice(0, MAX_REFERENCE_ANCHORS)" in script
        assert "in_list" not in script
        assert ".filter(Boolean);" in script

    async def test_extract_page_returns_empty_on_failure(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        extractor = LinkedInExtractor(mock_page)

        with patch(
            "linkedin_mcp_server.scraping.extractor.build_issue_diagnostics",
            return_value={"issue_template_path": "/tmp/issue.md"},
        ):
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/bad/",
                section_name="main_profile",
            )
        assert result.text == ""
        assert result.references == []
        assert result.error == {"issue_template_path": "/tmp/issue.md"}

    async def test_extract_page_raises_auth_error_for_account_picker(self, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))
        extractor = LinkedInExtractor(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="auth barrier text: welcome back + sign in using another account",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

    async def test_rate_limit_detected(self, mock_page):
        from linkedin_mcp_server.core.exceptions import RateLimitError

        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_rate_limit",
                new_callable=AsyncMock,
                side_effect=RateLimitError("Rate limited", suggested_wait_time=3600),
            ),
            pytest.raises(RateLimitError),
        ):
            await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/",
                section_name="main_profile",
            )

    async def test_returns_rate_limited_msg_after_retry(self, mock_page):
        """When both attempts return only noise, surface rate limit message."""
        noise_only = (
            "More profiles for you\n\n"
            "You've approached your profile search limit\n\n"
            "About\nAccessibility\nTalent Solutions"
        )
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": noise_only, "references": []}
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/details/experience/",
                section_name="experience",
            )

        assert result.text == _RATE_LIMITED_MSG
        # goto called twice (initial + retry)
        assert mock_page.goto.await_count == 2

    async def test_retry_succeeds_after_rate_limit(self, mock_page):
        """When first attempt is rate-limited but retry succeeds, return content."""
        noise_only = "More profiles for you\n\nAbout\nAccessibility\nTalent Solutions"
        call_count = 0

        async def evaluate_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return noise_only
            return "Education\nHarvard University\n1973 – 1975"

        async def root_content_side_effect(*args, **kwargs):
            return {
                "source": "root",
                "text": await evaluate_side_effect(),
                "references": [],
            }

        mock_page.evaluate = AsyncMock(side_effect=root_content_side_effect)
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor.extract_page(
                "https://www.linkedin.com/in/testuser/details/education/",
                section_name="education",
            )

        assert result.text == "Education\nHarvard University\n1973 – 1975"

    async def test_media_only_controls_are_not_misclassified_as_rate_limited(
        self, mock_page
    ):
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Play\nLoaded: 100.00%\nRemaining time 0:07\nShow captions",
                "references": [],
            }
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/in/testuser/recent-activity/all/",
                section_name="posts",
            )

        assert result.text == ""
        assert result.references == []

    async def test_extract_search_page_raises_auth_error_for_login_barrier(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
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
                extractor,
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
                extractor,
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
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=redirect_to_the_redesign,
            ),
            patch.object(
                extractor,
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                extractor, "_extract_root_content", side_effect=reload_at_read
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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


class TestNavigationDiagnostics:
    async def test_goto_with_auth_checks_clicks_remember_me_and_retries(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)

        async def goto_side_effect(*args, **kwargs):
            if mock_page.goto.await_count == 1:
                raise Exception("net::ERR_TOO_MANY_REDIRECTS")
            return None

        mock_page.goto = AsyncMock(side_effect=goto_side_effect)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                side_effect=[True],
            ) as mock_resolve,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier_quick",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert mock_page.goto.await_count == 2
        mock_resolve.assert_awaited_once()

    async def test_goto_with_auth_checks_unhooks_outer_listener_before_retry(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        listener_events: list[str] = []

        def record_on(event_name, callback):
            listener_events.append(f"on:{event_name}")

        def record_remove(event_name, callback):
            listener_events.append(f"off:{event_name}")

        mock_page.on.side_effect = record_on
        mock_page.remove_listener.side_effect = record_remove

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier_quick",
                new_callable=AsyncMock,
                side_effect=["account picker", None],
            ),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert listener_events == [
            "on:framenavigated",
            "off:framenavigated",
            "on:framenavigated",
            "off:framenavigated",
        ]

    async def test_goto_with_auth_checks_records_original_failure_before_retry(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(
            side_effect=[
                Exception("net::ERR_TOO_MANY_REDIRECTS"),
                Exception("retry failed"),
            ]
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                side_effect=[True, False],
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.record_page_trace",
                new_callable=AsyncMock,
            ) as mock_trace,
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(Exception, match="retry failed"),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        trace_steps = [call.args[1] for call in mock_trace.await_args_list]
        assert "extractor-navigation-error-before-remember-me-retry" in trace_steps

        trace_call = next(
            call
            for call in mock_trace.await_args_list
            if call.args[1] == "extractor-navigation-error-before-remember-me-retry"
        )
        assert (
            trace_call.kwargs["extra"]["error"]
            == "Exception: net::ERR_TOO_MANY_REDIRECTS"
        )

    async def test_a_hop_on_the_way_reaches_the_failure_log(self, mock_page):
        """Where a failed navigation went is the diagnostic it leaves behind.

        The address is read off the frame the event carries and not off the
        page, so a double whose frame never moves records nothing while
        looking exactly like one that works.
        """
        extractor = LinkedInExtractor(mock_page)
        checkpoint = "https://www.linkedin.com/checkpoint/challenge/"

        async def goto_then_fail(*args, **kwargs):
            navigate(mock_page, checkpoint)
            raise Exception("net::ERR_ABORTED")

        mock_page.goto = AsyncMock(side_effect=goto_then_fail)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                extractor,
                "_log_navigation_failure",
                new_callable=AsyncMock,
            ) as mock_log_failure,
            pytest.raises(Exception, match="ERR_ABORTED"),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        logged = mock_log_failure.await_args
        assert logged is not None
        assert logged.args[3] == [checkpoint]

    async def test_goto_with_auth_checks_logs_failure_context(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_TOO_MANY_REDIRECTS"))

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                extractor,
                "_log_navigation_failure",
                new_callable=AsyncMock,
            ) as mock_log_failure,
            pytest.raises(Exception, match="ERR_TOO_MANY_REDIRECTS"),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        mock_log_failure.assert_awaited_once()
        mock_page.on.assert_called_once()
        mock_page.remove_listener.assert_called_once()


class TestScrapePersonUrls:
    """Test that scrape_person visits the correct URLs per section set."""

    async def test_baseline_always_included(self, mock_page):
        """Passing only experience still visits main profile."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"experience"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert "main_profile" in result["sections"]
        assert any(u.endswith("/in/testuser/") for u in urls)
        assert any("/details/experience/" in u for u in urls)

    async def test_basic_info_only_visits_main_profile(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert urls[0].endswith("/in/testuser/")
        assert set(result["sections"]) == {"main_profile"}

    async def test_a_pasted_profile_link_reaches_the_canonical_profile_url(
        self, mock_page
    ):
        """A URL argument must be reduced before it becomes a path segment.

        Without this the navigation target is
        https://www.linkedin.com/in/https://de.linkedin.com/in/testuser, which
        LinkedIn does not serve, and the tool reports that page as a profile.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person(
                "https://de.linkedin.com/in/testuser", {"main_profile"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert urls == ["https://www.linkedin.com/in/testuser/"]
        assert result["url"] == "https://www.linkedin.com/in/testuser/"

    async def test_a_dot_segment_value_never_reaches_a_navigation(self, mock_page):
        # A browser resolves ../ away before the request, so this would open the
        # feed and return it as a profile.
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor, "extract_page", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(LinkedInScraperException):
                await extractor.scrape_person("testuser/../../feed", {"main_profile"})
        mock_extract.assert_not_called()

    async def test_an_already_encoded_username_is_not_encoded_twice(self, mock_page):
        """get_my_profile hands over the username exactly this way.

        It reads the segment out of page.url after the /in/me/ redirect, and a
        browser reports that path percent-encoded. Escaping it again turns %D0
        into %25D0, which is a different profile path, so the own-profile scrape
        of any member with a non-ASCII vanity would navigate somewhere else.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.scrape_person(
                "%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9", {"main_profile"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert urls == [
            "https://www.linkedin.com/in/%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9/"
        ]

    async def test_a_pasted_company_link_reaches_the_canonical_company_url(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("company text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company(
                "https://de.linkedin.com/company/testco/posts/", {"about"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert urls
        assert all(
            u.startswith("https://www.linkedin.com/company/testco") for u in urls
        )
        assert result["url"] == "https://www.linkedin.com/company/testco/"

    async def test_scrape_person_returns_section_errors(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted("profile text"),
                    extracted("", error={"issue_template_path": "/tmp/issue.md"}),
                ],
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert result["sections"]["main_profile"] == "profile text"
        assert (
            result["section_errors"]["posts"]["issue_template_path"] == "/tmp/issue.md"
        )

    async def test_experience_education_visits_correct_urls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person(
                "testuser", {"main_profile", "experience", "education"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 3
        assert any(u.endswith("/in/testuser/") for u in urls)
        assert any("/details/experience/" in u for u in urls)
        assert any("/details/education/" in u for u in urls)
        assert set(result["sections"]) == {"main_profile", "experience", "education"}

    async def test_all_sections_visit_all_urls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        all_sections = {
            "main_profile",
            "experience",
            "education",
            "interests",
            "honors",
            "languages",
            "certifications",
            "skills",
            "projects",
            "contact_info",
            "posts",
        }
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted("contact text"),
            ) as mock_overlay,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", all_sections)

        page_urls = [call.args[0] for call in mock_extract.call_args_list]
        overlay_urls = [call.args[0] for call in mock_overlay.call_args_list]
        all_urls = page_urls + overlay_urls
        # 10 full-page sections + 1 overlay (contact_info)
        assert len(page_urls) == 10
        assert len(overlay_urls) == 1
        # Verify each expected suffix was navigated
        assert any(u.endswith("/in/testuser/") for u in all_urls)
        assert any("/details/experience/" in u for u in all_urls)
        assert any("/details/education/" in u for u in all_urls)
        assert any("/details/interests/" in u for u in all_urls)
        assert any("/details/honors/" in u for u in all_urls)
        assert any("/details/languages/" in u for u in all_urls)
        assert any("/details/certifications/" in u for u in all_urls)
        assert any("/details/skills/" in u for u in all_urls)
        assert any("/details/projects/" in u for u in all_urls)
        assert any("/overlay/contact-info/" in u for u in overlay_urls)
        assert any("/recent-activity/all/" in u for u in all_urls)
        assert set(result["sections"]) == all_sections

    async def test_posts_visits_recent_activity(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Post 1\nPost 2"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("test-user", {"posts"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/recent-activity/all/" in url for url in urls)
        assert "posts" in result["sections"]

    async def test_certifications_visits_details_page(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Python for Data Science\nIBM"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("test-user", {"certifications"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/details/certifications/" in url for url in urls)
        assert "certifications" in result["sections"]

    async def test_skills_visits_details_page(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Python\nData Analysis"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("test-user", {"skills"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/details/skills/" in url for url in urls)
        assert "skills" in result["sections"]

    async def test_projects_visits_details_page(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("Portfolio Website\nBuilt with React"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("test-user", {"projects"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/details/projects/" in url for url in urls)
        assert "projects" in result["sections"]

    async def test_scrape_person_passes_max_scrolls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.scrape_person(
                "test-user", {"certifications"}, max_scrolls=15
            )

        for call in mock_extract.call_args_list:
            assert call.kwargs.get("max_scrolls") == 15


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


class TestConnectWithPerson:
    def _mock_scrape(
        self, profile_text: str, *, follow_up_text: str | None = None
    ) -> AsyncMock:
        """Return a mock for scrape_person.

        When ``follow_up_text`` is given, the second call returns that text
        — used to simulate verification re-reads after an action.
        """
        first = {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"main_profile": profile_text},
        }
        if follow_up_text is None:
            return AsyncMock(return_value=first)
        second = {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"main_profile": follow_up_text},
        }
        return AsyncMock(side_effect=[first, second])

    @staticmethod
    def _signals(
        invite: bool = False,
        compose: bool = False,
        edit: bool = False,
        labeled_action: bool = False,
        labeled_anchor: bool = False,
        incoming_row: bool = False,
    ) -> ActionSignals:
        return ActionSignals(
            has_invite_anchor=invite,
            has_compose_anchor_in_action_root=compose,
            has_edit_intro_anchor=edit,
            has_labeled_action_button=labeled_action,
            has_labeled_action_anchor=labeled_anchor,
            has_incoming_action_row=incoming_row,
        )

    async def test_connectable_navigates_deeplink_and_verifies(self, mock_page):
        """Connect via deeplink: dialog opens, submit succeeds, anchor disappears."""
        extractor = LinkedInExtractor(mock_page)
        text = "Jane\n\n· 3rd\n\nEngineer\n\nConnect\nMore\nAbout\n"
        post_text = "Jane\n\n· 3rd\n\nEngineer\n\nMessage\nPending\nMore\nAbout\n"

        with (
            patch.object(
                extractor,
                "scrape_person",
                self._mock_scrape(text, follow_up_text=post_text),
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[self._signals(invite=True), self._signals()],
            ),
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ) as mock_nav,
            patch.object(
                extractor,
                "_dialog_is_open",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "connected"
        mock_nav.assert_awaited_once()
        await_args = mock_nav.await_args
        assert await_args is not None
        assert "preload/custom-invite" in await_args.args[0]

    async def test_connectable_send_failed_when_anchor_persists(self, mock_page):
        """Dialog submitted but profile still exposes Connect → send_failed."""
        extractor = LinkedInExtractor(mock_page)
        text = "Jane\n\n· 3rd\n\nEngineer\n\nConnect\nMore\nAbout\n"

        with (
            patch.object(
                extractor, "scrape_person", self._mock_scrape(text, follow_up_text=text)
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[self._signals(invite=True), self._signals(invite=True)],
            ),
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                extractor, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                extractor,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "send_failed"

    async def test_premium_upsell_message_reads_linkedin_dialog_text(self, mock_page):
        """Premium upsell detection returns LinkedIn's raw dialog text."""
        extractor = LinkedInExtractor(mock_page)
        premium_link = MagicMock()
        premium_link.wait_for = AsyncMock(return_value=None)
        premium_link.is_visible = AsyncMock(return_value=True)
        premium_link.inner_text = AsyncMock(return_value="fallback")
        premium_link.first = premium_link
        mock_page.locator.return_value = premium_link
        mock_page.evaluate = AsyncMock(
            return_value="Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium"
        )

        result = await extractor._get_premium_upsell_message(timeout=1234)

        assert (
            result
            == "Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium"
        )
        mock_page.locator.assert_called_once_with(
            'dialog[open] a[href*="/premium/"], [role="dialog"] a[href*="/premium/"]'
        )
        premium_link.wait_for.assert_awaited_once_with(state="visible", timeout=1234)

    async def test_submit_invite_dialog_reports_premium_after_add_note(self, mock_page):
        """Add-note Premium upsell is a note-limit block, not no-dialog."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        extractor = LinkedInExtractor(mock_page)
        textarea = MagicMock()
        textarea.count = AsyncMock(return_value=0)
        add_note_button = MagicMock()
        add_note_button.click = AsyncMock(return_value=None)
        buttons = MagicMock()
        buttons.count = AsyncMock(return_value=3)
        buttons.nth.return_value = add_note_button

        def locator_for(selector: str):
            return textarea if "textarea" in selector else buttons

        mock_page.locator.side_effect = locator_for
        mock_page.wait_for_selector = AsyncMock(
            side_effect=PlaywrightTimeoutError("textarea timeout")
        )

        with (
            patch.object(
                extractor, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                extractor,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value="Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium",
            ) as mock_message,
            patch.object(
                extractor, "_dismiss_dialog", new_callable=AsyncMock
            ) as mock_dismiss,
        ):
            result = await extractor._submit_invite_dialog("Hello")

        assert result == (
            False,
            False,
            "Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium",
        )
        add_note_button.click.assert_awaited_once()
        mock_message.assert_awaited_once()
        mock_dismiss.assert_awaited_once()

    async def test_submit_invite_dialog_reports_premium_after_send_click_failure(
        self, mock_page
    ):
        """Premium upsell intercepting the Send click is a note-limit block.

        When LinkedIn swaps the invite dialog for the Premium upsell at the
        moment of submit, the original primary button is detached or pointer-
        event covered, so ``_click_dialog_primary_button`` and the keyboard
        fallback both fail. Without the post-click upsell probe the caller
        would dismiss the dialog and report ``connect_unavailable`` even
        though LinkedIn's raw quota message is sitting in the visible modal.
        """
        extractor = LinkedInExtractor(mock_page)

        # Textarea already exposed so the reveal/fill branch succeeds and the
        # test focuses on the post-submit failure path.
        textarea = MagicMock()
        textarea.count = AsyncMock(return_value=1)
        textarea.first = textarea
        textarea.fill = AsyncMock()

        buttons = MagicMock()
        buttons.count = AsyncMock(return_value=2)
        primary_button = MagicMock()
        primary_button.focus = AsyncMock()
        buttons.nth.return_value = primary_button

        def locator_for(selector: str):
            return textarea if "textarea" in selector else buttons

        mock_page.locator.side_effect = locator_for
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.press = AsyncMock()

        message = "You're out of free custom notes. Bypass the limit with Premium..."

        with (
            patch.object(
                extractor,
                "_dialog_is_open",
                new_callable=AsyncMock,
                # First call: dialog open at entry. Second call: still open
                # after the keyboard fallback, so sent remains False.
                side_effect=[True, True],
            ),
            patch.object(
                extractor,
                "_fill_dialog_textarea",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                extractor,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value=message,
            ) as mock_message,
            patch.object(
                extractor, "_dismiss_dialog", new_callable=AsyncMock
            ) as mock_dismiss,
        ):
            result = await extractor._submit_invite_dialog("Hello")

        assert result == (False, False, message)
        mock_message.assert_awaited_once()
        mock_dismiss.assert_awaited_once()

    async def test_connectable_no_dialog_returns_connect_unavailable(self, mock_page):
        """Deeplink opened but no dialog appeared → connect_unavailable."""
        extractor = LinkedInExtractor(mock_page)
        text = "Jane\n\n· 3rd\n\nEngineer\n\nConnect\nMore\nAbout\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(invite=True),
            ),
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                extractor, "_dialog_is_open", new_callable=AsyncMock, return_value=False
            ),
            patch.object(extractor, "_dismiss_dialog", new_callable=AsyncMock),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"

    async def test_returns_already_connected_via_anchor(self, mock_page):
        """1st-degree detected via /messaging/compose anchor."""
        extractor = LinkedInExtractor(mock_page)
        text = "Collin\n\n· 1st\n\nEngineer\n\nMessage\nMore\nAbout\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(compose=True),
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "already_connected"

    async def test_returns_self_profile_via_edit_intro_anchor(self, mock_page):
        """Editing-your-own-profile anchor blocks connect attempts."""
        extractor = LinkedInExtractor(mock_page)
        text = "Daniel\n\nEdit profile\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(edit=True),
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"
        assert "own profile" in result["message"]

    async def test_connect_via_more_menu(self, mock_page):
        """Follow-primary profile with Connect under More: detection sees
        no invite anchor initially, _open_more_menu surfaces it, deeplink
        fires."""
        extractor = LinkedInExtractor(mock_page)
        # Pre-More: Follow primary, Connect hidden under the More dropdown.
        pre = "Christian\n\n· 2nd\n\nFounder\n\nFollow\nMessage\nMore\n"
        post = "Christian\n\n· 2nd\n\nFounder\n\nMessage\nPending\nMore\n"

        with (
            patch.object(
                extractor,
                "scrape_person",
                self._mock_scrape(pre, follow_up_text=post),
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                # 1st: follow_only (compose+labeled, no invite).
                # 2nd: post-More reread reveals invite anchor.
                # 3rd: post-deeplink verification — invite anchor gone.
                side_effect=[
                    self._signals(compose=True, labeled_action=True),
                    self._signals(invite=True, compose=True, labeled_action=True),
                    self._signals(),
                ],
            ),
            patch.object(
                extractor,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_open_more,
            patch.object(
                extractor, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                extractor,
                "_dialog_is_open",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_click_dialog_primary_button",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "connected"
        mock_open_more.assert_awaited_once()
        # Deeplink fired exactly once.
        assert mock_nav.await_count == 1
        await_args = mock_nav.await_args
        assert await_args is not None
        assert "preload/custom-invite" in await_args.args[0]

    async def test_follow_only_after_more_does_not_send(self, mock_page):
        """Pending or genuinely follow-only profile: invite anchor never
        appears even after More-menu open. Critical write-gate guardrail —
        no deeplink fires, no connection request goes out."""
        extractor = LinkedInExtractor(mock_page)
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMessage\nMore\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                # Both reads (initial + post-More) show no invite anchor.
                side_effect=[
                    self._signals(compose=True, labeled_action=True),
                    self._signals(compose=True, labeled_action=True),
                ],
            ),
            patch.object(
                extractor,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_open_more,
            patch.object(
                extractor, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                extractor, "_submit_invite_dialog", new_callable=AsyncMock
            ) as mock_submit,
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"
        assert result.get("note_sent") is False or "note_sent" not in result
        mock_open_more.assert_awaited_once()
        # Critical: deeplink must NOT fire and dialog must NOT be submitted.
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_follow_only_with_note_reports_note_limit_from_deeplink_probe(
        self, mock_page
    ):
        """A requested note may reveal Premium quota without submitting."""
        extractor = LinkedInExtractor(mock_page)
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMessage\nMore\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[
                    self._signals(compose=True, labeled_action=True),
                    self._signals(compose=True, labeled_action=True),
                ],
            ),
            patch.object(
                extractor,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                extractor,
                "_probe_invite_note_limit",
                new_callable=AsyncMock,
                return_value="Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium",
            ) as mock_probe,
            patch.object(
                extractor, "_submit_invite_dialog", new_callable=AsyncMock
            ) as mock_submit,
        ):
            result = await extractor.connect_with_person("testuser", note="Hello")

        assert result["status"] == "custom_note_limit_reached"
        assert (
            result["message"]
            == "Wysyłaj nieograniczoną liczbę spersonalizowanych zaproszeń dzięki Premium"
        )
        assert result["note_sent"] is False
        mock_nav.assert_awaited_once()
        mock_probe.assert_awaited_once()
        mock_submit.assert_not_awaited()

    async def test_more_menu_unavailable_does_not_send(self, mock_page):
        """Action root present but no More button (unusual but possible):
        _open_more_menu returns False, no retry, no deeplink fires."""
        extractor = LinkedInExtractor(mock_page)
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMessage\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(compose=True, labeled_action=True),
            ),
            patch.object(
                extractor,
                "_open_more_menu",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                extractor, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                extractor, "_submit_invite_dialog", new_callable=AsyncMock
            ) as mock_submit,
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "connect_unavailable"
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_returns_pending(self, mock_page):
        """Profile with a pending invitation: detected via labeled <a> in
        the action root. Returns status='pending' without firing the
        deeplink (LinkedIn would only show 'already invited' anyway)."""
        extractor = LinkedInExtractor(mock_page)
        text = "Frank\n\n· 3rd\n\nFounder\n\nMessage\nPending\nMore\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(compose=True, labeled_anchor=True),
            ),
            patch.object(
                extractor, "_navigate_to_page", new_callable=AsyncMock
            ) as mock_nav,
            patch.object(
                extractor, "_submit_invite_dialog", new_callable=AsyncMock
            ) as mock_submit,
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "pending"
        # No write-path side effects.
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_returns_incoming_request_accepted(self, mock_page):
        """Structural detection + structural accept click, German locale."""
        extractor = LinkedInExtractor(mock_page)
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"
        post = "Eric\n\n· 1.\n\nAachen\n\nNachricht\nMehr\nInfo\n"

        with (
            patch.object(
                extractor,
                "scrape_person",
                self._mock_scrape(pre, follow_up_text=post),
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[
                    self._signals(incoming_row=True),
                    self._signals(compose=True),
                ],
            ),
            patch.object(
                extractor,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_accept,
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ) as mock_nav,
            patch.object(
                extractor,
                "_submit_invite_dialog",
                new_callable=AsyncMock,
            ) as mock_submit,
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "accepted"
        mock_accept.assert_awaited_once()
        mock_nav.assert_not_awaited()
        mock_submit.assert_not_awaited()

    async def test_incoming_request_send_failed_when_click_fails(self, mock_page):
        """Structural accept click did not land; no locale-text guessing —
        report send_failed without navigating or clicking by text."""
        extractor = LinkedInExtractor(mock_page)
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"

        with (
            patch.object(
                extractor,
                "scrape_person",
                self._mock_scrape(pre),
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(incoming_row=True),
            ),
            patch.object(
                extractor,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                extractor,
                "click_button_by_text",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_text_click,
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
            ) as mock_nav,
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "send_failed"
        mock_nav.assert_not_awaited()
        # No text-based clicking on the destructive accept path.
        mock_text_click.assert_not_awaited()

    async def test_incoming_request_send_failed_when_no_first_degree(self, mock_page):
        """Accept clicked but profile never transitions to 1st-degree."""
        extractor = LinkedInExtractor(mock_page)
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"

        with (
            patch.object(
                extractor,
                "scrape_person",
                AsyncMock(
                    return_value={
                        "url": "https://www.linkedin.com/in/testuser/",
                        "sections": {"main_profile": pre},
                    }
                ),
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(incoming_row=True),
            ),
            patch.object(
                extractor,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "send_failed"

    async def test_incoming_request_accepted_on_settle_retry(self, mock_page):
        """The first post-click read still renders the old top card;
        the settle retry sees the 1st-degree state and reports accepted."""
        extractor = LinkedInExtractor(mock_page)
        pre = "Eric\n\n· 2.\n\nAachen\n\nAnnehmen\nIgnorieren\nMehr\nInfo\n"
        post = "Eric\n\n· 1.\n\nAachen\n\nNachricht\nMehr\nInfo\n"
        page = {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"main_profile": pre},
        }
        page_post = {
            "url": "https://www.linkedin.com/in/testuser/",
            "sections": {"main_profile": post},
        }

        with (
            patch.object(
                extractor,
                "scrape_person",
                AsyncMock(side_effect=[page, page, page_post]),
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=[
                    self._signals(incoming_row=True),
                    self._signals(incoming_row=True),
                    self._signals(compose=True),
                ],
            ),
            patch.object(
                extractor,
                "_click_incoming_accept",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "accepted"
        mock_sleep.assert_awaited_once()

    async def test_returns_unavailable_when_no_signals_and_text(self, mock_page):
        """No structural signals, no actionable text → connect_unavailable."""
        extractor = LinkedInExtractor(mock_page)
        text = "Public Figure\n\n· 3rd+\n\nCEO\n\nFollow\nMore\nAbout\n"

        with (
            patch.object(extractor, "scrape_person", self._mock_scrape(text)),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                return_value=self._signals(),
            ),
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch.object(
                extractor, "_dialog_is_open", new_callable=AsyncMock, return_value=False
            ),
            patch.object(extractor, "_dismiss_dialog", new_callable=AsyncMock),
        ):
            result = await extractor.connect_with_person("testuser")

        # follow_only path goes through deeplink; no dialog opens → unavailable
        assert result["status"] == "connect_unavailable"

    async def test_returns_unavailable_on_empty_page(self, mock_page):
        extractor = LinkedInExtractor(mock_page)

        with patch.object(
            extractor,
            "scrape_person",
            AsyncMock(
                return_value={
                    "url": "https://www.linkedin.com/in/testuser/",
                    "sections": {},
                }
            ),
        ):
            result = await extractor.connect_with_person("testuser")

        assert result["status"] == "unavailable"

    async def test_submit_invite_dialog_handles_two_button_gating_dialog(
        self, mock_page
    ):
        """Two-button "Add a note to your invitation?" gating dialog (issue
        #455): nth(0) is "Add a note", nth(1) is "Send without a note".

        Asserts the secondary-button click that reveals the textarea fires
        even with btn_count == 2 (legacy guard required >= 3 and skipped
        the click, leaving the textarea unmounted)."""
        extractor = LinkedInExtractor(mock_page)

        # Track each button click so we can assert the "Add a note" path
        # was taken to reveal the textarea.
        clicks: list[int] = []

        textarea_visible = {"value": False}

        # Two button locators inside the gating dialog: nth(0) "Add a
        # note" reveals the textarea, nth(1) "Send without a note".
        button_locators = [MagicMock(), MagicMock()]
        for idx, btn in enumerate(button_locators):

            def make_click(i: int):
                async def _click(*args, **kwargs):
                    clicks.append(i)
                    if i == 0:
                        textarea_visible["value"] = True
                    return None

                return _click

            btn.click = AsyncMock(side_effect=make_click(idx))
            btn.focus = AsyncMock()

        button_collection = MagicMock()
        button_collection.count = AsyncMock(return_value=2)
        button_collection.nth = MagicMock(side_effect=lambda i: button_locators[i])

        textarea_locator = MagicMock()
        textarea_locator.count = AsyncMock(
            side_effect=lambda: 1 if textarea_visible["value"] else 0
        )
        textarea_locator.first = textarea_locator
        textarea_locator.fill = AsyncMock()

        # Route page.locator() calls by selector — buttons vs textarea —
        # so the gating dialog's button collection is distinguishable
        # from the textarea probe.
        def locator_router(selector: str):
            if "textarea" in selector:
                return textarea_locator
            return button_collection

        mock_page.locator = MagicMock(side_effect=locator_router)
        mock_page.wait_for_selector = AsyncMock()
        mock_page.keyboard = MagicMock()
        mock_page.keyboard.press = AsyncMock()

        with (
            patch.object(
                extractor, "_dialog_is_open", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                extractor,
                "_get_premium_upsell_message",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            (
                submitted,
                note_sent,
                note_limit_message,
            ) = await extractor._submit_invite_dialog("Hi from a test")

        assert submitted is True
        assert note_sent is True
        assert note_limit_message is None
        # Clicked "Add a note" (index 0) to reveal the textarea, then the
        # primary button (index 1) to send.
        assert clicks == [0, 1]
        textarea_locator.fill.assert_awaited_once()

    async def test_references_are_grouped_by_section(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(
                        "profile text",
                        [
                            {
                                "kind": "person",
                                "url": "/in/testuser/",
                                "text": "Test User",
                            }
                        ],
                    ),
                    extracted(
                        "post text",
                        [
                            {
                                "kind": "article",
                                "url": "/pulse/test-post/",
                                "text": "Test post",
                            }
                        ],
                    ),
                ],
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert result["references"] == {
            "main_profile": [
                {"kind": "person", "url": "/in/testuser/", "text": "Test User"}
            ],
            "posts": [
                {"kind": "article", "url": "/pulse/test-post/", "text": "Test post"}
            ],
        }

    async def test_error_isolation(self, mock_page):
        """One section failing doesn't block others."""

        async def extract_with_failure(url, *args, **kwargs):
            if "experience" in url:
                raise Exception("Simulated failure")
            return extracted(f"text for {url}")

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                side_effect=extract_with_failure,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.build_issue_diagnostics",
                return_value={"issue_template_path": "/tmp/issue.md"},
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person(
                "testuser", {"main_profile", "experience", "education"}
            )

        # main_profile and education should have sections, experience should not
        assert "main_profile" in result["sections"]
        assert "education" in result["sections"]
        assert "experience" not in result["sections"]
        assert result["section_errors"]["experience"]["issue_template_path"] == (
            "/tmp/issue.md"
        )

    async def test_a_rate_limited_section_is_reported_and_stops_the_rest(
        self, mock_page
    ):
        """A throttled section is named as an error, and the walk stops there.

        Both halves matter. Returning the section as merely absent reads as
        "nothing to find" and invites the caller to try again, which is the
        opposite of what LinkedIn just asked for. And continuing to the
        remaining sections would be another navigation each, immediately after
        being told to slow down.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(_RATE_LIMITED_MSG),
                    extracted("Post text"),
                ],
            ) as mock_extract,
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert "main_profile" not in result["sections"]
        assert result["section_errors"]["main_profile"]["error_type"] == "rate_limit"
        # The second section was never fetched, so its side effect is unused.
        assert mock_extract.await_count == 1
        assert "posts" not in result["sections"]

    async def test_a_failing_urn_read_cannot_bury_the_rate_limit(self, mock_page):
        """The URN read is skipped once throttled, so it cannot overwrite it.

        It runs after the section handling but inside the same try, so a
        failure there lands in the generic handler and replaces the entry with
        a diagnostic — losing the one thing this section had to report. There
        is nothing to read a URN from on a page with no content anyway.
        """
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ),
            patch.object(
                extractor,
                "_extract_profile_urn",
                new_callable=AsyncMock,
                side_effect=RuntimeError("execution context destroyed"),
            ) as mock_urn,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", set())

        mock_urn.assert_not_awaited()
        assert result["section_errors"]["main_profile"]["error_type"] == "rate_limit"

    async def test_earlier_sections_survive_a_later_rate_limit(self, mock_page):
        """Stopping early keeps what was already gathered."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted("Profile text"),
                    extracted(_RATE_LIMITED_MSG),
                ],
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted(""),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"posts"})

        assert result["sections"]["main_profile"] == "Profile text"
        assert result["section_errors"]["posts"]["error_type"] == "rate_limit"


class TestScrapeCompany:
    async def test_company_baseline_always_included(self, mock_page):
        """Passing only posts still visits about page."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"posts"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert any("/about/" in u for u in urls)
        assert any("/posts/" in u for u in urls)
        assert "about" in result["sections"]
        assert "posts" in result["sections"]

    async def test_about_only_visits_about(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("about text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"about"})

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 1
        assert "/about/" in urls[0]
        assert set(result["sections"]) == {"about"}

    async def test_all_sections_visit_correct_urls(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company(
                "testcorp", {"about", "posts", "jobs"}
            )

        urls = [call.args[0] for call in mock_extract.call_args_list]
        assert len(urls) == 3
        assert any("/about/" in u for u in urls)
        assert any("/posts/" in u for u in urls)
        assert any("/jobs/" in u for u in urls)
        assert set(result["sections"]) == {"about", "posts", "jobs"}

    async def test_a_rate_limited_company_section_is_reported_and_stops_the_rest(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=[
                    extracted(_RATE_LIMITED_MSG),
                    extracted("Posts text"),
                ],
            ) as mock_extract,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_company("testcorp", {"posts"})

        assert "about" not in result["sections"]
        assert result["section_errors"]["about"]["error_type"] == "rate_limit"
        assert mock_extract.await_count == 1
        assert "posts" not in result["sections"]

    async def test_scrape_company_extracts_company_urn(self, mock_page):
        """End-to-end: a canned-search anchor on the company about page
        produces a ``company_urn`` reference with the parent-company id.

        Stubs ``_extract_root_content`` (rather than ``extract_page``) so
        the real ``build_references`` pipeline runs against raw anchor
        data, mirroring what the JS crawler emits live.
        """
        extractor = LinkedInExtractor(mock_page)
        raw_root = {
            "source": "root",
            "text": "About SAP\nCompany overview",
            "references": [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5B%221115%22%5D"
                    "&origin=COMPANY_PAGE_CANNED_SEARCH",
                    "text": "10K+ employees",
                    "aria_label": "",
                    "title": "",
                    "heading": "",
                    "in_article": False,
                    "in_nav": False,
                    "in_footer": False,
                }
            ],
        }
        with (
            patch.object(
                extractor,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=raw_root,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor.scrape_company("sap", {"about"})

        urns = [
            ref for ref in result["references"]["about"] if ref["kind"] == "company_urn"
        ]
        assert len(urns) == 1
        assert urns[0]["value"] == "1115"
        assert urns[0]["url"] == (
            "/search/results/people/?currentCompany=%5B%221115%22%5D"
        )
        assert "text" not in urns[0]


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
            return_value=extracted(_RATE_LIMITED_MSG),
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
            patch.object(extractor, "_navigate_to_page", side_effect=navigate_page),
            patch.object(
                extractor,
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
            patch.object(extractor, "_navigate_to_page", side_effect=navigate_page),
            patch.object(
                extractor,
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
                extracted(_RATE_LIMITED_MSG),
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
        assert _RATE_LIMITED_MSG in message

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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                return_value=extracted(_RATE_LIMITED_MSG),
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
                    [extracted(_RATE_LIMITED_MSG)],
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


class TestSettleNavigation:
    """The listener decides whether anything happened; the URL cannot."""

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    @staticmethod
    def _sleep(clock, hops, page, schedule=()):
        """Advance the clock per poll, landing each hop at its own moment.

        Each hop replaces the document, which is what a reload and a redirect
        both do. A same-document change is spelled by leaving `time_origin`
        alone instead.
        """
        pending = list(schedule)

        async def sleep(seconds: float) -> None:
            clock.now += seconds
            while pending and pending[0] <= clock.now:
                pending.pop(0)
                hops.append("hop")
                page.time_origin += 1.0

        return sleep

    async def test_a_destroyed_context_reads_as_no_document(self, mock_page):
        """A navigation in flight takes the context the reading needs with it.

        The class patchright raises for that is `Error`, measured, and not a
        `RuntimeError`. A handler narrowed to the latter would turn the
        ordinary case this reading exists for into an unhandled exception,
        so the double is held to the real class.
        """
        extractor = LinkedInExtractor(mock_page)
        mock_page.evaluate = AsyncMock(
            side_effect=PatchrightError(
                "Page.evaluate: Execution context was destroyed, "
                "most likely because of a navigation."
            )
        )

        assert await extractor._document_origin() is None

    async def test_a_page_going_nowhere_costs_the_lag_and_not_the_quiet(
        self, mock_page
    ):
        """An ordinary failure has no navigation behind it.

        Charging it the quiet window spends half a second on every DOM error,
        and a call near its tool timeout loses the diagnostic it was about to
        build.
        """
        clock = self.Clock()
        extractor = LinkedInExtractor(mock_page)
        hops: list[str] = []

        with (
            patch.object(extractor_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page),
            ),
        ):
            assert (
                await extractor._settle_navigation(hops, mock_page.time_origin) is False
            )

        assert clock.now < extractor_module._URL_SETTLE_QUIET
        assert clock.now >= extractor_module._URL_SETTLE_LAG

    async def test_a_reload_is_a_navigation_though_the_address_holds(self, mock_page):
        """A reload replaces the document and leaves the address alone.

        Comparing addresses calls the replacement the same page, so a picker
        served by a reload was read as search results. The event says so.
        """
        clock = self.Clock()
        extractor = LinkedInExtractor(mock_page)
        hops: list[str] = []

        with (
            patch.object(extractor_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page, [0.05]),
            ),
        ):
            assert (
                await extractor._settle_navigation(hops, mock_page.time_origin) is True
            )

        assert mock_page.wait_for_load_state.await_count == 1

    async def test_a_chain_is_followed_to_its_last_hop(self, mock_page):
        """Hops are counted, not compared.

        A chain that returns to the route it started on reads as one that
        never left, and its last hop is what decides whether this is a
        checkpoint.
        """
        clock = self.Clock()
        extractor = LinkedInExtractor(mock_page)
        hops: list[str] = []

        with (
            patch.object(extractor_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page, [0.05, 0.4]),
            ),
        ):
            assert (
                await extractor._settle_navigation(hops, mock_page.time_origin) is True
            )

        assert len(hops) == 2
        assert clock.now >= 0.4 + extractor_module._URL_SETTLE_QUIET

    async def test_a_history_change_is_not_a_navigation(self, mock_page):
        """LinkedIn rewrites its own address, and the event cannot tell.

        `pushState`, `replaceState` and a hash change each fire
        `framenavigated` on the main frame, and a search page appends
        `currentJobId` that way by itself. Settling on the event alone charges
        every healthy page the quiet window plus a document wait plus the
        barrier check that follows from it. The document surviving is what
        says nothing was replaced.
        """
        clock = self.Clock()
        extractor = LinkedInExtractor(mock_page)
        origin = mock_page.time_origin
        navigate(mock_page, same_document=True)
        hops = ["https://www.linkedin.com/jobs/search/?currentJobId=1"]

        with (
            patch.object(extractor_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page),
            ),
        ):
            assert await extractor._settle_navigation(hops, origin) is False

        assert clock.now >= extractor_module._URL_SETTLE_LAG
        assert clock.now < extractor_module._URL_SETTLE_QUIET
        assert mock_page.wait_for_load_state.await_count == 0

    async def test_a_redirect_behind_a_history_change_is_still_caught(self, mock_page):
        """The address is announced before the checkpoint commits.

        A search page names its selected job the moment a card is chosen, and
        a checkpoint arriving right behind it would be waved through by a
        settler that left on the first hop. The wait is for a replaced
        document, so the second hop is what ends it.
        """
        clock = self.Clock()
        extractor = LinkedInExtractor(mock_page)
        origin = mock_page.time_origin
        navigate(mock_page, same_document=True)
        hops = ["https://www.linkedin.com/jobs/search/?currentJobId=1"]

        with (
            patch.object(extractor_module, "time", clock),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                side_effect=self._sleep(clock, hops, mock_page, [0.05]),
            ),
        ):
            assert await extractor._settle_navigation(hops, origin) is True

        assert mock_page.wait_for_load_state.await_count == 1


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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                extractor, "_extract_root_content", side_effect=reload_at_read
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "linkedin_mcp_server.scraping.extractor.detect_auth_barrier",
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
        pages = iter([extracted("first page"), extracted(_RATE_LIMITED_MSG)])
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
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(_RATE_LIMITED_MSG),
        ):
            result = await getattr(extractor, method)(*args)

        assert result["sections"] == {}
        assert result["section_errors"][section]["error_type"] == "rate_limit"


class TestSearchPeople:
    async def test_search_people_omits_orphaned_references(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted(
                "",
                [
                    {
                        "kind": "person",
                        "url": "/in/testuser/",
                        "text": "Test User",
                    }
                ],
            ),
        ):
            result = await extractor.search_people("python")

        assert result["sections"] == {}
        assert "references" not in result

    async def test_search_people_network_filter_first_degree(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await extractor.search_people("engineer", network=["F"])

        assert "network=%5B%22F%22%5D" in result["url"]

    async def test_search_people_network_filter_multi_degree(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await extractor.search_people("engineer", network=["F", "S"])

        assert "network=%5B%22F%22%2C%22S%22%5D" in result["url"]

    async def test_search_people_current_company_filter(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await extractor.search_people("engineer", current_company="1115")

        assert "currentCompany=%5B%221115%22%5D" in result["url"]

    async def test_search_people_invalid_network_token_raises(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with pytest.raises(ValueError, match="Invalid network token"):
            await extractor.search_people("engineer", network=["X"])

    async def test_search_people_rejects_plain_company_name(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with pytest.raises(ValueError, match="must be a numeric"):
            await extractor.search_people("engineer", current_company="SAP")

    async def test_search_people_rejects_unicode_digit_company(self, mock_page):
        """LinkedIn URN ids are ASCII decimal; reject Unicode digits even
        though ``str.isdigit()`` would accept them."""
        extractor = LinkedInExtractor(mock_page)
        with pytest.raises(ValueError, match="must be a numeric"):
            await extractor.search_people("engineer", current_company="١١١٥")

    async def test_search_people_empty_current_company_is_noop(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await extractor.search_people("engineer", current_company="")

        assert "currentCompany" not in result["url"]

    async def test_search_people_combines_all_filters(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor,
            "extract_page",
            new_callable=AsyncMock,
            return_value=extracted("Jane Doe"),
        ):
            result = await extractor.search_people(
                "engineer",
                location="Seattle",
                network=["F"],
                current_company="1115",
            )

        assert "keywords=engineer" in result["url"]
        assert "location=Seattle" in result["url"]
        assert "network=%5B%22F%22%5D" in result["url"]
        assert "currentCompany=%5B%221115%22%5D" in result["url"]


class TestBuildContentSearchUrl:
    """Tests for _build_content_search_url URL construction."""

    def test_basic_keywords(self):
        url = LinkedInExtractor._build_content_search_url("Buscamos Unity")
        assert url == (
            "https://www.linkedin.com/search/results/content/"
            "?keywords=Buscamos+Unity&origin=FACETED_SEARCH"
        )

    def test_date_posted_past_week(self):
        url = LinkedInExtractor._build_content_search_url(
            "Buscamos Unity", date_posted="past-week"
        )
        assert "datePosted=%5B%22past-week%22%5D" in url

    def test_date_posted_alias_normalized(self):
        url = LinkedInExtractor._build_content_search_url(
            "python", date_posted="past_24_hours"
        )
        assert "datePosted=%5B%22past-24h%22%5D" in url

    def test_every_accepted_date_posted_reaches_linkedin_as_a_real_token(self):
        """LinkedIn ignores an unrecognized token instead of rejecting it, so
        an accepted value that never maps to one of its three would return
        unfiltered results while looking filtered."""
        for accepted, expected in _CONTENT_DATE_POSTED_MAP.items():
            url = LinkedInExtractor._build_content_search_url(
                "python", date_posted=accepted
            )
            assert expected in ("past-24h", "past-week", "past-month")
            assert f"%22{expected}%22" in url

    def test_no_date_posted_omits_facet(self):
        url = LinkedInExtractor._build_content_search_url("python")
        assert "datePosted" not in url

    def test_whitespace_date_posted_omits_facet(self):
        # Whitespace-only date_posted must be ignored, not appended as an
        # invalid facet token (regression guard).
        url = LinkedInExtractor._build_content_search_url("python", date_posted="   ")
        assert "datePosted" not in url


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
            return_value=extracted(_RATE_LIMITED_MSG),
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


class TestStripLinkedInNoise:
    def test_strips_footer(self):
        text = "Bill Gates\nChair, Gates Foundation\n\nAbout\nAccessibility\nTalent Solutions\nCareers"
        assert strip_linkedin_noise(text) == "Bill Gates\nChair, Gates Foundation"

    def test_strips_footer_with_talent_solutions_variant(self):
        text = "Profile content here\n\nAbout\nTalent Solutions\nMore footer"
        assert strip_linkedin_noise(text) == "Profile content here"

    def test_strips_sidebar_recommendations(self):
        text = "Experience\nCo-chair\nGates Foundation\n\nMore profiles for you\nSundar Pichai\nCEO at Google"
        assert strip_linkedin_noise(text) == "Experience\nCo-chair\nGates Foundation"

    def test_strips_premium_upsell(self):
        text = "Education\nHarvard University\n\nExplore premium profiles\nRandom Person\nSoftware Engineer"
        assert strip_linkedin_noise(text) == "Education\nHarvard University"

    def test_picks_earliest_marker(self):
        text = "Content\n\nExplore premium profiles\nStuff\n\nMore profiles for you\nMore stuff\n\nAbout\nAccessibility"
        assert strip_linkedin_noise(text) == "Content"

    def test_no_noise_returns_unchanged(self):
        text = "Clean content with no LinkedIn chrome"
        assert strip_linkedin_noise(text) == "Clean content with no LinkedIn chrome"

    def test_empty_string(self):
        assert strip_linkedin_noise("") == ""

    def test_truncate_noise_preserves_media_controls_for_rate_limit_detection(self):
        text = "Play\nLoaded: 100.00%\nRemaining time 0:07\nShow captions"
        assert _truncate_linkedin_noise(text) == text
        assert strip_linkedin_noise(text) == ""

    def test_about_in_profile_content_not_stripped(self):
        """'About' followed by actual content (not 'Accessibility') should be preserved."""
        text = "About\nChair of the Gates Foundation.\n\nFeatured\nPost"
        assert (
            strip_linkedin_noise(text)
            == "About\nChair of the Gates Foundation.\n\nFeatured\nPost"
        )

    def test_real_footer_with_languages(self):
        text = (
            "Company info\n\n"
            "About\nAccessibility\nTalent Solutions\nCareers\n"
            "Select language\nEnglish (English)\nDeutsch (German)"
        )
        assert strip_linkedin_noise(text) == "Company info"

    def test_preserves_real_careers_content(self):
        text = "Careers\nWe're hiring globally.\nOpen roles in engineering and design."
        assert strip_linkedin_noise(text) == text

    def test_preserves_real_questions_content(self):
        text = "Questions?\nReach out to our recruiting team for details."
        assert strip_linkedin_noise(text) == text

    def test_strips_media_controls_lines(self):
        text = (
            "Feed post number 1\n"
            "Play\n"
            "Loaded: 100.00%\n"
            "Remaining time 0:07\n"
            "Playback speed\n"
            "Actual post content\n"
            "Show captions\n"
            "Close modal window"
        )
        assert strip_linkedin_noise(text) == "Feed post number 1\nActual post content"


class TestStripConversationChrome:
    THREAD = (
        "MAY 25\n"
        "Grace Hopper sent the following message at 5:27 PM\n"
        "Grace Hopper  5:27 PM\n"
        "\n"
        "Hello!"
    )
    PAGE = (
        "Messaging\n"
        "Search messages\n"
        "Compose a new message\n"
        "Inbox\n"
        "Attention screen reader users, messaging items continuously update.\n"
        "Ada Lovelace\n"
        "Jun 8\n"
        "Ada: Preview belonging to a different conversation\n"
        ". Press return to go to conversation details\n"
        "Open the options list in your conversation with Ada Lovelace and Grace Hopper\n"
        "Status is reachable\n"
        "Load more conversations\n"
        "Grace Hopper\n"
        "Status is online\n"
        "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
        + THREAD
        + "\n"
        "Maximize compose field\n"
        "Attach an image to your conversation with Grace Hopper\n"
        "Open GIF Keyboard\n"
        "Send\n"
        "Open send options"
    )

    def test_strips_sidebar_and_composer(self):
        assert strip_conversation_chrome(self.PAGE) == self.THREAD

    def test_other_conversation_previews_removed(self):
        assert "different conversation" not in strip_conversation_chrome(self.PAGE)
        assert "Ada Lovelace" not in strip_conversation_chrome(self.PAGE)

    def test_missing_composer_strips_only_leading_chrome(self):
        text = (
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            + self.THREAD
        )
        assert strip_conversation_chrome(text) == self.THREAD

    def test_missing_thread_header_strips_only_composer(self):
        text = self.THREAD + "\nMaximize compose field\nOpen send options"
        assert strip_conversation_chrome(text) == self.THREAD

    def test_quoted_composer_string_in_message_survives(self):
        text = (
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            "Maximize compose field\n"
            "is the label I keep seeing\n"
            "Maximize compose field\n"
            "Open send options"
        )
        assert (
            strip_conversation_chrome(text)
            == "Maximize compose field\nis the label I keep seeing"
        )

    def test_quoted_companion_with_suffix_does_not_confirm_composer(self):
        text = "Hello!\nMaximize compose field\nOpen send options is what I clicked"
        assert strip_conversation_chrome(text) == text

    def test_quoted_attach_text_does_not_confirm_composer(self):
        text = (
            "Hello!\n"
            "Maximize compose field\n"
            "Attach an image to your conversation with Grace is the label I clicked"
        )
        assert strip_conversation_chrome(text) == text

    def test_distant_companion_text_does_not_confirm_composer(self):
        filler = "\n".join(f"message {n}" for n in range(10))
        text = (
            "Maximize compose field\n"
            + filler
            + "\nOpen send options is what I clicked"
        )
        assert strip_conversation_chrome(text) == text

    def test_quoted_composer_without_companions_does_not_truncate(self):
        text = (
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            "Hello!\n"
            "Maximize compose field\n"
            "is what the button says"
        )
        assert (
            strip_conversation_chrome(text)
            == "Hello!\nMaximize compose field\nis what the button says"
        )

    def test_quoted_thread_header_in_message_keeps_earlier_messages(self):
        text = (
            "Load more conversations\n"
            "Grace Hopper\n"
            "Open the options list in your conversation with Grace Hopper and Ada Lovelace\n"
            "Hello!\n"
            "Open the options list in your conversation with is a label I quoted\n"
            "Bye!\n"
            "Maximize compose field\n"
            "Open send options"
        )
        assert strip_conversation_chrome(text) == (
            "Hello!\n"
            "Open the options list in your conversation with is a label I quoted\n"
            "Bye!"
        )

    def test_sidebar_end_without_thread_header_still_strips_sidebar(self):
        text = (
            "Ada: Preview belonging to a different conversation\n"
            "Load more conversations\n" + self.THREAD
        )
        assert strip_conversation_chrome(text) == self.THREAD

    def test_unknown_locale_returns_unchanged(self):
        assert strip_conversation_chrome(self.PAGE, locale="de") == self.PAGE

    def test_no_markers_returns_stripped_text(self):
        assert strip_conversation_chrome("Hello!\nHi there!") == "Hello!\nHi there!"

    def test_empty_string(self):
        assert strip_conversation_chrome("") == ""


class TestActivityFeedExtraction:
    """Tests for activity page detection and wait behavior in _extract_page_once."""

    async def test_activity_page_waits_for_content_and_uses_slow_scroll(
        self, mock_page
    ):
        """Activity URLs should call wait_for_function and use slower scroll params."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Post content " * 50,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 1.0
        assert kwargs["max_scrolls"] == 10
        assert len(result.text) > 200

    async def test_company_posts_page_waits_for_content_and_uses_slow_scroll(
        self, mock_page
    ):
        """Company posts URLs get the same lazy-load wait and scroll budget
        as person activity pages, even though they lack /recent-activity/."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Post content " * 50,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/company/microsoft/posts/",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 1.0
        assert kwargs["max_scrolls"] == 10
        assert len(result.text) > 200

    async def test_company_posts_page_with_query_string_still_waits(self, mock_page):
        """The lazy-load branch keys off the parsed path, so a company posts
        url carrying a query string is not mistaken for a static page."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Post content " * 50,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/company/microsoft/posts/?viewAsMember=true",
                section_name="posts",
            )

        mock_page.wait_for_function.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["max_scrolls"] == 10

    async def test_non_activity_non_details_page_skips_wait_and_uses_fast_scroll(
        self, mock_page
    ):
        """Plain profile URLs (not activity, search, or details) skip wait_for_function."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Profile text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
            )

        mock_page.wait_for_function.assert_not_awaited()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 0.5
        assert kwargs["max_scrolls"] == 5

    async def test_details_page_waits_for_panel_content(self, mock_page):
        """Detail pages (/details/experience/ etc.) call wait_for_function to wait for the panel."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Experience\nSoftware Engineer",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
            )

        mock_page.wait_for_function.assert_awaited_once()
        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["pause_time"] == 0.5
        assert kwargs["max_scrolls"] == 5

    async def test_max_scrolls_override_passed_to_scroll_to_bottom(self, mock_page):
        """Custom max_scrolls on a detail page overrides the default of 5."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Experience\nSoftware Engineer",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
                max_scrolls=20,
            )

        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["max_scrolls"] == 20

    async def test_default_scrolls_without_max_scrolls_override(self, mock_page):
        """Without max_scrolls, detail pages use the default of 5."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Experience\nSoftware Engineer",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
            )

        mock_scroll.assert_awaited_once()
        _, kwargs = mock_scroll.call_args
        assert kwargs["max_scrolls"] == 5

    async def test_details_page_clicks_show_more_until_gone(self, mock_page):
        """Detail pages click 'Show more' in a loop until the button disappears."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()

        show_more = MagicMock()
        # count() returns 1, 1, 0 across iterations — button disappears on 3rd check
        show_more.count = AsyncMock(side_effect=[1, 1, 0])
        show_more.is_visible = AsyncMock(return_value=True)
        show_more.scroll_into_view_if_needed = AsyncMock()
        show_more.click = AsyncMock()
        show_more.first = show_more
        show_more.filter = MagicMock(return_value=show_more)

        def locator_side_effect(selector):
            if selector == "main button":
                return show_more
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        extractor = LinkedInExtractor(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/certifications/",
                section_name="certifications",
            )

        assert show_more.click.await_count == 2

    async def test_details_page_show_more_respects_max_scrolls_budget(self, mock_page):
        """When 'Show more' never disappears, loop exits after max_scrolls clicks."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()

        show_more = MagicMock()
        show_more.count = AsyncMock(return_value=1)  # always present
        show_more.is_visible = AsyncMock(return_value=True)
        show_more.scroll_into_view_if_needed = AsyncMock()
        show_more.click = AsyncMock()
        show_more.first = show_more
        show_more.filter = MagicMock(return_value=show_more)

        def locator_side_effect(selector):
            if selector == "main button":
                return show_more
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        extractor = LinkedInExtractor(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/details/experience/",
                section_name="experience",
                max_scrolls=3,
            )

        assert show_more.click.await_count == 3

    async def test_non_details_page_does_not_click_show_more(self, mock_page):
        """Non-details URLs (main profile, activity) skip the Show more loop."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()

        show_more = MagicMock()
        show_more.count = AsyncMock(return_value=1)
        show_more.click = AsyncMock()
        show_more.first = show_more
        show_more.filter = MagicMock(return_value=show_more)

        def locator_side_effect(selector):
            if selector == "main button":
                return show_more
            return MagicMock(count=AsyncMock(return_value=0))

        mock_page.locator = MagicMock(side_effect=locator_side_effect)
        extractor = LinkedInExtractor(mock_page)

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
            )

        show_more.click.assert_not_awaited()

    async def test_activity_page_timeout_proceeds_gracefully(self, mock_page):
        """When activity feed content never loads, extraction proceeds with available text."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        tab_headers = "All activity\nPosts\nComments\nVideos\nImages"
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": tab_headers, "references": []}
        )
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout")
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/recent-activity/all/",
                section_name="posts",
            )

        # Should return whatever text is available, not crash
        assert result.text == tab_headers


class TestCompanyPeopleExtraction:
    """Tests for /company/<slug>/people/ hydration wait in _extract_page_once."""

    async def test_waits_for_listing_with_5s_timeout(self, mock_page):
        """Company /people/ pages call wait_for_function so the employee
        listing has hydrated before scroll/extract. Empty/restricted listings
        are common, so the timeout is 5s rather than the 10s pattern shared
        with is_search/is_details."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Anthropic\nFollowing\nHome\nAbout\nPeople",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/company/anthropicresearch/people/",
                section_name="employees",
            )

        mock_page.wait_for_function.assert_awaited_once()
        wait_predicate = mock_page.wait_for_function.call_args[0][0]
        wait_kwargs = mock_page.wait_for_function.call_args.kwargs
        assert "/in/" in wait_predicate
        assert "querySelectorAll" in wait_predicate
        assert wait_kwargs["timeout"] == 5000
        mock_scroll.assert_awaited_once()

    async def test_continues_extraction_on_wait_timeout(self, mock_page):
        """When the hydration wait times out (genuinely empty listing), the
        extractor swallows PlaywrightTimeoutError and still scrolls + extracts
        rather than propagating the error to the caller."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Empty company page",
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout")
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
            ) as mock_scroll,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/company/anthropicresearch/people/",
                section_name="employees",
            )

        mock_scroll.assert_awaited_once()
        assert result.text  # non-empty placeholder text from the mock


class TestSearchResultsExtraction:
    """Tests for search results page detection and wait behavior in _extract_page_once."""

    async def test_search_results_page_waits_for_content(self, mock_page):
        """Search results URLs should call wait_for_function to wait for content."""
        mock_page.evaluate = AsyncMock(
            return_value={
                "source": "root",
                "text": "Search results for John Doe. " * 10,
                "references": [],
            }
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
            )

        mock_page.wait_for_function.assert_awaited_once()
        assert len(result.text) > 100

    async def test_non_search_page_does_not_wait_for_search_content(self, mock_page):
        """Non-search URLs should not trigger the search results wait."""
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": "Profile text", "references": []}
        )
        mock_page.wait_for_function = AsyncMock()
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            await extractor._extract_page_once(
                "https://www.linkedin.com/in/billgates/",
                section_name="main_profile",
            )

        mock_page.wait_for_function.assert_not_awaited()

    async def test_search_results_timeout_proceeds_gracefully(self, mock_page):
        """When search results never load, extraction proceeds with available text."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        placeholder = "Search results for John Doe. No results found"
        mock_page.evaluate = AsyncMock(
            return_value={"source": "root", "text": placeholder, "references": []}
        )
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("Timeout")
        )
        extractor = LinkedInExtractor(mock_page)
        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.scroll_to_bottom",
                new_callable=AsyncMock,
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
            result = await extractor._extract_page_once(
                "https://www.linkedin.com/search/results/people/?keywords=John+Doe",
                section_name="search_results",
            )

        assert result.text == placeholder


class TestScrapePersonCallbacks:
    """Test that scrape_person invokes callbacks at each stage."""

    async def test_scrape_person_calls_callbacks(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch.object(
                extractor,
                "_extract_overlay",
                new_callable=AsyncMock,
                return_value=extracted("overlay text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.scrape_person(
                "testuser", {"experience", "education"}, callbacks=cb
            )

        cb.on_start.assert_awaited_once()
        assert cb.on_start.call_args[0][0] == "person profile"

        # 3 sections: main_profile (always) + experience + education
        assert cb.on_progress.await_count == 3
        messages = [c.args[0] for c in cb.on_progress.call_args_list]
        assert messages == [
            "Scraped main_profile (1/3)",
            "Scraped experience (2/3)",
            "Scraped education (3/3)",
        ]
        # Last section should be at 95%
        assert cb.on_progress.call_args_list[-1].args[1] == 95

        cb.on_complete.assert_awaited_once()
        assert cb.on_complete.call_args[0][0] == "person profile"
        cb.on_error.assert_not_awaited()

    async def test_scrape_person_no_callbacks_by_default(self, mock_page):
        """Without callbacks, scrape_person works identically to before."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert "main_profile" in result["sections"]

    async def test_scrape_person_calls_on_error(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                side_effect=LinkedInScraperException("boom"),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(LinkedInScraperException):
                await extractor.scrape_person(
                    "testuser", {"main_profile"}, callbacks=cb
                )

        cb.on_start.assert_awaited_once()
        cb.on_error.assert_awaited_once()
        error_arg = cb.on_error.call_args[0][0]
        assert isinstance(error_arg, LinkedInScraperException)
        assert "boom" in str(error_arg)
        cb.on_complete.assert_not_awaited()


class TestMainProfileAlreadyLoaded:
    """Reuse path for scrape_person when get_my_profile already loaded the page."""

    async def test_get_my_profile_passes_already_loaded_flag(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/in/realuser/"
        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock) as nav,
            patch.object(
                extractor,
                "scrape_person",
                new_callable=AsyncMock,
                return_value={"url": "...", "sections": {}},
            ) as scrape,
        ):
            await extractor.get_my_profile(sections={"main_profile"})

        nav.assert_awaited_once_with("https://www.linkedin.com/in/me/")
        assert scrape.await_count == 1
        assert scrape.call_args.kwargs["main_profile_already_loaded"] is True

    async def test_scrape_person_already_loaded_skips_navigation(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/in/foo/"
        with (
            patch.object(
                extractor,
                "_extract_loaded_section",
                new_callable=AsyncMock,
                return_value=extracted("reused"),
            ) as loaded,
            patch.object(
                extractor, "extract_page", new_callable=AsyncMock
            ) as extract_page,
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock) as nav,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.scrape_person(
                "foo", {"main_profile"}, main_profile_already_loaded=True
            )

        loaded.assert_awaited_once()
        extract_page.assert_not_awaited()
        nav.assert_not_awaited()

    async def test_scrape_person_already_loaded_url_mismatch_falls_back(
        self, mock_page
    ):
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/feed/"
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("fallback"),
            ) as extract_page,
            patch.object(
                extractor,
                "_extract_loaded_section",
                new_callable=AsyncMock,
            ) as loaded,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.scrape_person(
                "foo", {"main_profile"}, main_profile_already_loaded=True
            )

        extract_page.assert_awaited_once()
        loaded.assert_not_awaited()

    async def test_scrape_person_already_loaded_rate_limit_falls_back(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.url = "https://www.linkedin.com/in/foo/"

        from linkedin_mcp_server.scraping.extractor import _RATE_LIMITED_MSG

        with (
            patch.object(
                extractor,
                "_extract_loaded_section",
                new_callable=AsyncMock,
                return_value=extracted(_RATE_LIMITED_MSG),
            ) as loaded,
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("retry succeeded"),
            ) as extract_page,
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person(
                "foo", {"main_profile"}, main_profile_already_loaded=True
            )

        loaded.assert_awaited_once()
        extract_page.assert_awaited_once()
        assert result["sections"]["main_profile"] == "retry succeeded"


class TestScrapeCompanyCallbacks:
    """Test that scrape_company invokes callbacks at each stage."""

    async def test_scrape_company_calls_callbacks(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        cb = MagicMock(spec=ProgressCallback)
        cb.on_start = AsyncMock()
        cb.on_progress = AsyncMock()
        cb.on_complete = AsyncMock()
        cb.on_error = AsyncMock()

        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("text"),
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.scrape_company(
                "testcorp", {"about", "posts", "jobs"}, callbacks=cb
            )

        cb.on_start.assert_awaited_once()
        assert cb.on_start.call_args[0][0] == "company profile"

        # 3 sections: about + posts + jobs
        assert cb.on_progress.await_count == 3
        messages = [c.args[0] for c in cb.on_progress.call_args_list]
        assert messages == [
            "Scraped about (1/3)",
            "Scraped posts (2/3)",
            "Scraped jobs (3/3)",
        ]
        assert cb.on_progress.call_args_list[-1].args[1] == 95

        cb.on_complete.assert_awaited_once()
        assert cb.on_complete.call_args[0][0] == "company profile"
        cb.on_error.assert_not_awaited()


class TestGetSidebarProfiles:
    async def test_returns_sidebar_profiles_from_all_sections(self, mock_page):
        """Happy path: extracts profiles from all sections, merges Show all results."""
        sidebar_js_result = {
            "sections": {
                "more_profiles_for_you": ["/in/alice/", "/in/bob/"],
                "explore_premium_profiles": ["/in/carol/"],
                "people_you_may_know": ["/in/dave/"],
            },
            "showAllUrls": {
                "more_profiles_for_you": "https://www.linkedin.com/search/results/people/?keywords=test",
            },
        }
        show_all_js_result = ["/in/alice/", "/in/eve/", "/in/frank/"]

        mock_page.evaluate = AsyncMock(
            side_effect=[sidebar_js_result, show_all_js_result]
        )
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            result = await extractor.get_sidebar_profiles("testuser")

        assert result["url"] == "https://www.linkedin.com/in/testuser/"
        mpfy = result["sidebar_profiles"]["more_profiles_for_you"]
        # sidebar links first, then show_all expansion, deduped
        assert mpfy == ["/in/alice/", "/in/bob/", "/in/eve/", "/in/frank/"]
        assert result["sidebar_profiles"]["explore_premium_profiles"] == ["/in/carol/"]
        assert result["sidebar_profiles"]["people_you_may_know"] == ["/in/dave/"]

    @pytest.mark.parametrize(
        ("error_type", "message"),
        [
            pytest.param(
                AuthenticationError,
                "Run with --login",
                id="authentication-error",
            ),
            pytest.param(
                ProxyConnectionError,
                "Proxy unavailable",
                id="proxy-connection-error",
            ),
        ],
    )
    async def test_scraper_exception_from_show_all_propagates(
        self,
        mock_page,
        error_type: type[LinkedInScraperException],
        message: str,
    ):
        sidebar_js_result = {
            "sections": {"more_profiles_for_you": ["/in/alice/"]},
            "showAllUrls": {
                "more_profiles_for_you": "https://www.linkedin.com/search/results/people/?keywords=test"
            },
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=[None, error_type(message)],
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
            pytest.raises(error_type, match=message),
        ):
            await extractor.get_sidebar_profiles("testuser")

    async def test_raw_exception_from_show_all_keeps_inline_profiles(self, mock_page):
        show_all_url = "https://www.linkedin.com/search/results/people/?keywords=test"
        sidebar_js_result = {
            "sections": {"more_profiles_for_you": ["/in/alice/"]},
            "showAllUrls": {"more_profiles_for_you": show_all_url},
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=[None, RuntimeError("navigation failed")],
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
            patch.object(extractor_module.logger, "debug") as debug_mock,
        ):
            result = await extractor.get_sidebar_profiles("testuser")

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "sidebar_profiles": {"more_profiles_for_you": ["/in/alice/"]},
        }
        debug_mock.assert_called_once_with(
            "Failed to navigate to Show all for section %s: %s",
            "more_profiles_for_you",
            show_all_url,
        )

    async def test_skips_show_all_when_url_contains_premium(self, mock_page):
        """Show all URL containing /premium is skipped without navigation."""
        sidebar_js_result = {
            "sections": {"explore_premium_profiles": ["/in/carol/"]},
            "showAllUrls": {
                "explore_premium_profiles": "https://www.linkedin.com/premium/products/"
            },
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        extractor = LinkedInExtractor(mock_page)
        navigate_mock = AsyncMock()
        with (
            patch.object(extractor, "_navigate_to_page", navigate_mock),
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
            result = await extractor.get_sidebar_profiles("testuser")

        navigate_mock.assert_awaited_once()  # only the initial profile navigation
        mock_page.evaluate.assert_awaited_once()  # no show_all JS call
        assert result["sidebar_profiles"]["explore_premium_profiles"] == ["/in/carol/"]

    async def test_skips_show_all_when_page_redirects_to_premium(self, mock_page):
        """If navigating to Show all lands on a /premium URL, skip that section."""
        sidebar_js_result = {
            "sections": {"more_profiles_for_you": ["/in/alice/"]},
            "showAllUrls": {
                "more_profiles_for_you": "https://www.linkedin.com/search/results/people/?keywords=test"
            },
        }
        mock_page.evaluate = AsyncMock(return_value=sidebar_js_result)
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        navigate_call_count = 0

        async def fake_navigate(url: str) -> None:
            nonlocal navigate_call_count
            navigate_call_count += 1
            if navigate_call_count >= 2:
                mock_page.url = "https://www.linkedin.com/premium/grow-your-network/"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor, "_navigate_to_page", side_effect=fake_navigate),
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
            result = await extractor.get_sidebar_profiles("testuser")

        mock_page.evaluate.assert_awaited_once()  # sidebar JS only, no show_all expansion
        assert result["sidebar_profiles"]["more_profiles_for_you"] == ["/in/alice/"]

    async def test_returns_empty_sidebar_profiles_when_no_sections_found(
        self, mock_page
    ):
        """No matching sidebar headings -> empty sidebar_profiles dict."""
        mock_page.evaluate = AsyncMock(return_value={"sections": {}, "showAllUrls": {}})
        mock_page.url = "https://www.linkedin.com/in/testuser/"

        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            result = await extractor.get_sidebar_profiles("testuser")

        assert result == {
            "url": "https://www.linkedin.com/in/testuser/",
            "sidebar_profiles": {},
        }


class TestExtractProfileUrn:
    async def test_returns_urn_from_compose_href(self, mock_page):
        """Extracts the recipient URN from the messaging compose link."""
        mock_page.evaluate = AsyncMock(
            return_value="/messaging/compose/?recipient=ACoAAB1IelEBLEkqTkNbZ-a1D8mq5R-6C1ihSEk&lipi=urn..."
        )

        extractor = LinkedInExtractor(mock_page)
        result = await extractor._extract_profile_urn()

        assert result == "ACoAAB1IelEBLEkqTkNbZ-a1D8mq5R-6C1ihSEk"

    async def test_returns_none_when_no_compose_button(self, mock_page):
        """Returns None when no messaging compose link is found."""
        mock_page.evaluate = AsyncMock(return_value=None)

        extractor = LinkedInExtractor(mock_page)
        result = await extractor._extract_profile_urn()

        assert result is None

    async def test_returns_none_when_no_recipient_param(self, mock_page):
        """Returns None when the compose href has no recipient query param."""
        mock_page.evaluate = AsyncMock(
            return_value="/messaging/compose/?someOtherParam=value"
        )

        extractor = LinkedInExtractor(mock_page)
        result = await extractor._extract_profile_urn()

        assert result is None


class TestScrapePersonProfileUrn:
    async def test_includes_profile_urn_in_result_when_found(self, mock_page):
        """scrape_person includes profile_urn in result when _extract_profile_urn returns a value."""
        urn = "ACoAAB1IelEBLEkqTkNbZ-a1D8mq5R-6C1ihSEk"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ),
            patch.object(
                extractor,
                "_extract_profile_urn",
                new_callable=AsyncMock,
                return_value=urn,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert result["profile_urn"] == urn

    async def test_omits_profile_urn_when_not_found(self, mock_page):
        """scrape_person omits profile_urn key when _extract_profile_urn returns None."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ),
            patch.object(
                extractor,
                "_extract_profile_urn",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.scrape_person("testuser", {"main_profile"})

        assert "profile_urn" not in result


class TestGetInbox:
    async def test_returns_inbox_section(self, mock_page):
        """get_inbox returns sections with inbox key."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
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
                extractor,
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", nav_mock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", nav_mock),
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
                extractor,
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
            patch.object(extractor, "_navigate_to_page", nav_mock),
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
                extractor,
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", nav_mock),
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
            patch.object(extractor, "_navigate_to_page", nav_mock),
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
            patch.object(extractor, "_navigate_to_page", nav_mock),
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
                extractor,
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
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                extractor,
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
    async def test_dry_run_returns_confirmation_required(self, mock_page):
        """send_message with confirm_send=False returns confirmation_required status."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Test User",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_href",
                new_callable=AsyncMock,
                return_value="https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
            ),
            patch.object(
                extractor,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_box",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                extractor,
                "_compose_page_matches_recipient",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_dismiss_message_ui",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=False
            )

        assert result["status"] == "confirmation_required"
        assert result["sent"] is False

    async def test_message_unavailable_when_no_compose_href(self, mock_page):
        """send_message returns message_unavailable when no compose URL found."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Test User",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_href",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "message_unavailable"
        assert result["sent"] is False

    async def test_uses_profile_urn_when_provided(self, mock_page):
        """send_message builds compose URL from profile_urn without Message-button lookup."""
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Test User",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_href",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_resolve_href,
            patch.object(
                extractor,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_box",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                extractor,
                "_compose_page_matches_recipient",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_dismiss_message_ui",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.send_message(
                "testuser",
                "Hello!",
                confirm_send=False,
                profile_urn="ACoAAB1IelEB",
            )

        # _resolve_message_compose_href should NOT be called when profile_urn given
        mock_resolve_href.assert_not_awaited()
        assert result["status"] == "confirmation_required"

    async def test_profile_urn_compose_url_includes_full_params(self, mock_page):
        """send_message with profile_urn builds URL with profileUrn, screenContext, interop."""
        extractor = LinkedInExtractor(mock_page)
        navigate_calls = []

        async def capture_navigate(url):
            navigate_calls.append(url)

        with (
            patch.object(
                extractor,
                "_navigate_to_page",
                new_callable=AsyncMock,
                side_effect=capture_navigate,
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
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Test User",
            ),
            patch.object(
                extractor,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_box",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                extractor,
                "_compose_page_matches_recipient",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_dismiss_message_ui",
                new_callable=AsyncMock,
            ),
        ):
            await extractor.send_message(
                "testuser",
                "Hello!",
                confirm_send=False,
                profile_urn="ACoAAB1IelEB",
            )

        # Second navigate call is the compose URL (first is the profile page)
        compose_url = navigate_calls[1]
        assert "profileUrn=" in compose_url
        assert "urn%3Ali%3Afsd_profile%3AACoAAB1IelEB" in compose_url
        assert "recipient=ACoAAB1IelEB" in compose_url
        assert "screenContext=NON_SELF_PROFILE_VIEW" in compose_url
        assert "interop=msgOverlay" in compose_url


class TestResolveMessageComposeBox:
    async def test_returns_locator_when_count_positive(self, mock_page):
        """_resolve_message_compose_box returns locator.last when count() > 0."""
        extractor = LinkedInExtractor(mock_page)
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=1)
        sentinel = MagicMock(name="last_locator")
        sentinel.wait_for = AsyncMock()
        mock_locator.last = sentinel
        mock_locator.wait_for = AsyncMock()
        mock_page.locator = MagicMock(return_value=mock_locator)

        result = await extractor._resolve_message_compose_box()

        assert result is sentinel
        # wait_for should NOT be called on the early-return path
        sentinel.wait_for.assert_not_called()
        mock_locator.wait_for.assert_not_called()

    async def test_returns_none_when_all_selectors_miss(self, mock_page):
        """_resolve_message_compose_box returns None when no selector matches."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        extractor = LinkedInExtractor(mock_page)
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(return_value=0)
        mock_locator.last = MagicMock()
        mock_locator.last.wait_for = AsyncMock(
            side_effect=PlaywrightTimeoutError("timeout")
        )
        mock_page.locator = MagicMock(return_value=mock_locator)

        result = await extractor._resolve_message_compose_box()

        assert result is None

    async def test_falls_through_when_count_raises(self, mock_page):
        """_resolve_message_compose_box handles count() exceptions gracefully."""
        from patchright.async_api import TimeoutError as PlaywrightTimeoutError

        extractor = LinkedInExtractor(mock_page)
        mock_locator = MagicMock()
        mock_locator.count = AsyncMock(side_effect=Exception("detached"))
        mock_locator.last = MagicMock()
        mock_locator.last.wait_for = AsyncMock(
            side_effect=PlaywrightTimeoutError("timeout")
        )
        mock_page.locator = MagicMock(return_value=mock_locator)

        result = await extractor._resolve_message_compose_box()

        assert result is None


class TestSendMessageComposerInteraction:
    """Tests for the page.evaluate + keyboard.type send path (patchright workaround)."""

    def _patch_send_message_to_compose(self, extractor, mock_page):
        """Return a context manager that patches send_message up to the compose step."""
        return (
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
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
                "_read_profile_display_name",
                new_callable=AsyncMock,
                return_value="Test User",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_href",
                new_callable=AsyncMock,
                return_value="https://www.linkedin.com/messaging/compose/?recipient=ACoAAB",
            ),
            patch.object(
                extractor,
                "_wait_for_message_surface",
                new_callable=AsyncMock,
                return_value="composer",
            ),
            patch.object(
                extractor,
                "_resolve_message_compose_box",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch.object(
                extractor,
                "_compose_page_matches_recipient",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                extractor,
                "_dismiss_message_ui",
                new_callable=AsyncMock,
            ),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        )

    async def test_focus_and_type_via_evaluate_and_keyboard(self, mock_page):
        """send_message uses page.evaluate to focus and page.keyboard.type to type."""
        extractor = LinkedInExtractor(mock_page)
        mock_keyboard = MagicMock()
        mock_keyboard.type = AsyncMock()
        mock_keyboard.press = AsyncMock()
        mock_page.keyboard = mock_keyboard
        # evaluate returns: True (focus), True (send button click)
        mock_page.evaluate = AsyncMock(side_effect=[True, True])
        patches = self._patch_send_message_to_compose(extractor, mock_page)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patch.object(
                extractor,
                "_message_text_occurrences",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                extractor,
                "_message_text_visible",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "sent"
        assert result["sent"] is True
        # Verify keyboard.type was used (not press_sequentially)
        mock_keyboard.type.assert_awaited_once_with("Hello!", delay=15)

    async def test_compose_interact_failed_when_focus_fails(self, mock_page):
        """send_message returns compose_interact_failed when JS focus fails."""
        extractor = LinkedInExtractor(mock_page)
        mock_keyboard = MagicMock()
        mock_keyboard.type = AsyncMock()
        mock_page.keyboard = mock_keyboard
        # evaluate returns False (focus failed)
        mock_page.evaluate = AsyncMock(return_value=False)
        patches = self._patch_send_message_to_compose(extractor, mock_page)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "compose_interact_failed"
        assert result["sent"] is False

    async def test_enter_fallback_when_send_button_not_found(self, mock_page):
        """send_message falls back to Enter key when JS cannot find send button."""
        extractor = LinkedInExtractor(mock_page)
        mock_keyboard = MagicMock()
        mock_keyboard.type = AsyncMock()
        mock_keyboard.press = AsyncMock()
        mock_page.keyboard = mock_keyboard
        # evaluate returns: True (focus), False (no send button found)
        mock_page.evaluate = AsyncMock(side_effect=[True, False])
        patches = self._patch_send_message_to_compose(extractor, mock_page)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patch.object(
                extractor,
                "_message_text_occurrences",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                extractor,
                "_message_text_visible",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "sent"
        # Enter was pressed as fallback
        mock_keyboard.press.assert_awaited_once_with("Enter")

    async def test_baseline_is_taken_after_typing_and_before_send(self, mock_page):
        """The occurrence baseline is captured between typing and the click.

        Taken any earlier it would miss what the composer already holds; taken
        after the click it could already include the delivered message, and
        the confirmation would compare that copy against itself.
        """
        extractor = LinkedInExtractor(mock_page)
        steps: list[str] = []
        mock_keyboard = MagicMock()
        mock_keyboard.type = AsyncMock(
            side_effect=lambda *args, **kwargs: steps.append("type")
        )
        mock_keyboard.press = AsyncMock()
        mock_page.keyboard = mock_keyboard

        async def evaluate(*args, **kwargs):
            steps.append("click" if steps else "focus")
            return True

        async def occurrences(message):
            steps.append("baseline")
            return 2

        async def visible(message, *, previous_occurrences):
            steps.append(f"confirm:{previous_occurrences}")
            return True

        mock_page.evaluate = AsyncMock(side_effect=evaluate)
        patches = self._patch_send_message_to_compose(extractor, mock_page)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patch.object(
                extractor,
                "_message_text_occurrences",
                new_callable=AsyncMock,
                side_effect=occurrences,
            ),
            patch.object(
                extractor,
                "_message_text_visible",
                new_callable=AsyncMock,
                side_effect=visible,
            ),
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "sent"
        # The confirmation receives exactly the baseline taken before the click.
        assert steps == ["focus", "type", "baseline", "click", "confirm:2"]

    async def test_send_unavailable_when_click_adds_nothing(self, mock_page):
        """A clicked Send button that changes nothing is not a sent message."""
        extractor = LinkedInExtractor(mock_page)
        mock_keyboard = MagicMock()
        mock_keyboard.type = AsyncMock()
        mock_keyboard.press = AsyncMock()
        mock_page.keyboard = mock_keyboard
        # evaluate returns: True (focus), True (send button found and clicked)
        mock_page.evaluate = AsyncMock(side_effect=[True, True])
        patches = self._patch_send_message_to_compose(extractor, mock_page)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patch.object(
                extractor,
                "_message_text_occurrences",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch.object(
                extractor,
                "_message_text_visible",
                new_callable=AsyncMock,
                return_value=False,
            ) as visible,
        ):
            result = await extractor.send_message(
                "testuser", "Hello!", confirm_send=True
            )

        assert result["status"] == "send_unavailable"
        assert result["sent"] is False
        visible.assert_awaited_once_with("Hello!", previous_occurrences=1)


class TestMessageTextOccurrences:
    """Tests for the occurrence-based send confirmation (issue #866)."""

    async def test_occurrences_run_the_shared_counting_routine(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.evaluate = AsyncMock(return_value=3)

        assert await extractor._message_text_occurrences("Hello!") == 3

        mock_page.evaluate.assert_awaited_once_with(
            _MESSAGE_OCCURRENCES_JS, {"expected": "Hello!"}
        )

    async def test_missing_count_reads_as_zero(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.evaluate = AsyncMock(return_value=None)

        assert await extractor._message_text_occurrences("Hello!") == 0

    async def test_confirmation_waits_for_more_than_the_baseline(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(return_value=None)

        assert (
            await extractor._message_text_visible("Hello!", previous_occurrences=2)
            is True
        )

        mock_page.wait_for_function.assert_awaited_once_with(
            _MESSAGE_OCCURRENCES_INCREASED_JS,
            arg={"expected": "Hello!", "previous": 2},
        )
        # Baseline and confirmation run one routine, so they cannot disagree
        # about what counts as an occurrence.
        assert _MESSAGE_OCCURRENCES_JS in _MESSAGE_OCCURRENCES_INCREASED_JS

    async def test_confirmation_timeout_is_not_a_send(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=PlaywrightTimeoutError("timeout")
        )

        assert (
            await extractor._message_text_visible("Hello!", previous_occurrences=0)
            is False
        )

    async def test_other_confirmation_errors_do_not_confirm(self, mock_page):
        """A broken confirmation surfaces as an error, never as a send."""
        extractor = LinkedInExtractor(mock_page)
        mock_page.wait_for_function = AsyncMock(
            side_effect=PatchrightError("execution context destroyed")
        )

        with pytest.raises(PatchrightError):
            await extractor._message_text_visible("Hello!", previous_occurrences=0)


class TestBuildFeedReferences:
    """Tests for _build_feed_references SDUI-capture / DOM-anchor merging."""

    def test_sdui_urls_become_relative_feed_post_references(self):
        captured = [
            "https://www.linkedin.com/posts/alice_some-slug-ugcPost-1-xx",
            "https://www.linkedin.com/posts/bob_other-post-share-2-yy",
        ]
        refs = _build_feed_references([], captured)
        assert refs == [
            {
                "kind": "feed_post",
                "url": "/posts/alice_some-slug-ugcPost-1-xx",
                "context": "feed",
            },
            {
                "kind": "feed_post",
                "url": "/posts/bob_other-post-share-2-yy",
                "context": "feed",
            },
        ]

    def test_duplicate_sdui_urls_are_deduped(self):
        captured = [
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
        ]
        refs = _build_feed_references([], captured)
        assert len(refs) == 1
        assert refs[0]["url"] == "/posts/alice_x-ugcPost-1-xx"

    def test_dom_anchor_feed_update_passes_through(self):
        # DOM anchors that classify_link recognises as feed_post survive
        # the merge alongside SDUI captures.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:1234567890/",
                "text": "View post",
            }
        ]
        refs = _build_feed_references(raw_anchors, [])
        assert any(
            r["url"] == "/feed/update/urn:li:activity:1234567890/"
            and r["kind"] == "feed_post"
            for r in refs
        )

    def test_non_posts_paths_in_sdui_capture_are_skipped(self):
        # Defensive: only /posts/<slug> shapes count for SDUI append.
        captured = [
            "https://www.linkedin.com/in/someuser/",
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
        ]
        refs = _build_feed_references([], captured)
        assert [r["url"] for r in refs] == ["/posts/alice_x-ugcPost-1-xx"]

    def test_cap_matches_num_posts_ceiling(self):
        captured = [
            f"https://www.linkedin.com/posts/p{i}-ugcPost-{i}-xx" for i in range(60)
        ]
        refs = _build_feed_references([], captured)
        # Cap is 50, mirroring _REFERENCE_CAPS["feed"] / num_posts <= 50.
        assert len(refs) == 50

    def test_non_feed_post_dom_anchors_are_filtered(self):
        # Sidebar profile / company / external anchors must not crowd
        # out SDUI permalinks — references["feed"] is feed_post-only.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/in/sidebar-user/",
                "text": "Sidebar User",
            },
            {
                "href": "https://www.linkedin.com/company/some-corp/",
                "text": "Some Corp",
            },
            {
                "href": "https://example.com/external/",
                "text": "External Link",
            },
        ]
        refs = _build_feed_references(raw_anchors, [])
        assert refs == []

    def test_feed_post_dom_anchors_coexist_with_sdui_captures(self):
        # The two sources fold into the same feed_post kind without
        # collapsing across URL shapes pointing at the same post.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:111/",
                "text": "View post",
            }
        ]
        captured = ["https://www.linkedin.com/posts/alice_x-ugcPost-1-xx"]
        refs = _build_feed_references(raw_anchors, captured)
        urls = [r["url"] for r in refs]
        kinds = {r["kind"] for r in refs}
        assert urls == [
            "/feed/update/urn:li:activity:111/",
            "/posts/alice_x-ugcPost-1-xx",
        ]
        assert kinds == {"feed_post"}


class TestProxyNavigationFailures:
    """A proxy outage during an ordinary tool call is reported as itself."""

    async def test_proxy_error_is_raised_instead_of_a_scraping_failure(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(
            side_effect=Exception("net::ERR_PROXY_CONNECTION_FAILED at …")
        )

        with pytest.raises(ProxyConnectionError):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

    async def test_proxy_error_is_converted_before_it_reaches_a_trace(self, mock_page):
        # The trace records the raw exception text, which for a proxy failure
        # can quote the proxy URL and put a password into trace.jsonl.
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(
            side_effect=Exception("net::ERR_TUNNEL_CONNECTION_FAILED")
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.record_page_trace",
                new_callable=AsyncMock,
            ) as mock_trace,
            pytest.raises(ProxyConnectionError),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        recorded = [call.args[1] for call in mock_trace.await_args_list]
        assert "extractor-navigation-error" not in recorded

    async def test_ordinary_navigation_failure_is_unaffected(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(side_effect=Exception("net::ERR_ABORTED"))

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(Exception) as excinfo,
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert not isinstance(excinfo.value, ProxyConnectionError)


class TestNavigationFailureLogRedaction:
    """The navigation-failure log must not carry proxy credentials.

    It reaches the log even for errors the marker check does not recognise as
    proxy faults, and that log is what users paste into issue reports.
    """

    async def test_credentials_are_redacted_from_the_log(
        self, mock_page, monkeypatch, caplog
    ):
        import logging

        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = "acctzone9"
        config.browser.proxy_password = "s3cr3t"
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

        extractor = LinkedInExtractor(mock_page)
        # No proxy marker, so it is not converted and reaches the logger.
        mock_page.goto = AsyncMock(
            side_effect=Exception(
                "failed via http://acctzone9:s3cr3t@gate.example:7000"
            )
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            caplog.at_level(logging.WARNING),
            pytest.raises(Exception),
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert "s3cr3t" not in caplog.text
        assert "acctzone9" not in caplog.text


class TestNavigationFailureCrossesTheToolBoundaryClean:
    """The re-raised exception itself must be credential-free.

    Redacting the extractor's own trace and log is not enough: everything
    downstream logs the exception too, starting with the catch-all in
    error_handler and FastMCP's handler above it.
    """

    async def test_reraised_exception_carries_no_credentials(
        self, mock_page, monkeypatch
    ):
        from linkedin_mcp_server.config.schema import AppConfig

        config = AppConfig()
        config.browser.proxy_server = "http://gate.example:7000"
        config.browser.proxy_username = "acctzone9"
        config.browser.proxy_password = "s3cr3t"
        monkeypatch.setattr("linkedin_mcp_server.config.get_config", lambda: config)

        extractor = LinkedInExtractor(mock_page)
        mock_page.goto = AsyncMock(
            side_effect=Exception(
                "failed via http://acctzone9:s3cr3t@gate.example:7000"
            )
        )

        with (
            patch(
                "linkedin_mcp_server.scraping.extractor.resolve_remember_me_prompt",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(Exception) as excinfo,
        ):
            await extractor._goto_with_auth_checks(
                "https://www.linkedin.com/in/testuser/"
            )

        assert "s3cr3t" not in str(excinfo.value)
        assert "acctzone9" not in str(excinfo.value)
        # The raw error must not survive as a cause either: the handlers
        # downstream print the whole chain.
        assert excinfo.value.__cause__ is None


def _no_signals() -> ActionSignals:
    """Every structural signal absent, which is all these tests need."""
    return ActionSignals(
        has_invite_anchor=False,
        has_compose_anchor_in_action_root=False,
        has_edit_intro_anchor=False,
        has_labeled_action_button=False,
        has_labeled_action_anchor=False,
        has_incoming_action_row=False,
    )


class TestGetMyProfileAlias:
    async def test_survives_a_redirect_that_never_resolves_the_alias(self, mock_page):
        """The one caller allowed to hold "me".

        get_my_profile navigates to /in/me/ and reads the identifier back out of
        the redirect. When the redirect has not happened it still holds the
        alias, and refusing there would answer the tool that owns the alias with
        an instruction to call itself.
        """
        mock_page.url = "https://www.linkedin.com/in/me/"
        extractor = LinkedInExtractor(mock_page)
        with (
            patch.object(
                extractor,
                "extract_page",
                new_callable=AsyncMock,
                return_value=extracted("profile text"),
            ) as mock_extract,
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
            patch(
                "linkedin_mcp_server.scraping.extractor.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await extractor.get_my_profile()

        # The alias survives normalization, and because the page is already on
        # it, the scrape reuses the loaded document instead of navigating again.
        assert result["url"] == "https://www.linkedin.com/in/me/"
        assert "main_profile" in result["sections"]
        mock_extract.assert_not_called()

    async def test_refuses_the_alias_from_an_ordinary_caller(self, mock_page):
        extractor = LinkedInExtractor(mock_page)
        with patch.object(
            extractor, "extract_page", new_callable=AsyncMock
        ) as mock_extract:
            with pytest.raises(InvalidReferenceError):
                await extractor.scrape_person("me", {"main_profile"})
        mock_extract.assert_not_called()


class TestEveryNormalizedEntryPoint:
    """Each method that was rewired, refusing a value that redirects the path.

    Without this, removing normalization from one method leaves every other test
    untouched: the bare-identifier assertions build the same URL either way. The
    traversal value is the one input whose result differs, and it has to fail
    before any navigation rather than after one.
    """

    @staticmethod
    def _calls(extractor: LinkedInExtractor):
        return (
            patch.object(extractor, "extract_page", new_callable=AsyncMock),
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
        )

    async def test_connect_with_person_normalizes_before_its_own_downstream_use(
        self, mock_page
    ):
        """The traversal case cannot see this one.

        scrape_person normalizes too, so removing connect_with_person's own call
        still raises on "../../feed". A full URL is what separates them: the
        scrape would succeed while the invite deeplink and the action-signal
        selectors kept receiving the URL where they expect the vanity.
        """
        extractor = LinkedInExtractor(mock_page)
        seen: list[str] = []
        with (
            patch.object(
                extractor,
                "scrape_person",
                new_callable=AsyncMock,
                return_value={"sections": {"main_profile": "text"}},
            ),
            patch.object(
                extractor,
                "_read_action_signals",
                new_callable=AsyncMock,
                side_effect=lambda username: seen.append(username) or _no_signals(),
            ),
            patch.object(extractor, "_navigate_to_page", new_callable=AsyncMock),
        ):
            await extractor.connect_with_person(
                "https://de.linkedin.com/in/williamhgates"
            )

        assert seen == ["williamhgates"]

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
