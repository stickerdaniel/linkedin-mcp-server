"""Tests for the job posting, job search and saved-job list owner."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import asyncio

import pytest
from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.scraping import jobs as jobs_module
from linkedin_mcp_server.scraping.capture import CapturePlan, SectionCapture
from linkedin_mcp_server.scraping.content import PageContentReader
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.scraping.job_pages import JobPageCapture, JobPageReader
from linkedin_mcp_server.scraping.jobs import JobScraper
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession
from scraping.support.navigation import navigate


def _scraper(page) -> JobScraper:
    """Wire the job owner the way the facade does."""
    session = ScrapingSession(page)
    navigator = PageNavigator(session)
    content = PageContentReader(session)
    return JobScraper(
        navigator,
        SectionCapture(session, navigator, content),
        JobPageReader(session, navigator, content),
    )


def extracted(
    text: str,
    references: list[Reference] | None = None,
    error: dict | None = None,
) -> ExtractedSection:
    """Create an ExtractedSection for tests."""
    return ExtractedSection(text=text, references=references or [], error=error)


def captured(
    page,
    section: ExtractedSection,
    *,
    scroll_seconds: float = 0.0,
) -> JobPageCapture:
    """One page read, landing wherever the page double already sits.

    Built against the page rather than a literal address: the offset and route
    guards read the capture, so a fixed URL here would answer them with the
    test's own assumption instead of with what the navigation did.
    """
    return JobPageCapture(
        section=section,
        landed_url=page.url,
        scroll_seconds=scroll_seconds,
    )


class TestScrapeJob:
    async def test_scrape_job(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted("Job: Software Engineer"),
        ) as capture:
            result = await scraper.scrape_job("12345")

        capture.assert_awaited_once_with(
            "https://www.linkedin.com/jobs/view/12345/",
            section_name="job_posting",
            plan=CapturePlan(),
        )
        assert result["url"] == "https://www.linkedin.com/jobs/view/12345/"
        assert "job_posting" in result["sections"]
        assert "pages_visited" not in result
        assert "sections_requested" not in result

    async def test_scrape_job_omits_rate_limited_sentinel(self, mock_page):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(RATE_LIMITED_SECTION_TEXT),
        ):
            result = await scraper.scrape_job("12345")

        assert result["sections"] == {}
        assert result["section_errors"]["job_posting"]["error_type"] == "rate_limit"

    async def test_scrape_job_omits_orphaned_references_when_text_empty(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
        with patch.object(
            scraper._capture,
            "capture",
            new_callable=AsyncMock,
            return_value=extracted(
                "",
                [{"kind": "job", "url": "/jobs/view/12345/", "text": "Engineer"}],
            ),
        ):
            result = await scraper.scrape_job("12345")

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
            section = texts(url) if supply is None else next(supply)
            # Sealed after the navigation, the way the reader seals it: a
            # capture built from the address the test set up beforehand would
            # answer the route and offset guards with the test's own
            # assumption.
            return captured(mock_page, section)

        return navigate_page

    async def test_returns_job_ids(self, mock_page):
        """search_jobs should return a job_ids list extracted from hrefs."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Job 1\nJob 2\nJob 3")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222", "333"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111", "222", "333"]
        assert "search_results" in result["sections"]

    async def test_returns_references(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(
                    mock_page,
                    extracted(
                        "Job 1",
                        [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}],
                    ),
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["references"] == {
            "search_results": [
                {"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}
            ]
        }

    async def test_componentkey_jobs_without_anchors_get_fallback_references(
        self, mock_page
    ):
        scraper = _scraper(mock_page)
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
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [page]),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["222", "111"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

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
        scraper = _scraper(mock_page)
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
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                return_value=raw_page,
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222", "111", "333"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
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
            result = await scraper.search_jobs("python", max_pages=1)

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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Job 1")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["job_ids"] == ["111"]

    async def test_a_foreign_host_still_skips_job_ids(self, mock_page):
        """Only the path is normalized; the host still has to be LinkedIn.

        Comparing paths alone would accept any origin serving a
        `/jobs/search` path, which is what an interstitial or a proxied error
        page can look like.
        """
        mock_page.url = "https://example.com/jobs/search?keywords=python"
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Job 1")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ) as ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        ids.assert_not_called()

    async def test_pagination_follows_what_the_page_rendered(self, mock_page):
        """&start= advances by the cards found, not by LinkedIn's stride.

        A live search rendered 11 cards per navigation while advertising 25
        per page, so a fixed stride skipped 13 of every 24 jobs.
        """
        scraper = _scraper(mock_page)
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
            patch.object(
                scraper._pages, "_extract_search_page", side_effect=mock_extract
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=2)

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
        scraper = _scraper(mock_page)
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
                PageContentReader,
                "_extract_root_content",
                new_callable=AsyncMock,
                side_effect=raw_pages,
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=id_pages,
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
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
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=2)

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
        scraper = _scraper(mock_page)
        id_pages = iter([["100", "200"], ["200", "300"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [extracted("text")] * 2),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["100", "200", "300"]
        assert mock_extract.await_count == 2

    async def test_a_dropped_location_is_reported_and_the_results_kept(self, mock_page):
        """A filter LinkedIn drops costs relevance, not correctness.

        The results are still about the keywords that were asked for, only
        broader, so stopping would return nothing where something useful is
        in hand. Saying nothing is the part that cannot be defended: a search
        for Python in Berlin comes back as Python anywhere and reads as
        though Berlin had none.
        """
        scraper = _scraper(mock_page)

        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("python jobs")],
                    lands_on=("https://www.linkedin.com/jobs/search/?keywords=python"),
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["901"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", location="Berlin", max_pages=1)

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
        scraper = _scraper(mock_page)
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
            return captured(mock_page, next(pages))

        with (
            patch.object(scraper._pages, "_extract_search_page", side_effect=land),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["901"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", location="Berlin", max_pages=2)

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
        scraper = _scraper(mock_page)

        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("recommended for you")],
                    lands_on="https://www.linkedin.com/jobs/search/",
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["901"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", location="Berlin")

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
        scraper = _scraper(mock_page)

        with (
            patch.object(
                scraper._pages,
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
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

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
        scraper = _scraper(mock_page)

        with (
            patch.object(
                scraper._pages,
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
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", location="Berlin", max_pages=1)

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
        scraper = _scraper(mock_page)
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
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [page]),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        # The exclusion is the rail scope, and the double answers the same
        # ids either way, so asking for it is the only thing that can fail
        # when the search stops scoping the read.
        mock_ids.assert_awaited_once_with(scoped=True)
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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("the first page")] * 3,
                    lands_on="https://www.linkedin.com/jobs/search/?keywords=python",
                ),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["101", "102"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=3)

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
        scraper = _scraper(mock_page)
        id_pages = iter([["100", "200"], ["100", "200"]])
        extract_call_count = 0

        navigate_page = self._navigating(mock_page, lambda _url: None)

        async def mock_extract(url, *args, **kwargs):
            nonlocal extract_call_count
            await navigate_page(url)
            extract_call_count += 1
            if extract_call_count == 1:
                return captured(
                    mock_page,
                    extracted(
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
                    ),
                )
            return captured(
                mock_page,
                extracted(
                    "text",
                    [
                        {
                            "kind": "job",
                            "url": "/jobs/view/200/",
                            "text": "Senior Software Engineer",
                            "context": "job result",
                        }
                    ],
                ),
            )

        with (
            patch.object(
                scraper._pages, "_extract_search_page", side_effect=mock_extract
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=5)

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
        scraper = _scraper(mock_page)
        # One advertised page is 25 results and the first navigation renders
        # exactly 25, which is the boundary: the offset reaches the end
        # without passing it. Rendering more would clear `>=` and `>` alike
        # and leave the comparison untested.
        id_pages = iter([[str(i) for i in range(25)], ["900"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("text")),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_total_pages,
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=10)

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
        scraper = _scraper(mock_page)
        seen: list[float | None] = []

        async def read_page(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            # A real page reports what its scroll spent, and only that. Twelve
            # seconds of navigation with no scrolling would leave the budget
            # untouched, which is the case this replaced.
            clock.now += 12.0
            return captured(mock_page, extracted("Job results"), scroll_seconds=12.0)

        async def sleep(seconds: float) -> None:
            clock.now += seconds

        # Fresh ids every call, or the search stops after two navigations and
        # the budget is never spent over the ten this is named for.
        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(jobs_module, "time", clock),
            patch.object(scraper._pages, "_extract_search_page", side_effect=read_page),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            await scraper.search_jobs("python", max_pages=10, tool_timeout=100000)

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
        scraper = _scraper(mock_page)
        seen: list[float | None] = []

        async def read_page(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            # All navigation, no scrolling.
            clock.now += 12.0
            return captured(mock_page, extracted("Job results"))

        async def sleep(seconds: float) -> None:
            clock.now += seconds

        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(jobs_module, "time", clock),
            patch.object(scraper._pages, "_extract_search_page", side_effect=read_page),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            await scraper.search_jobs("python", max_pages=10, tool_timeout=100000)

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
        scraper = _scraper(mock_page)
        seen: list[float | None] = []

        async def read_page(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            clock.now += 18.7
            return captured(mock_page, extracted("Job results"))

        async def sleep(seconds: float) -> None:
            """The inter-page delay costs wall clock, the same as a navigation."""
            clock.now += seconds

        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(jobs_module, "time", clock),
            patch.object(scraper._pages, "_extract_search_page", side_effect=read_page),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=10)

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
        scraper = _scraper(mock_page)
        seen: list[float | None] = []

        async def read_page(url, section_name, scroll_deadline=None, **kwargs):
            seen.append(scroll_deadline)
            navigate(mock_page, url)
            clock.now += 18.7
            return captured(mock_page, extracted("Job results"))

        async def sleep(seconds: float) -> None:
            clock.now += seconds

        pages = [[str(100 + p * 10 + i) for i in range(10)] for p in range(10)]

        with (
            patch.object(jobs_module, "time", clock),
            patch.object(scraper._pages, "_extract_search_page", side_effect=read_page),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=pages,
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                side_effect=sleep,
            ),
        ):
            # 176.25 * _SEARCH_TIMEOUT_FRACTION is a 141s budget.
            result = await scraper.search_jobs(
                "python", max_pages=10, tool_timeout=176.25
            )

        assert len(seen) == 6
        assert result["job_ids"] == [jid for page in pages[:6] for jid in page]

    async def test_zero_max_pages_fetches_nothing(self, mock_page):
        """max_pages=0 should fetch zero pages (validation at tool boundary)."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("text")),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=0)

        assert result["job_ids"] == []
        assert mock_extract.await_count == 0

    async def test_single_page(self, mock_page):
        """max_pages=1 should only visit one page; filters appear in URL."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Job posting text")),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["42"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs(
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
        scraper = _scraper(mock_page)
        text_pages = iter(["Page 1 content", "Page 2 content"])
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                side_effect=self._navigating(
                    mock_page, lambda _url: extracted(next(text_pages))
                ),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=2)

        assert "\n---\n" in result["sections"]["search_results"]
        assert "Page 1 content" in result["sections"]["search_results"]
        assert "Page 2 content" in result["sections"]["search_results"]
        assert mock_extract.await_count == 2

    async def test_empty_results(self, mock_page):
        """Should handle empty results gracefully and skip ID extraction."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [extracted("")]),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("nonexistent_xyz")

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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("")],
                    lands_on="https://www.linkedin.com/jobs/search-results/",
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"] == {}
        assert result["section_errors"]["search_results"]["error_type"] == (
            "search_replaced"
        )
        mock_ids.assert_not_awaited()

    async def test_empty_redesign_page_reports_a_dropped_filter(self, mock_page):
        """A clean empty result cannot hide a location the redirect dropped."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
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
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", location="Berlin", max_pages=1)

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
        scraper = _scraper(mock_page)
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
            return captured(mock_page, next(pages))

        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=navigate_page,
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=2)

        assert result["job_ids"] == ["111"]
        assert result["sections"]["search_results"] == "Page 1"
        error = result["section_errors"]["search_results"]
        assert error["error_type"] == "pagination_stopped"
        assert mock_ids.await_count == 1

    async def test_no_ids_on_first_page_captures_text(self, mock_page):
        """Non-empty text with zero job IDs should be returned in sections."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                # Navigating, or the page keeps the fixture's `keywords=python`
                # while the search asks for something else, and the check that
                # the answer is about the question stops the loop.
                side_effect=self._navigating(
                    mock_page, [extracted("No matching jobs found")]
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("xyzzy123", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"]["search_results"] == "No matching jobs found"

    async def test_a_login_redirect_raises_an_auth_error(self, mock_page):
        """A login wall reached mid-search is an expired session.

        Its text used to come back under `search_results`, with the login
        page's own references beside it, so the caller could not tell it from
        a search that found those words. A section error is not enough
        either: only the auth error starts the relogin the tool has.
        """
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/uas/login"
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(
                    mock_page,
                    extracted(
                        "Login page content",
                        [
                            {
                                "kind": "person",
                                "url": "/in/testuser/",
                                "text": "Test User",
                            }
                        ],
                    ),
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(AuthenticationError, match="--login"):
                await scraper.search_jobs("python", max_pages=2)

        mock_ids.assert_not_awaited()

    async def test_a_plain_redirect_is_reported_not_returned(self, mock_page):
        """Anything else that is not the search page is dropped and diagnosed.

        Keeping the landing page's text and references handed a page that is
        not the search back under `search_results`, carrying whatever links
        it held. An empty result with nothing beside it is not an option
        either: that is what an exhausted search looks like.
        """
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/feed/"
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(
                    mock_page,
                    extracted(
                        "Feed content",
                        [
                            {
                                "kind": "person",
                                "url": "/in/testuser/",
                                "text": "Test User",
                            }
                        ],
                    ),
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=2)

        mock_ids.assert_not_awaited()
        assert result["job_ids"] == []
        assert "search_results" not in result["sections"]
        assert "references" not in result
        assert "search_results" in result["section_errors"]

    async def test_rate_limited_skips_ids_and_text(self, mock_page):
        """Rate-limited pages should yield no IDs or text."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted(RATE_LIMITED_SECTION_TEXT)),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["job_ids"] == []
        assert result["sections"] == {}
        assert result["section_errors"]["search_results"]["error_type"] == "rate_limit"
        mock_ids.assert_not_awaited()

    async def test_rate_limit_wins_over_an_unexpected_landing(self, mock_page):
        """The specific diagnosis survives a simultaneous route failure."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted(RATE_LIMITED_SECTION_TEXT)],
                    lands_on="https://www.linkedin.com/feed/",
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("", error=failure)],
                    lands_on="https://www.linkedin.com/jobs/search-results/",
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        assert result["section_errors"]["search_results"] == failure
        mock_ids.assert_not_awaited()

    async def test_every_returned_id_carries_exactly_one_job_reference(self, mock_page):
        """One canonical reference per id, whichever way the DOM described it.

        The rail names four jobs. Two arrive with anchors, one of those twice
        under different labels, and two arrive with none at all. The answer
        has to be four job references in rail order, so a lost synthesis, a
        surviving duplicate or a pane job leaking in all show up here rather
        than as a reference count that happens to match.
        """
        scraper = _scraper(mock_page)
        page = extracted(
            "Job results",
            [
                {"kind": "job", "url": "/jobs/view/222/", "text": "Two"},
                {"kind": "job", "url": "/jobs/view/111/", "text": "One"},
                {"kind": "job", "url": "/jobs/view/222/", "text": "Two again"},
                {"kind": "job", "url": "/jobs/view/999/", "text": "Detail pane"},
            ],
        )
        with (
            patch.object(
                scraper._pages,
                "_extract_search_page",
                side_effect=self._navigating(mock_page, [page]),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222", "333", "444"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_search_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.search_jobs("python", max_pages=1)

        references = result["references"]["search_results"]
        jobs = [reference for reference in references if reference["kind"] == "job"]
        assert result["job_ids"] == ["111", "222", "333", "444"]
        assert [reference["url"] for reference in jobs] == [
            "/jobs/view/222/",
            "/jobs/view/111/",
            "/jobs/view/333/",
            "/jobs/view/444/",
        ]
        # The first anchor for an id is the one kept. Asserted on the label
        # because the final dedupe would collapse a second emission anyway,
        # and collapsing prefers the *longer* text: without the per-page
        # guard this reads "Two again" while the count still says four.
        assert jobs[0]["text"] == "Two"
        assert jobs[2] == {"kind": "job", "url": "/jobs/view/333/"}


class TestGetSavedJobs:
    """Tests for get_saved_jobs with job ID extraction and pagination."""

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
            return captured(mock_page, next(supply))

        return navigate

    async def test_returns_job_ids(self, mock_page):
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Saved Job 1\nSaved Job 2")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111", "222"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=1)

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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Captive portal")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["999"],
            ) as ids,
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == []
        assert "saved_jobs" not in result["sections"]
        assert "references" not in result
        assert "saved_jobs" in result["section_errors"]
        ids.assert_not_called()

    async def test_returns_references(self, mock_page):
        """References are keyed by the section name, per the return contract."""
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(
                    mock_page,
                    extracted(
                        "Job 1",
                        [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}],
                    ),
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=1)

        assert result["references"] == {
            "saved_jobs": [{"kind": "job", "url": "/jobs/view/111/", "text": "Job 1"}]
        }

    async def test_page_texts_joined_with_separator(self, mock_page):
        """Multi-page text is joined so the caller can tell pages apart."""
        scraper = _scraper(mock_page)
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(
                    mock_page, [extracted("page one"), extracted("page two")]
                ),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=2,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=2)

        assert result["sections"]["saved_jobs"] == "page one\n---\npage two"

    async def test_pagination_uses_start_offset(self, mock_page):
        """The my-items list pages in 10s, not the 25 used by job search."""
        scraper = _scraper(mock_page)
        id_pages = iter([["100", "200"], ["300"], ["400"]])
        urls_visited: list[str] = []

        navigate = self._navigating(mock_page, [extracted("page text")] * 3)

        async def mock_extract(url, *args, **kwargs):
            urls_visited.append(url)
            return await navigate(url)

        with (
            patch.object(
                scraper._pages, "_extract_saved_jobs_page", side_effect=mock_extract
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=3)

        assert result["job_ids"] == ["100", "200", "300", "400"]
        assert urls_visited == [
            "https://www.linkedin.com/my-items/saved-jobs/",
            "https://www.linkedin.com/my-items/saved-jobs/?start=10",
            "https://www.linkedin.com/my-items/saved-jobs/?start=20",
        ]

    async def test_early_stop_no_new_ids(self, mock_page):
        scraper = _scraper(mock_page)
        id_pages = iter([["100"], ["100"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(mock_page, [extracted("text")] * 2),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=5)

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
        scraper = _scraper(mock_page)
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
                "linkedin_mcp_server.scraping.navigation.detect_auth_barrier",
                new_callable=AsyncMock,
                return_value="account picker: #rememberme-div",
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await scraper.get_saved_jobs(max_pages=1)

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

        scraper = _scraper(mock_page)
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
                side_effect=redirect,
            ),
            pytest.raises(AuthenticationError, match="--login"),
        ):
            await scraper.get_saved_jobs(max_pages=1)

    async def test_a_blank_foreign_page_is_not_an_empty_list(self, mock_page):
        """An empty page returned before the route is judged says nothing.

        A captive portal or interstitial that renders no text broke the loop
        ahead of the guard, so the call came back with no sections, no ids and
        no `section_errors`, which is exactly what an account with nothing
        saved looks like.
        """
        mock_page.url = "https://interstitial.example/blank"
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("")),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == []
        assert "saved_jobs" in result["section_errors"]

    async def test_an_empty_list_is_still_an_empty_list(self, mock_page):
        """An account with nothing saved renders nothing, and that is not an error."""
        mock_page.url = "https://www.linkedin.com/jobs-tracker/"
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("")),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=1)

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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(
                    mock_page,
                    [extracted("the list")] * 3,
                    lands_on="https://www.linkedin.com/jobs-tracker/",
                ),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100", "200"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=3)

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
        scraper = _scraper(mock_page)
        id_pages = iter([["100"], ["200"], ["300"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(mock_page, [extracted("text")] * 3),
            ) as mock_extract,
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_total_pages,
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=10)

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
        scraper = _scraper(mock_page)
        pages = iter([extracted("first page"), extracted(RATE_LIMITED_SECTION_TEXT)])
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                side_effect=lambda *a, **kw: captured(mock_page, next(pages)),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=3)

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
        scraper = _scraper(mock_page)
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Saved Job 1")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["111"],
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=1)

        assert result["job_ids"] == ["111"]
        assert result["sections"]["saved_jobs"] == "Saved Job 1"

    async def test_a_login_redirect_raises_an_auth_error(self, mock_page):
        """A redirect to the login wall is an expired session, not a result.

        Mirrors ``search_jobs``. Returning the login page's text under
        `saved_jobs` left the dead browser registered and offered no
        relogin, so the next call walked into the same wall.
        """
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/uas/login"
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Login page content")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["999"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(AuthenticationError, match="--login"):
                await scraper.get_saved_jobs(max_pages=2)

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
        scraper = _scraper(mock_page)
        mock_page.url = "https://www.linkedin.com/feed/"
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                new_callable=AsyncMock,
                return_value=captured(mock_page, extracted("Some other page")),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["999"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=2)

        mock_ids.assert_not_awaited()
        assert result["job_ids"] == []
        assert "saved_jobs" not in result["sections"]
        assert "references" not in result
        assert "saved_jobs" in result["section_errors"]

    async def test_a_page_count_read_that_moves_the_address_stops_the_list(
        self, mock_page
    ):
        """The offset is judged after the page-count read, not before it.

        The count is a DOM read, and the page under it can move while it
        happens: this list is the one LinkedIn redirects, and the redirect
        rewrites the query. The capture predates that, so an offset judged
        from `landed_url` reads the address the extraction saw and accepts a
        page served at some other offset, whose ids then join the list under
        the caller's own `start`.

        Only the order is under test here. The page lands exactly where it
        was asked to, and nothing but the count read moves it, so a check
        taken before that read passes and one taken after it stops.
        """
        scraper = _scraper(mock_page)

        async def read_page(url, *args, **kwargs):
            mock_page.url = url
            return captured(mock_page, extracted("first page"))

        async def count_pages_and_drift():
            mock_page.url = "https://www.linkedin.com/jobs-tracker/?start=10"
            return None

        with (
            patch.object(
                scraper._pages, "_extract_saved_jobs_page", side_effect=read_page
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                return_value=["100"],
            ) as mock_ids,
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                side_effect=count_pages_and_drift,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=2)

        mock_ids.assert_not_awaited()
        assert result["job_ids"] == []
        assert result["sections"] == {}
        assert (
            result["section_errors"]["saved_jobs"]["error_type"] == "pagination_stopped"
        )

    async def test_saved_references_are_capped_at_fifteen_across_pages(self, mock_page):
        """Two pages of twelve unique jobs each, and fifteen come back.

        The per-page cap is the page reader's and is proved there; this is
        the cap over the joined list, which is the only thing standing
        between a ten-page walk and a hundred references in one section.
        """
        scraper = _scraper(mock_page)
        pages = [
            extracted(
                f"page {page}",
                [
                    {
                        "kind": "job",
                        "url": f"/jobs/view/{page * 100 + index}/",
                        "text": f"Job {page}-{index}",
                    }
                    for index in range(12)
                ],
            )
            for page in (1, 2)
        ]
        id_pages = iter(
            [
                [str(100 + index) for index in range(12)],
                [str(200 + index) for index in range(12)],
            ]
        )
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(mock_page, pages),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda *a, **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=2)

        references = result["references"]["saved_jobs"]
        assert len(references) == 15
        assert references[0]["url"] == "/jobs/view/100/"
        assert references[-1]["url"] == "/jobs/view/202/"

    async def test_a_later_page_upgrades_a_bare_saved_reference(self, mock_page):
        """The same job twice keeps the richer of the two descriptions.

        Page one names it with nothing but a URL; page two carries the label
        and the context. Deduplicating on first sight would keep the bare one
        and the whole list would read as unlabelled, which is what the caller
        gets no other signal about.
        """
        scraper = _scraper(mock_page)
        pages = [
            extracted("page 1", [{"kind": "job", "url": "/jobs/view/100/"}]),
            extracted(
                "page 2",
                [
                    {
                        "kind": "job",
                        "url": "/jobs/view/100/",
                        "text": "Staff Engineer",
                        "context": "saved job",
                    },
                    {"kind": "job", "url": "/jobs/view/200/", "text": "Two"},
                ],
            ),
        ]
        id_pages = iter([["100"], ["200"]])
        with (
            patch.object(
                scraper._pages,
                "_extract_saved_jobs_page",
                side_effect=self._navigating(mock_page, pages),
            ),
            patch.object(
                scraper._pages,
                "_extract_job_ids",
                new_callable=AsyncMock,
                side_effect=lambda *a, **kw: next(id_pages),
            ),
            patch.object(
                scraper._pages,
                "_get_total_list_pages",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "linkedin_mcp_server.scraping.jobs.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await scraper.get_saved_jobs(max_pages=2)

        assert result["references"]["saved_jobs"] == [
            {
                "kind": "job",
                "url": "/jobs/view/100/",
                "text": "Staff Engineer",
                "context": "saved job",
            },
            {"kind": "job", "url": "/jobs/view/200/", "text": "Two"},
        ]
