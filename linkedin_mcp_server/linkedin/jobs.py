"""Job posting, job search and saved-job list workflows."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import asyncio
import logging
import time

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.core.utils import detect_rate_limit, handle_modal_close
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.linkedin.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_mcp_server.linkedin.identifiers import job_view_url, normalize_job_id
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.job_policy import (
    JOB_SEARCH_PATHS,
    RESULTS_PER_LINKEDIN_PAGE,
    SAVED_JOBS_PAGE_SIZE,
    SAVED_JOBS_PATHS,
    SAVED_JOBS_URL,
    SCROLL_BUDGET_TOTAL,
    SCROLL_DEADLINE_MAX,
    SEARCH_TIMEOUT_FRACTION,
    JobsTrackerStage,
    apply_link_missing_section_error,
    dropped_filters_section_error,
    dropped_offset_section_error,
    label_similar_jobs,
    lost_keywords_section_error,
    missing_description_section_error,
    no_matching_jobs_section_error,
    posting_state,
    reconcile_search_references,
)
from linkedin_mcp_server.linkedin.link_metadata import Reference, dedupe_references
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.search_urls import build_job_search_url
from linkedin_mcp_server.linkedin.session import NAV_DELAY
from linkedin_mcp_server.linkedin.text import (
    JOB_APPLY_EN_US,
    JOB_POSTING_EN_US,
    JOB_SEARCH_EN_US,
    JobApplyTextTable,
    JobSearchTextTable,
)

logger = logging.getLogger(__name__)


def _on_tracker_tab(url: str, stage: JobsTrackerStage) -> bool:
    """Whether ``url`` is LinkedIn's own tracker, showing the ``stage`` tab.

    Host and parsed path rather than a substring, so another origin serving
    the same path is not the account's list. No ``?stage=`` is the saved tab.
    """
    parsed = urlparse(url)
    landed_stage = parse_qs(parsed.query).get("stage", ["saved"])[0]
    return (
        parsed.netloc == "www.linkedin.com"
        and parsed.path.rstrip("/") in SAVED_JOBS_PATHS
        and landed_stage == stage
    )


_LOCALE_JOB_LABELS: dict[str, tuple[str, str]] = {
    "en": ("Easy Apply", "Applied"),
}


def _get_job_labels(locale: str = "en") -> tuple[str, str]:
    """Return (easy_apply, applied) labels for the given locale.

    Falls back to English if locale is not in the table.
    """
    return _LOCALE_JOB_LABELS.get(locale, _LOCALE_JOB_LABELS["en"])


_DIALOG_SELECTOR = 'dialog[open], [role="dialog"]'


class JobReader:
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
        search_text: JobSearchTextTable = JOB_SEARCH_EN_US,
        apply_text: JobApplyTextTable = JOB_APPLY_EN_US,
    ):
        self._navigator = navigator
        self._capture = capture
        self._pages = pages
        self._search_text = search_text
        self._apply_text = apply_text

    async def read_job(self, job_id: str) -> dict[str, Any]:
        """Read a single job posting.

        Returns:
            {url, sections: {name: text}}, plus {apply: {type}} when the
            posting shows it was applied to or has closed.
        """
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id, "/")
        extracted = await self._capture.capture(
            url,
            section_name="job_posting",
            plan=CapturePlan(CaptureMode.JOB_POSTING),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["job_posting"] = extracted.text
            if extracted.references:
                references["job_posting"] = label_similar_jobs(
                    extracted.references, job_id
                )
            if not JOB_POSTING_EN_US.has_description(extracted.text):
                section_errors["job_posting"] = missing_description_section_error()
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
        if "job_posting" in sections:
            state = posting_state(sections["job_posting"], self._apply_text)
            if state is not None:
                result["apply"] = {"type": state}
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def get_job_apply_url(self, job_id: str) -> dict[str, Any]:
        """Read how a posting takes applications and where the employer's form is.

        Returns:
            {url, apply: {type, url?}}, or {url, section_errors} when the
            posting could not be read. An external posting whose Apply
            revealed no link carries a section error beside its type.
        """
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id, "/")
        try:
            read = await self._pages.read_apply_link(url, job_id, self._apply_text)
        except LinkedInOperationError:
            raise
        except Exception as e:
            logger.warning("Failed to read how %s takes applications: %s", url, e)
            return {
                "url": url,
                "section_errors": {
                    "apply": build_issue_diagnostics(
                        e,
                        context="get_job_apply_url",
                        target_url=url,
                        section_name="apply",
                    )
                },
            }

        apply: dict[str, str] = {"type": read.type}
        result: dict[str, Any] = {"url": url, "apply": apply}
        if read.url is not None:
            apply["url"] = read.url
        elif read.type == "external":
            result["section_errors"] = {"apply": apply_link_missing_section_error()}
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
        total: dict[str, Any] | None = None
        promoted_ids: list[str] = []
        # False once a page that added ids could not be read. The list would
        # then cover only some of `job_ids`, and a caller takes a job missing
        # from it as not promoted.
        promoted_complete = True

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
            # and take every page gathered with it. Navigation and the
            # rate-limit retry are handed the budget's end below.
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
                    page_deadline=started + budget,
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
                is_no_match = bool(extracted.text) and self._search_text.shows_no_match(
                    extracted.text
                )
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
                if lost and not is_no_match:
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

                if is_no_match:
                    # LinkedIn's substitute for zero results keeps the route
                    # and the query, so every check above passes, and its
                    # cards are real job links. Read as a result page it
                    # returned unrelated postings as `job_ids`, and a second
                    # page served the same ones again. After a page of real
                    # results it only means the list has ended.
                    logger.debug(
                        "Search page %d shows recommendations, not results",
                        page_num + 1,
                    )
                    if not all_job_ids:
                        section_errors["search_results"] = (
                            no_matching_jobs_section_error(keywords)
                        )
                    break

                if page_num == 0:
                    count = self._search_text.result_count(extracted.text)
                    if count is not None:
                        total = {"count": count[0], "exact": count[1]}

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

                # Best effort, like the page count: a read that fails costs the
                # key and never the page.
                try:
                    promoted = set(
                        await self._pages._extract_promoted_job_ids(
                            self._search_text.promoted_label
                        )
                    )
                except LinkedInOperationError:
                    raise
                except Exception as e:
                    logger.debug("Could not read promoted jobs: %s", e)
                    promoted_complete = False
                else:
                    promoted_ids.extend(jid for jid in new_ids if jid in promoted)

                for jid in new_ids:
                    seen_ids.add(jid)
                    all_job_ids.append(jid)

                page_texts.append(extracted.text)
                if page_refs:
                    page_references.extend(page_refs)

            except LinkedInOperationError:
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
        if total is not None:
            result["total"] = total
        # Each page adds its ids only after its promoted read, so ids with no
        # failed read mean every page that contributed was read.
        if all_job_ids and promoted_complete:
            result["promoted_job_ids"] = promoted_ids
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

    async def get_saved_jobs(
        self, max_pages: int = 3, stage: JobsTrackerStage = "saved"
    ) -> dict[str, Any]:
        """List the authenticated user's jobs at one job-tracker stage.

        Navigates to ``/jobs-tracker/?stage=<stage>``, extracts innerText and
        job IDs from each page, and paginates with ``&start=`` offsets (10 per
        step).

        Args:
            max_pages: Maximum pages to load (1-10, default 3)
            stage: Tracker tab to read: saved, in_progress, applied, archived

        Returns:
            {url, sections: {saved_jobs: text}, job_ids: [str]}
        """
        base_url = f"{SAVED_JOBS_URL}?stage={stage}"
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
                else f"{base_url}&start={page_num * SAVED_JOBS_PAGE_SIZE}"
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
                # The stage too: a tracker that dropped it shows another tab,
                # and applied jobs would come back as saved ones. No
                # parameter is the saved tab.
                if not _on_tracker_tab(capture.landed_url, stage):
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
                # `/jobs-tracker/`. Judged from where the page landed and not
                # from that measurement, so paging works again wherever
                # LinkedIn keeps the offset.
                # Read here rather than taken from the capture: the page-count
                # read above is a whole navigation's worth of opportunity for
                # the address to move, and the capture predates it.
                landed_url = self._pages.current_url
                # The tab too, for the same reason: a page-one address that
                # lost `?stage=` has no `start` to fail on, and would hand the
                # saved tab's ids back as the stage asked for.
                if not _on_tracker_tab(landed_url, stage):
                    logger.debug(
                        "Saved-jobs page moved to %s while its page count "
                        "was read (requested %s)",
                        landed_url,
                        url,
                    )
                    await self._navigator._raise_if_auth_barrier(landed_url)
                    raise RuntimeError(f"Saved jobs page moved to {landed_url}")
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

            except LinkedInOperationError:
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

    @staticmethod
    def _application_action_result(
        url: str,
        status: str,
        message: str,
        *,
        questions: list[dict[str, str]] | None = None,
        already_applied: bool = False,
        saved: bool = False,
        sent: bool = False,
    ) -> dict[str, Any]:
        """Build a structured response for job-application tools."""
        result: dict[str, Any] = {
            "url": url,
            "status": status,
            "message": message,
        }
        if questions is not None:
            result["questions"] = questions
        if already_applied:
            result["already_applied"] = True
        if saved:
            result["saved"] = True
        if sent:
            result["sent"] = True
        return result

    async def save_job(
        self,
        job_id: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Bookmark a job posting with explicit confirmation gating."""
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id, "/")
        page = self._pages._session.page
        await self._navigator._navigate_to_page(url)
        await detect_rate_limit(page)

        try:
            await page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Job page did not load for %s", job_id)

        await handle_modal_close(page)

        # Detect Save button. The verb prefix in aria-label is locale-
        # dependent (Save / Saved in English). aria-pressed is a stable
        # structural signal: present and "true" means already saved.
        button_state = await page.evaluate(
            """() => {
                const candidates = Array.from(document.querySelectorAll(
                    'main button[aria-label*="Save"], main button[aria-label*="Saved"]'
                ));
                const btn = candidates.find(b =>
                    !b.disabled
                    && (b.offsetWidth || b.offsetHeight || b.getClientRects().length)
                );
                if (!btn) return { found: false };
                return {
                    found: true,
                    pressed: btn.getAttribute('aria-pressed') === 'true',
                    label: btn.getAttribute('aria-label') || '',
                };
            }"""
        )

        if not button_state.get("found"):
            return self._application_action_result(
                page.url,
                "save_unavailable",
                "LinkedIn did not expose a Save button on this job posting.",
            )

        if button_state.get("pressed"):
            return self._application_action_result(
                page.url,
                "already_saved",
                "Job is already saved.",
                saved=True,
            )

        if not confirm_send:
            return self._application_action_result(
                page.url,
                "confirmation_required",
                "Set confirm_send=true to bookmark the job.",
            )

        clicked = await page.evaluate(
            """() => {
                const candidates = Array.from(document.querySelectorAll(
                    'main button[aria-label*="Save"]'
                ));
                const btn = candidates.find(b =>
                    !b.disabled
                    && b.getAttribute('aria-pressed') !== 'true'
                    && (b.offsetWidth || b.offsetHeight || b.getClientRects().length)
                );
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )
        if not clicked:
            return self._application_action_result(
                page.url,
                "save_click_failed",
                "Could not click Save button.",
            )

        await asyncio.sleep(0.5)
        post_state = await page.evaluate(
            """() => {
                const btn = Array.from(document.querySelectorAll(
                    'main button[aria-label*="Save"], main button[aria-label*="Saved"]'
                )).find(b => !b.disabled
                    && (b.offsetWidth || b.offsetHeight || b.getClientRects().length));
                return btn ? btn.getAttribute('aria-pressed') === 'true' : false;
            }"""
        )
        if not post_state:
            return self._application_action_result(
                page.url,
                "save_not_confirmed",
                "Clicked Save but LinkedIn did not flip the saved state.",
            )

        return self._application_action_result(
            page.url,
            "saved",
            "Job saved.",
            saved=True,
        )

    async def list_applied_jobs(self, max_pages: int = 3) -> dict[str, Any]:
        """List jobs the authenticated user has applied to."""
        base_url = "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED"
        all_text: list[str] = []
        all_ids: list[str] = []
        seen_ids: set[str] = set()
        last_url = base_url

        for page_index in range(max_pages):
            page_url = (
                base_url
                if page_index == 0
                else f"{base_url}&start={page_index * RESULTS_PER_LINKEDIN_PAGE}"
            )
            extracted = await self._capture.extract_page(
                page_url, section_name="applied_jobs"
            )
            last_url = page_url
            if not extracted.text or extracted.text == RATE_LIMITED_SECTION_TEXT:
                break
            all_text.append(extracted.text)
            ids = await self._pages._extract_job_ids()
            for jid in ids:
                if jid not in seen_ids:
                    seen_ids.add(jid)
                    all_ids.append(jid)
            await asyncio.sleep(NAV_DELAY)

        sections = {"applied_jobs": "\n\n---\n\n".join(all_text)} if all_text else {}
        return {
            "url": last_url,
            "sections": sections,
            "job_ids": all_ids,
        }

    async def easy_apply_inspect(self, job_id: str) -> dict[str, Any]:
        """Inspect the Easy Apply flow without submitting an application."""
        opened = await self._open_easy_apply_dialog(job_id)
        if not opened["ok"]:
            return opened["result"]

        page = self._pages._session.page
        try:
            inspection = await self._inspect_easy_apply_dialog()
            return self._application_action_result(
                page.url,
                "ok",
                (
                    f"Easy Apply dialog opened with {inspection['step_count']} step(s) "
                    f"and {len(inspection['questions'])} question(s)."
                ),
                questions=inspection["questions"],
            )
        finally:
            await self._dismiss_dialog()

    async def easy_apply_submit(
        self,
        job_id: str,
        *,
        confirm_send: bool,
    ) -> dict[str, Any]:
        """Submit a zero-question Easy Apply application."""
        opened = await self._open_easy_apply_dialog(job_id)
        if not opened["ok"]:
            return opened["result"]

        page = self._pages._session.page
        try:
            inspection = await self._inspect_easy_apply_dialog()
            if inspection["questions"]:
                return self._application_action_result(
                    page.url,
                    "manual_review",
                    (
                        "Easy Apply contains questions. Resolve them in a human "
                        "session; do not auto-submit."
                    ),
                    questions=inspection["questions"],
                )
            if inspection["step_count"] != 1 or not inspection["submit_button"]:
                return self._application_action_result(
                    page.url,
                    "manual_review",
                    "Easy Apply is multi-step or missing a single Submit button.",
                    questions=inspection["questions"],
                )

            if not confirm_send:
                return self._application_action_result(
                    page.url,
                    "confirmation_required",
                    "Set confirm_send=true to submit the Easy Apply application.",
                )

            submitted = await self._click_easy_apply_submit()
            if not submitted:
                return self._application_action_result(
                    page.url,
                    "submit_failed",
                    "Could not click Submit application button.",
                )

            # Wait for confirmation: dialog closes or "Applied" badge appears
            try:
                await page.wait_for_function(
                    """() => {
                        const dialog = document.querySelector('dialog[open], [role="dialog"]');
                        if (!dialog) return true; // Dialog closed = success
                        return Array.from(
                            document.querySelectorAll('main span, main div')
                        ).some(el => /\\bApplied\\b/i.test((el.innerText || '').trim()));
                    }""",
                    timeout=5000,
                )
                confirmed = True
            except PlaywrightTimeoutError:
                confirmed = False

            if not confirmed:
                return self._application_action_result(
                    page.url,
                    "submission_unconfirmed",
                    "Submit was clicked but no success confirmation detected.",
                )

            return self._application_action_result(
                page.url,
                "submitted",
                "Easy Apply submitted.",
                sent=True,
            )
        finally:
            await self._dismiss_dialog()

    async def _open_easy_apply_dialog(self, job_id: str) -> dict[str, Any]:
        """Navigate to a job and open the Easy Apply dialog."""
        job_id = normalize_job_id(job_id)
        url = job_view_url(job_id, "/")
        page = self._pages._session.page
        await self._navigator._navigate_to_page(url)
        await detect_rate_limit(page)

        try:
            await page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("Job page did not load for %s", job_id)

        await handle_modal_close(page)

        # Use per-locale table for label detection (per CLAUDE.md scraping rules).
        easy_apply_label, applied_label = _get_job_labels()
        state = await page.evaluate(
            f"""() => {{
                const easyApplyBtn = Array.from(document.querySelectorAll(
                    'main button[aria-label*="{easy_apply_label}"]'
                )).find(b => !b.disabled
                    && (b.offsetWidth || b.offsetHeight || b.getClientRects().length));
                const appliedBadge = Array.from(
                    document.querySelectorAll('main span, main div')
                ).some(el => /\\b{applied_label}\\b/i.test((el.innerText || '').trim()));
                return {{
                    has_easy_apply: Boolean(easyApplyBtn),
                    already_applied: appliedBadge && !easyApplyBtn,
                }};
            }}"""
        )

        if state.get("already_applied"):
            return {
                "ok": False,
                "result": self._application_action_result(
                    page.url,
                    "already_applied",
                    "LinkedIn shows this job as already applied.",
                    already_applied=True,
                ),
            }

        if not state.get("has_easy_apply"):
            return {
                "ok": False,
                "result": self._application_action_result(
                    page.url,
                    "no_easy_apply",
                    "LinkedIn did not expose an Easy Apply button on this job.",
                ),
            }

        clicked = await page.evaluate(
            f"""() => {{
                const btn = Array.from(document.querySelectorAll(
                    'main button[aria-label*="{easy_apply_label}"]'
                )).find(b => !b.disabled
                    && (b.offsetWidth || b.offsetHeight || b.getClientRects().length));
                if (!btn) return false;
                btn.click();
                return true;
            }}"""
        )
        if not clicked:
            return {
                "ok": False,
                "result": self._application_action_result(
                    page.url,
                    "easy_apply_click_failed",
                    "Could not click Easy Apply button.",
                ),
            }

        try:
            await page.wait_for_selector(
                _DIALOG_SELECTOR, state="visible", timeout=5000
            )
        except PlaywrightTimeoutError:
            return {
                "ok": False,
                "result": self._application_action_result(
                    page.url,
                    "dialog_not_opened",
                    "Easy Apply dialog did not appear after clicking the button.",
                ),
            }

        return {"ok": True, "result": None}

    async def _inspect_easy_apply_dialog(self) -> dict[str, Any]:
        """Read the open Easy Apply dialog and report structure."""
        page = self._pages._session.page
        return await page.evaluate(
            """() => {
                const dialog = document.querySelector(
                    'dialog[open], [role="dialog"]'
                );
                if (!dialog) return {
                    step_count: 0,
                    questions: [],
                    submit_button: false,
                    text: '',
                };

                let stepCount = 1;
                const progress = dialog.querySelector('[role="progressbar"]');
                if (progress) {
                    const max = parseInt(
                        progress.getAttribute('aria-valuemax') || '1', 10
                    );
                    if (Number.isFinite(max) && max > 0) stepCount = max;
                }

                const questions = [];
                const inputs = dialog.querySelectorAll(
                    'input[type="text"], input[type="email"], input[type="tel"],'
                    + 'input[type="number"], input[type="file"], select, textarea'
                );
                for (const el of inputs) {
                    if (!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) {
                        continue;
                    }
                    const id = el.getAttribute('id');
                    let labelText = '';
                    if (id) {
                        const lbl = dialog.querySelector(
                            `label[for="${CSS.escape(id)}"]`
                        );
                        if (lbl) labelText = (lbl.innerText || '').trim();
                    }
                    if (!labelText) {
                        labelText = (el.getAttribute('aria-label') || '').trim();
                    }
                    if (!labelText) {
                        const wrap = el.closest('label, fieldset, div');
                        labelText = wrap ? (wrap.innerText || '').trim() : '';
                    }
                    questions.push({
                        label: labelText.slice(0, 200),
                        field_type: el.type || el.tagName.toLowerCase(),
                    });
                }

                const submitBtn = Array.from(
                    dialog.querySelectorAll('button')
                ).find(b => {
                    if (b.disabled) return false;
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.innerText || '').toLowerCase();
                    return label.includes('submit application')
                        || text.includes('submit application');
                });
                return {
                    step_count: stepCount,
                    questions,
                    submit_button: Boolean(submitBtn),
                    text: (dialog.innerText || '').slice(0, 4000),
                };
            }"""
        )

    async def _click_easy_apply_submit(self) -> bool:
        """Click the Submit application button in the Easy Apply dialog."""
        page = self._pages._session.page
        return await page.evaluate(
            """() => {
                const dialog = document.querySelector(
                    'dialog[open], [role="dialog"]'
                );
                if (!dialog) return false;
                const btn = Array.from(
                    dialog.querySelectorAll('button')
                ).find(b => {
                    if (b.disabled) return false;
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.innerText || '').toLowerCase();
                    return label.includes('submit application')
                        || text.includes('submit application');
                });
                if (!btn) return false;
                btn.click();
                return true;
            }"""
        )

    async def _get_next_button(self) -> bool:
        """Click the 'Continue to next step' button. Returns True if clicked."""
        page = self._pages._session.page
        return await page.evaluate(
            """() => {
                const dialog = document.querySelector('dialog[open], [role="dialog"]');
                if (!dialog) return false;
                const nextBtn = Array.from(
                    dialog.querySelectorAll('button')
                ).find(b => {
                    if (b.disabled) return false;
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.innerText || '').toLowerCase();
                    return label.includes('continue to next step')
                        || label.includes('next step')
                        || text.includes('continue to next step')
                        || text.includes('next step');
                });
                if (!nextBtn) return false;
                nextBtn.click();
                return true;
            }"""
        )

    async def _has_next_step(self) -> bool:
        """Check if there's a next step button (more steps to fill)."""
        page = self._pages._session.page
        return await page.evaluate(
            """() => {
                const dialog = document.querySelector('dialog[open], [role="dialog"]');
                if (!dialog) return false;
                const nextBtn = Array.from(
                    dialog.querySelectorAll('button')
                ).find(b => {
                    if (b.disabled) return false;
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const text = (b.innerText || '').toLowerCase();
                    return label.includes('continue to next step')
                        || label.includes('next step')
                        || text.includes('continue to next step')
                        || text.includes('next step');
                });
                return Boolean(nextBtn);
            }"""
        )

    async def _auto_fill_fields(self, answers: dict[str, str]) -> int:
        """Auto-fill form fields with provided answers. Returns count of filled fields."""
        if not answers:
            return 0

        page = self._pages._session.page
        filled = 0
        for label, value in answers.items():
            if not value:
                continue

            result = await page.evaluate(
                """(params) => {
                    const {label, value} = params;
                    const dialog = document.querySelector('dialog[open], [role="dialog"]');
                    if (!dialog) return false;

                    const labelEl = Array.from(dialog.querySelectorAll('label, h3, span'))
                        .find(el => (el.innerText || '').toLowerCase().includes(label.toLowerCase()));

                    if (!labelEl) return false;

                    const input = labelEl.querySelector('input, select, textarea');
                    const formGroup = labelEl.closest('div[data-test-form-item]') || labelEl.closest('.form-item');
                    const formInput = formGroup ? formGroup.querySelector('input, select, textarea') : null;
                    const target = input || formInput;

                    if (!target) return false;

                    if (target.tagName === 'SELECT') {
                        const option = Array.from(target.options)
                            .find(opt => opt.text.toLowerCase().includes(value.toLowerCase()));
                        if (option) {
                            target.value = option.value;
                            target.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    } else if (target.type === 'checkbox' || target.type === 'radio') {
                        if (value.toLowerCase() === 'yes' || value.toLowerCase() === 'true') {
                            if (!target.checked) target.click();
                            return true;
                        }
                    } else {
                        target.value = value;
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                    return false;
                }""",
                {"label": label, "value": value},
            )
            if result:
                filled += 1
        return filled

    async def _upload_resume_to_form(self, resume_path: str) -> bool:
        """Upload resume file to the file input in the dialog."""
        if not resume_path:
            return False

        page = self._pages._session.page
        try:
            file_input = page.locator(
                'dialog[open] input[type="file"], [role="dialog"] input[type="file"]'
            )
            if await file_input.count() > 0:
                await file_input.first.set_input_files(resume_path)
                return True
        except Exception as e:
            logger.warning("Failed to upload resume to file input: %s", e)
        return False

    # TODO: Deferred multi-step auto-fill implementation; register tool when
    # multi-step review UX is finalized.
    async def easy_apply_full(
        self,
        job_id: str,
        *,
        confirm_send: bool,
        resume_path: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Full multi-step Easy Apply with auto-fill."""
        opened = await self._open_easy_apply_dialog(job_id)
        if not opened["ok"]:
            return opened["result"]

        page = self._pages._session.page
        try:
            if not confirm_send:
                return self._application_action_result(
                    page.url,
                    "confirmation_required",
                    "Set confirm_send=true to submit.",
                )

            if resume_path:
                uploaded = await self._upload_resume_to_form(resume_path)
                if uploaded:
                    logger.info("Resume upload triggered for %s", job_id)
                    await asyncio.sleep(1)

            if answers:
                filled = await self._auto_fill_fields(answers)
                logger.info("Auto-filled %d fields for %s", filled, job_id)

            steps_completed = 0
            max_steps = 10

            while steps_completed < max_steps:
                if await self._has_next_step():
                    clicked = await self._get_next_button()
                    if clicked:
                        steps_completed += 1
                        await asyncio.sleep(0.5)
                        if answers:
                            await self._auto_fill_fields(answers)
                    else:
                        logger.warning(
                            "Has next step but couldn't click for %s", job_id
                        )
                        break
                else:
                    break

            submitted = await self._click_easy_apply_submit()
            if not submitted:
                return self._application_action_result(
                    page.url,
                    "submit_failed",
                    "Could not find Submit button.",
                )

            try:
                await page.wait_for_function(
                    """() => {
                        const dialog = document.querySelector('dialog[open], [role="dialog"]');
                        if (!dialog) return true;
                        return Array.from(
                            document.querySelectorAll('main span, main div')
                        ).some(el => /\\bApplied\\b/i.test((el.innerText || '').trim()));
                    }""",
                    timeout=5000,
                )
                confirmed = True
            except PlaywrightTimeoutError:
                confirmed = False

            if not confirmed:
                return self._application_action_result(
                    page.url,
                    "submission_unconfirmed",
                    "Submitted but confirmation not detected.",
                )

            return self._application_action_result(
                page.url,
                "submitted",
                f"Easy Apply submitted ({steps_completed} step(s) completed).",
                sent=True,
            )
        finally:
            await self._dismiss_dialog()

    async def _dismiss_dialog(self) -> None:
        """Best-effort dismissal of any open dialog."""
        page = self._pages._session.page
        try:
            close_clicked = await page.evaluate(
                """() => {
                    const dialog = document.querySelector(
                        'dialog[open], [role="dialog"]'
                    );
                    if (!dialog) return true;
                    const closeBtn = Array.from(
                        dialog.querySelectorAll('button')
                    ).find(b => {
                        if (b.disabled) return false;
                        const label = (
                            b.getAttribute('aria-label') || ''
                        ).toLowerCase();
                        return label.includes('dismiss')
                            || label.includes('close')
                            || label === 'cancel'
                            || (b.innerText || '').toLowerCase().trim() === 'cancel';
                    });
                    if (closeBtn) {
                        closeBtn.click();
                        return true;
                    }
                    return false;
                }"""
            )
            if not close_clicked:
                await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            logger.debug("Could not dismiss Easy Apply dialog", exc_info=True)
