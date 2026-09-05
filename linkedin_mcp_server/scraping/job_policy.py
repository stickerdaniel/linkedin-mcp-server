"""Routing, paging and reporting policy for the job list workflows."""

from __future__ import annotations

from urllib.parse import urlparse

from linkedin_mcp_server.scraping.link_metadata import (
    JOB_PATH_RE,
    Reference,
    _SEARCH_RESULTS_REFERENCE_CAP,
)


def reconcile_search_references(
    references: list[Reference], ids: list[str]
) -> list[Reference]:
    """Align one page's references with the job ids read from its rail.

    References come from the whole `<main>`, which also holds the detail pane;
    ids come from the selected results rail. The rail therefore decides which
    jobs exist, while the DOM references supply richer labels when available.
    Non-job references share the search-results cap's remaining allowance.
    """
    ordered_ids = list(dict.fromkeys(ids))
    kept_ids = set(ordered_ids)
    emitted_ids: set[str] = set()
    ancillary_left = max(0, _SEARCH_RESULTS_REFERENCE_CAP - len(ordered_ids))
    out: list[Reference] = []

    for ref in references:
        if ref.get("kind") == "job":
            match = JOB_PATH_RE.match(str(ref.get("url", "")))
            if match is None:
                continue
            job_id = match.group(1)
            if job_id not in kept_ids or job_id in emitted_ids:
                continue
            out.append(ref)
            emitted_ids.add(job_id)
            continue

        if ancillary_left:
            out.append(ref)
            ancillary_left -= 1

    for job_id in ordered_ids:
        if job_id not in emitted_ids:
            out.append({"kind": "job", "url": f"/jobs/view/{job_id}/"})

    return out


def lost_keywords_section_error(asked: str, landed: str) -> dict[str, str]:
    """The ``section_errors`` entry for a search that is not the one asked for.

    Both values are named, because the one shape this cannot rule out is
    LinkedIn re-encoding a query rather than changing it. `parse_qs` folds
    `%20` and `+` together, so ordinary spacing differences are already gone
    by the time they are compared; an unencoded `C++` read back as `C` and
    two spaces is the measured exception, and naming both sides is what makes
    that diagnosable from the response instead of from a debugger.
    """
    return {
        "error_type": "search_replaced",
        "error_message": (
            f"LinkedIn answered a search for {landed!r} where {asked!r} was "
            "asked for, so the results are about something else."
        ),
    }


def dropped_offset_section_error(offset: int, landed: str) -> dict[str, str]:
    """The ``section_errors`` entry for a list that cannot be paged further.

    LinkedIn dropping the offset serves the first page again, so the loop
    stops there. Stopping quietly is also what an exhausted list does, and the
    caller cannot tell the two apart: it reads a short list as the whole list
    and never asks again. Being told is what lets a client decide.
    """
    return {
        "error_type": "pagination_stopped",
        "error_message": (
            f"LinkedIn did not keep offset {offset} (landed on {landed}), "
            "so the list stops at the results already read."
        ),
    }


def dropped_filters_section_error(names: list[str], landed: str) -> dict[str, str]:
    """The ``section_errors`` entry for filters LinkedIn did not keep.

    Reported rather than raised, and the results kept: they are broader than
    the caller asked for and still about the same keywords, so a location or
    a work type LinkedIn dropped costs relevance rather than correctness.
    Saying nothing is what cannot be defended, since a search for remote
    Python in Berlin then returns Python anywhere and reads as though Berlin
    had none.
    """
    return {
        "error_type": "filters_dropped",
        "error_message": (
            f"LinkedIn did not keep {', '.join(names)} (landed on {landed}), "
            "so the results are broader than the search asked for."
        ),
    }


# LinkedIn's offset stride in the search URL. It is NOT how many cards a
# page renders: a live search served 11 per navigation while advertising 25
# per page, so paging by this number skipped 13 of every 24 jobs. Only the
# "are we past the last page" check may use it.
RESULTS_PER_LINKEDIN_PAGE = 25

# The routes a job search may legitimately end on. `/jobs/search` is what the
# URL builder produces; `/jobs/search-results` is where LinkedIn's redesigned
# experience redirects it. Compared as parsed paths rather than as a prefix,
# because `/jobs/search?keywords=x` is the same route and puts a `?` where a
# prefix test wants the slash.
JOB_SEARCH_PATHS = frozenset({"/jobs/search", "/jobs/search-results"})


def route(target: str) -> tuple[str, str]:
    """Host and path, which is what identifies a LinkedIn page.

    Not the whole URL: LinkedIn appends `currentJobId` to the query of a
    search page by itself, measured across three live searches where neither
    the path nor the rest of the query moved. The host has to come along, or
    a redirect that keeps the path reads as no redirect at all.
    """
    parsed = urlparse(target)
    return parsed.netloc, parsed.path.rstrip("/")


def same_job_search(before: tuple[str, str], after: tuple[str, str]) -> bool:
    """Whether a route change is LinkedIn moving a search to its redesign.

    `/jobs/search/` 302s to `/jobs/search-results/` for accounts on the new
    experience. The destination is the same search: it keeps the keywords,
    honours `start`, and renders the same results, so treating the hop as a
    page replacement ended every such search on its first page.

    Only between those two, and only on one host. The point of the comparison
    around it is that a search which ends up somewhere else is not a search,
    and an account picker served in place of one moves the route exactly like
    this redirect does.
    """
    return (
        before[0] == after[0]
        and before[1] in JOB_SEARCH_PATHS
        and after[1] in JOB_SEARCH_PATHS
    )


# Scrolling is bounded per navigation and across a whole search, because
# max_pages reaches 10 and tool_timeout_seconds defaults to 180.
SCROLL_DEADLINE_MAX = 12.0
SCROLL_BUDGET_TOTAL = 60.0

# A cancelled tool returns nothing, so the search stops itself while there is
# still time to hand back what it has. Measured: ten navigations of a Paris
# developer search take 83s in total, 6.5s each, so this leaves the normal case
# untouched and only catches a run that is genuinely running out.
#
# This predicts, it does not guarantee. Only the decision to *start* a page is
# bounded; once started, a page runs to its own timeouts, and `goto` alone
# allows 30s. The reserve is what covers that gap, and it has three claims on
# it: the extraction and assembly after the last navigation, a page slower than
# every page before it, and the browser startup inside `get_ready_extractor`,
# which FastMCP is already timing before this budget begins. A page that
# overruns the reserve is still cancelled and still loses every page gathered.
# Bounding that too means handing the remaining budget down into navigation
# and the rate-limit retry; see #754 rather than the margin. Scrolling is
# already handed a deadline, so it takes what is left of this budget when
# that is less than its own cap.
#
# The timeout arrives as an argument because `get_config()` parses `sys.argv`
# on its first call, and a scraping path is the wrong place to discover that.
SEARCH_TIMEOUT_FRACTION = 0.8

SAVED_JOBS_URL = "https://www.linkedin.com/my-items/saved-jobs/"
# Where a saved-jobs navigation may legitimately end. LinkedIn redirects the
# first to the second and drops the query doing so, so the tool navigates to
# one and arrives at the other.
SAVED_JOBS_PATHS = frozenset({"/my-items/saved-jobs", "/jobs-tracker"})

# The my-items lists page in 10s, unlike job search. Verified live: ?start=10
# returns the 11th saved job, while ?start=25 lands past the end of a two-page
# list and yields nothing.
SAVED_JOBS_PAGE_SIZE = 10
