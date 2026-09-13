"""Job posting, job search and saved-job list workflows."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import asyncio
import logging
import time

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import SectionCapture
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.identifiers import job_view_url, normalize_job_id
from linkedin_mcp_server.scraping.job_pages import JobPageReader
from linkedin_mcp_server.scraping.job_policy import (
    JOB_SEARCH_PATHS,
    RESULTS_PER_LINKEDIN_PAGE,
    SAVED_JOBS_PAGE_SIZE,
    SAVED_JOBS_PATHS,
    SAVED_JOBS_URL,
    SCROLL_BUDGET_TOTAL,
    SCROLL_DEADLINE_MAX,
    SEARCH_TIMEOUT_FRACTION,
    dropped_filters_section_error,
    dropped_offset_section_error,
    lost_keywords_section_error,
    reconcile_search_references,
)
from linkedin_mcp_server.scraping.link_metadata import Reference, dedupe_references
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.search_urls import build_job_search_url
from linkedin_mcp_server.scraping.session import NAV_DELAY

logger = logging.getLogger(__name__)


class JobScraper:
    """Own every workflow whose subject is a LinkedIn job posting or list.

    The pages themselves are read by `JobPageReader`, which is a service under
    this rather than a peer domain owner: it answers with a `JobPageCapture`
    and knows nothing about budgets, offsets or which page comes next. The two
    list walks below deliberately check their captures in different orders,
    because a search proves its query survived before it accepts an empty
    page while a saved list has no query to prove.
    """

    def __init__(
        self,
        navigator: PageNavigator,
        capture: SectionCapture,
        pages: JobPageReader,
    ):
        self._navigator = navigator
        self._capture = capture
        self._pages = pages

    async def scrape_job(self, job_id: str) -> dict[str, Any]:
        """Scrape a single job posting.

        Returns:
            {url, sections: {name: text}}
        """
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id, "/")
        extracted = await self._capture.extract_page(url, section_name="job_posting")

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["job_posting"] = extracted.text
            if extracted.references:
                references["job_posting"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["job_posting"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["job_posting"] = extracted.error

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        max_pages: int = 3,
        date_posted: str | None = None,
        job_type: str | None = None,
        experience_level: str | None = None,
        work_type: str | None = None,
        easy_apply: bool = False,
        sort_by: str | None = None,
        tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Search for jobs with pagination and job ID extraction.

        Scrolls the job sidebar (not the main page) and paginates through
        results. Uses LinkedIn's "Page X of Y" indicator to cap pagination,
        and stops early when a page yields no new job IDs.

        Args:
            keywords: Search keywords
            location: Optional location filter
            max_pages: Maximum pages to load (1-10, default 3)
            date_posted: Filter by date posted (past_hour, past_24_hours, past_week, past_month)
            job_type: Filter by job type (full_time, part_time, contract, temporary, volunteer, internship, other)
            experience_level: Filter by experience level (internship, entry, associate, mid_senior, director, executive)
            work_type: Filter by work type (on_site, remote, hybrid)
            easy_apply: Only show Easy Apply jobs
            sort_by: Sort results (date, relevance)

        Returns:
            {url, sections: {search_results: text}, job_ids: [str]}
        """
        base_url = build_job_search_url(
            keywords,
            location=location,
            date_posted=date_posted,
            job_type=job_type,
            experience_level=experience_level,
            work_type=work_type,
            easy_apply=easy_apply,
            sort_by=sort_by,
        )
        all_job_ids: list[str] = []
        seen_ids: set[str] = set()
        page_texts: list[str] = []
        page_references: list[Reference] = []
        section_errors: dict[str, dict[str, Any]] = {}
        # Kept beside the errors rather than in them. A filter LinkedIn
        # dropped describes the results that came back, and those stay in the
        # response whatever stops the loop later, so a rate limit on page two
        # used to hide that page one had been unfiltered all along.
        filters_warning: dict[str, str] | None = None
        total_pages: int | None = None
        total_pages_queried = False

        # The search-wide scroll budget is spent as it goes rather than
        # divided up front, because dividing it charges every navigation for
        # navigations that may never run. At max_pages=10 each page got 6s,
        # and a first card that takes 4.5s leaves no room for the batch behind
        # it, so asking for more pages returned fewer jobs than asking for
        # three. Each page now takes the per-page cap or what is left,
        # whichever is smaller, and the total is the same 60s.
        scroll_budget_left = SCROLL_BUDGET_TOTAL
        # The offset follows what the pages actually rendered. LinkedIn's own
        # stride would skip every result it renders beyond it.
        offset = 0

        # The next navigation is costed from the slowest one so far rather than
        # a constant: the real figure is 6.5s and the `goto` timeout alone is
        # 30s, so a fixed guess is wrong in both directions.
        started = time.monotonic()
        budget = tool_timeout * SEARCH_TIMEOUT_FRACTION
        slowest_page = 0.0

        for page_num in range(max_pages):
            # Stop once the offset is past the last advertised result
            if (
                total_pages is not None
                and offset >= total_pages * RESULTS_PER_LINKEDIN_PAGE
            ):
                logger.debug(
                    "Offset %d is past the %d advertised pages, stopping",
                    offset,
                    total_pages,
                )
                break

            elapsed = time.monotonic() - started
            if page_num > 0 and elapsed + NAV_DELAY + slowest_page > budget:
                logger.debug(
                    "Stopping after %d pages: %.1fs spent, another page costs "
                    "up to %.1fs and the budget is %.1fs",
                    page_num,
                    elapsed,
                    NAV_DELAY + slowest_page,
                    budget,
                )
                break

            if page_num > 0:
                await asyncio.sleep(NAV_DELAY)

            # Started after the delay, because the prediction above adds
            # `NAV_DELAY` to `slowest_page` itself. Timing from before the
            # sleep folds it into every page after the first and then charges
            # it a second time, which stops a page early for every two seconds
            # of delay the run has already paid for.
            page_started = time.monotonic()

            url = base_url if offset == 0 else f"{base_url}&start={offset}"
            # Against what is left of the tool's own timeout as well. The
            # per-page cap is twelve seconds and the whole search gets
            # `tool_timeout` times the fraction above, so a caller passing ten
            # seconds had the first scroll alone allowed to outlast the call
            # and take every page gathered with it. Scrolling is the one part
            # already told how long it may run, so it is the one part this can
            # bound without handing the budget down into navigation.
            scroll_deadline = min(
                SCROLL_DEADLINE_MAX,
                scroll_budget_left,
                max(0.0, budget - (time.monotonic() - started)),
            )

            try:
                capture = await self._pages._extract_search_page(
                    url,
                    section_name="search_results",
                    scroll_deadline=scroll_deadline,
                )
                extracted = capture.section
                slowest_page = max(slowest_page, time.monotonic() - page_started)
                scroll_budget_left = max(
                    0.0, scroll_budget_left - capture.scroll_seconds
                )

                # Rate limits and extraction failures are already classified;
                # they win over route diagnostics. A clean empty page is not
                # accepted yet, because a redirect that dropped the keywords,
                # filters or offset can render empty too. Calling that "no jobs"
                # is a successful answer to a different search.
                if extracted.text == RATE_LIMITED_SECTION_TEXT:
                    section_errors["search_results"] = rate_limited_section_error()
                    break
                if not extracted.text and extracted.error:
                    section_errors["search_results"] = extracted.error
                    break

                # Prove the destination still represents the requested search
                # before accepting even an empty result. The id extraction is
                # later, after a clean empty page has stopped the loop.
                #
                # The parsed path, like the redirect check above, and not a
                # prefix: `/jobs/search?keywords=x` is the same route, and the
                # `?` sits where a prefix test wants the slash. That page is
                # healthy, passes the redirect check, and yields its text,
                # while this guard skipped extraction and ended pagination,
                # so the search returned `job_ids: []` with nothing to say
                # why. LinkedIn was not observed serving the slashless form,
                # but a same-document `replaceState` can produce it.
                #
                # Both routes, because LinkedIn 302s `/jobs/search/` to
                # `/jobs/search-results` for the redesigned experience. The
                # destination is the search, serves the same results and
                # honours `start`, so refusing it skipped extraction on every
                # account already moved over.
                parsed_url = urlparse(capture.landed_url)
                if (
                    parsed_url.netloc != "www.linkedin.com"
                    or parsed_url.path.rstrip("/") not in JOB_SEARCH_PATHS
                ):
                    logger.debug(
                        "Unexpected page URL after extraction: %s — "
                        "skipping job ID extraction",
                        capture.landed_url,
                    )
                    # Dropped whole. Keeping its text and references handed a
                    # page that is not the search back under `search_results`,
                    # carrying whatever job links it held. Raised rather than
                    # broken out of, because a result with no ids and nothing
                    # beside it is what an exhausted search looks like.
                    await self._navigator._raise_if_auth_barrier(capture.landed_url)
                    raise RuntimeError(
                        f"Search navigation ended on {capture.landed_url}"
                    )

                # The offset has to have survived as well as the route. A
                # navigation canonicalised back to the bare search URL serves
                # the first page again, and the loop then reads it a second
                # time, appends its text to itself under `search_results`, and
                # stops on the repeated ids with no error to say so. The
                # saved list does exactly this since LinkedIn moved it, so
                # this is not hypothetical; job search was measured honouring
                # `start` at 0, 10 and 21, which is why the mismatch stops the
                # loop rather than raising. Only `start` is compared, because
                # LinkedIn appends `currentJobId` to the query by itself.
                # The filters have to have survived too, and their presence
                # is what can be checked: a redirect to the bare search page
                # keeps the route and drops the query whole, and generic
                # recommendations then come back as a filtered search. Not
                # their values, because a query LinkedIn re-encodes on its way
                # would fail a comparison every healthy call makes.
                #
                # Losing the keywords ends the search, since what comes back
                # is not a narrower answer to the question but an answer to a
                # different one. Losing any other filter is reported and the
                # results kept: they are broader than asked for and still
                # about the same keywords, and stopping on a parameter
                # LinkedIn merely renamed would return nothing at all.
                landed_query = parse_qs(parsed_url.query)
                asked = parse_qs(urlparse(base_url).query)
                asked_keywords = asked.get("keywords", [""])[0]
                landed_keywords = landed_query.get("keywords", [""])[0]
                if asked_keywords and landed_keywords != asked_keywords:
                    logger.debug(
                        "Search keywords did not survive navigation "
                        "(asked %r, landed %r on %s), stopping",
                        asked_keywords,
                        landed_keywords,
                        capture.landed_url,
                    )
                    section_errors["search_results"] = lost_keywords_section_error(
                        asked_keywords, landed_keywords
                    )
                    break

                # Presence only for the rest, where the keywords are compared
                # by value: LinkedIn encodes several of these itself, a
                # location becoming a `geoUrn`, so a value comparison would
                # fail on every healthy call that used one.
                lost = sorted(
                    name
                    for name in asked
                    if name not in ("keywords", "start") and not landed_query.get(name)
                )
                if lost:
                    logger.debug(
                        "Search filters %s did not survive navigation to %s",
                        lost,
                        capture.landed_url,
                    )
                    filters_warning = dropped_filters_section_error(
                        lost, capture.landed_url
                    )

                landed_start = landed_query.get("start", ["0"])[0]
                if landed_start != str(offset):
                    logger.debug(
                        "Search offset %d did not survive navigation "
                        "(landed on %s), stopping",
                        offset,
                        capture.landed_url,
                    )
                    section_errors["search_results"] = dropped_offset_section_error(
                        offset, capture.landed_url
                    )
                    break

                if not extracted.text:
                    # The route and query survived, so this is a real empty
                    # result rather than a redirect that silently replaced the
                    # search. Do not read ids from a DOM that supplied no text.
                    break

                # Read total pages from pagination state (once only, best-effort)
                if not total_pages_queried:
                    total_pages_queried = True
                    try:
                        total_pages = await self._pages._get_total_search_pages()
                    except Exception as e:
                        logger.debug("Could not read total pages: %s", e)
                    else:
                        if total_pages is not None:
                            logger.debug("LinkedIn reports %d total pages", total_pages)

                page_ids = list(
                    dict.fromkeys(await self._pages._extract_job_ids(scoped=True))
                )
                # Advance by what this navigation rendered, including ids seen
                # on earlier pages: the next unseen result sits right behind them.
                #
                # This counts the whole document because everything the page
                # holds also sits in the rail. That is only true while the
                # next URL is built from `base_url`. LinkedIn appends
                # `currentJobId` to the landed URL after a navigation, and
                # carrying that forward opens a detail pane for a job the
                # rail has not reached, whose permalink is then counted as a
                # result and skips one. Keep paging from `base_url`.
                offset += len(page_ids)
                new_ids = [jid for jid in page_ids if jid not in seen_ids]

                page_refs = reconcile_search_references(extracted.references, page_ids)

                if not new_ids:
                    page_texts.append(extracted.text)
                    if page_refs:
                        page_references.extend(page_refs)
                    logger.debug("No new job IDs on page %d, stopping", page_num + 1)
                    break

                for jid in new_ids:
                    seen_ids.add(jid)
                    all_job_ids.append(jid)

                page_texts.append(extracted.text)
                if page_refs:
                    page_references.extend(page_refs)

            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error on search page %d: %s", page_num + 1, e)
                section_errors["search_results"] = build_issue_diagnostics(
                    e,
                    context="search_jobs",
                    target_url=url,
                    section_name="search_results",
                )
                break

        result: dict[str, Any] = {
            "url": base_url,
            "sections": {"search_results": "\n---\n".join(page_texts)}
            if page_texts
            else {},
            "job_ids": all_job_ids,
        }
        if page_references:
            result["references"] = {
                "search_results": dedupe_references(page_references)
            }
        if filters_warning is not None:
            existing = section_errors.get("search_results")
            if existing is None:
                section_errors["search_results"] = filters_warning
            else:
                # Both are true of this response, and only one slot holds
                # them. The stop reason leads, since it explains why the list
                # ends where it does, and the filter note follows it whole.
                existing["error_message"] = (
                    f"{existing['error_message']} {filters_warning['error_message']}"
                )
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_saved_jobs(self, max_pages: int = 3) -> dict[str, Any]:
        """List the authenticated user's saved job postings.

        Navigates to ``/my-items/saved-jobs/``, extracts innerText and job IDs
        from each page, and paginates with ``?start=`` offsets (10 per step).

        Args:
            max_pages: Maximum pages to load (1-10, default 3)

        Returns:
            {url, sections: {saved_jobs: text}, job_ids: [str]}
        """
        base_url = SAVED_JOBS_URL
        all_job_ids: list[str] = []
        seen_ids: set[str] = set()
        page_texts: list[str] = []
        page_references: list[Reference] = []
        section_errors: dict[str, dict[str, Any]] = {}
        total_pages: int | None = None
        total_pages_queried = False

        for page_num in range(max_pages):
            if total_pages is not None and page_num >= total_pages:
                logger.debug("All %d saved-jobs pages fetched, stopping", total_pages)
                break

            if page_num > 0:
                await asyncio.sleep(NAV_DELAY)

            url = (
                base_url
                if page_num == 0
                else f"{base_url}?start={page_num * SAVED_JOBS_PAGE_SIZE}"
            )

            try:
                capture = await self._pages._extract_saved_jobs_page(
                    url, section_name="saved_jobs"
                )
                extracted = capture.section

                # Rate limit first: it is the more specific diagnosis, and a
                # page that was throttled may carry a generic error too. Then
                # the extraction error, which names what actually failed and
                # would be masked by the route guard below.
                if extracted.text == RATE_LIMITED_SECTION_TEXT:
                    section_errors["saved_jobs"] = rate_limited_section_error()
                    break
                if extracted.error:
                    section_errors["saved_jobs"] = extracted.error
                    break

                # Host and parsed path, like the job-search guard: a
                # substring test accepts any origin that happens to serve
                # this path, and an interstitial carrying a single
                # /jobs/view/ anchor would come back as the account's saved
                # jobs.
                #
                # Both destinations, because LinkedIn now answers
                # /my-items/saved-jobs/ with a redirect to /jobs-tracker/ and
                # drops the query on the way. Measured on 2026-08-21 against
                # an authenticated profile, for the bare URL and for
                # ?start=10 alike. The old route is kept because the redirect
                # is a rollout and the server still navigates to it.
                parsed_url = urlparse(capture.landed_url)
                if (
                    parsed_url.netloc != "www.linkedin.com"
                    or parsed_url.path.rstrip("/") not in SAVED_JOBS_PATHS
                ):
                    logger.debug(
                        "Unexpected page URL after saved-jobs extraction: %s "
                        "(requested %s) — skipping job ID extraction",
                        capture.landed_url,
                        url,
                    )
                    # The page is dropped whole. Keeping its text and
                    # references put a stranger's page under `saved_jobs`
                    # with the job links it happened to carry, which reads
                    # as the account's own list. Raised and not broken out
                    # of, because an empty result with nothing beside it is
                    # what an account with nothing saved looks like.
                    # Classified first, so an expired session reaches the
                    # relogin path instead of a diagnostic. Against the page
                    # that answered, because that is where the barrier is; the
                    # address that was asked for is on the line above.
                    await self._navigator._raise_if_auth_barrier(capture.landed_url)
                    raise RuntimeError(
                        f"Saved jobs navigation ended on {capture.landed_url}"
                    )

                if not extracted.text:
                    # Nothing to read, and the page is the one that was asked
                    # for: an account with nothing saved.
                    break

                if not total_pages_queried:
                    total_pages_queried = True
                    try:
                        total_pages = await self._pages._get_total_list_pages()
                    except Exception as e:
                        logger.debug("Could not read saved-jobs page count: %s", e)
                    else:
                        if total_pages is not None:
                            logger.debug(
                                "LinkedIn reports %d saved-jobs pages", total_pages
                            )

                # An offset that did not survive the navigation means this
                # is the first page again, and reading it a second time
                # appends the whole list to itself under `saved_jobs` before
                # the no-new-ids branch stops the loop. Measured on
                # 2026-08-21: `/jobs-tracker/?start=10` lands on
                # `/jobs-tracker/`, and so does the old route, so the offset
                # is gone from the list rather than from one address for it.
                # Judged from where the page landed and not from that
                # measurement, so an account still served the old route keeps
                # paginating.
                # Read here rather than taken from the capture: the page-count
                # read above is a whole navigation's worth of opportunity for
                # the address to move, and the capture predates it.
                landed_url = self._pages.current_url
                landed_start = parse_qs(urlparse(landed_url).query).get("start", ["0"])[
                    0
                ]
                if landed_start != str(page_num * SAVED_JOBS_PAGE_SIZE):
                    logger.debug(
                        "Saved-jobs offset %d did not survive navigation "
                        "(landed on %s), stopping",
                        page_num * SAVED_JOBS_PAGE_SIZE,
                        landed_url,
                    )
                    section_errors["saved_jobs"] = dropped_offset_section_error(
                        page_num * SAVED_JOBS_PAGE_SIZE, landed_url
                    )
                    break

                page_ids = await self._pages._extract_job_ids()
                new_ids = [jid for jid in page_ids if jid not in seen_ids]

                if not new_ids:
                    page_texts.append(extracted.text)
                    if extracted.references:
                        page_references.extend(extracted.references)
                    logger.debug(
                        "No new saved job IDs on page %d, stopping", page_num + 1
                    )
                    break

                for jid in new_ids:
                    seen_ids.add(jid)
                    all_job_ids.append(jid)

                page_texts.append(extracted.text)
                if extracted.references:
                    page_references.extend(extracted.references)

            except LinkedInScraperException:
                raise
            except Exception as e:
                logger.warning("Error on saved jobs page %d: %s", page_num + 1, e)
                section_errors["saved_jobs"] = build_issue_diagnostics(
                    e,
                    context="get_saved_jobs",
                    target_url=url,
                    section_name="saved_jobs",
                )
                break

        result: dict[str, Any] = {
            "url": base_url,
            "sections": {"saved_jobs": "\n---\n".join(page_texts)}
            if page_texts
            else {},
            "job_ids": all_job_ids,
        }
        if page_references:
            result["references"] = {
                "saved_jobs": dedupe_references(page_references, cap=15)
            }
        if section_errors:
            result["section_errors"] = section_errors
        return result
