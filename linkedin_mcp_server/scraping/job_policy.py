"""Routing, paging and reporting policy for the job workflows."""

from __future__ import annotations

from ipaddress import IPv6Address, ip_address
from typing import Literal
from urllib.parse import parse_qs, urlparse

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


def label_similar_jobs(references: list[Reference], job_id: str) -> list[Reference]:
    """Mark every job a posting links to, other than itself, as a similar job.

    Compared by id rather than by where the link sits, so the posting's own
    apply link keeps the section's context and the "More jobs" cards do not
    read as the posting.
    """
    own_url = f"/jobs/view/{job_id}/"
    out: list[Reference] = []
    for ref in references:
        if ref["kind"] == "job" and ref["url"] != own_url:
            ref = Reference(**ref)
            ref["context"] = "similar job"
        out.append(ref)
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


def no_matching_jobs_section_error(keywords: str) -> dict[str, str]:
    """The ``section_errors`` entry for a search LinkedIn found nothing for.

    LinkedIn answers it with unrelated postings on the same route and query,
    and those came back as `job_ids`. They are dropped, and the empty list is
    explained, because an empty list alone also describes a page that did not
    render.
    """
    return {
        "error_type": "no_matching_jobs",
        "error_message": (
            f"LinkedIn found no jobs matching {keywords!r} and showed unrelated "
            "recommendations instead, so none are returned."
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


# The two filters the redesigned route reads out of the query's words instead
# of its parameters. Every other filter it drops (`f_JT`, `f_E`, `f_EA`,
# `sortBy`) has no such second home, so naming the keywords for one of those
# would offer a repair that does not exist.
_KEYWORD_FILTERS = frozenset({"location", "f_WT"})


def dropped_filters_section_error(names: list[str], landed: str) -> dict[str, str]:
    """The ``section_errors`` entry for filters LinkedIn did not keep.

    Reported rather than raised, and the results kept: they are broader than
    the caller asked for and still about the same keywords, so a location or
    a work type LinkedIn dropped costs relevance rather than correctness.
    Saying nothing is what cannot be defended, since a search for remote
    Python in Berlin then returns Python anywhere and reads as though Berlin
    had none.

    The redesigned route keeps only the keywords, `f_TPR` and `start`, and
    reads a location and a work type from the words of the query instead, so
    there the message says where they go. Measured: "remote forward deployed
    engineer in France" returned remote postings in France. Only when one of
    those two is what was dropped: the route drops the rest as well, and a
    search that lost its job type is told to rewrite keywords that cannot
    carry one.
    """
    message = (
        f"LinkedIn did not keep {', '.join(names)} (landed on {landed}), "
        "so the results are broader than the search asked for."
    )
    if route(landed)[1] == "/jobs/search-results" and _KEYWORD_FILTERS & set(names):
        message += (
            " Its redesigned search reads location and work type from the "
            'keywords instead, as in "remote python developer in France".'
        )
    return {"error_type": "filters_dropped", "error_message": message}


def missing_description_section_error() -> dict[str, str]:
    """Report an absent description heading without discarding captured text."""
    return {
        "error_type": "description_missing",
        "error_message": (
            "The captured posting text has no recognized description heading "
            "and may be incomplete. The text was kept; calling again may "
            "return more."
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
# The timeout arrives as an argument so this budget matches the timeout the
# server registered for the tool, including directly constructed servers.
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

ApplyType = Literal["easy_apply", "external", "applied", "closed", "unknown"]

# LinkedIn's interstitial for links that leave the site, with the destination in
# its `url` parameter. Measured on 2026-09-14: an external posting's Apply opens
# a "Share your profile?" dialog whose Continue link is
# `/safety/go/?url=https%3A%2F%2Fgrnh.se%2F...`.
SAFETY_REDIRECT_PATH = "/safety/go"


# Name suffixes reserved for a host's own network, which no registry serves.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def reaches_the_public_internet(host: str) -> bool:
    """Whether an apply destination names somewhere outside this host.

    The destination of an external Apply is chosen by whoever posted the job,
    and the server loads it in the browser to follow its redirects. A loopback,
    link-local or private-range address would turn that into a request against
    whatever the host or its container can reach, on a stranger's word. None of
    those is an employer's site, so they answer as no address at all.

    Judged on the address as written, which is what this module can see. A
    public name that resolves into private space is the browser's resolution to
    make and is not caught here, and neither is a redirect into one: a route
    sees only the first request of a redirect (measured, `test_job_apply_dom`).
    What bounds the rest is that a destination is only ever loaded, never read:
    the tool answers with an address, so a page fetched this way returns
    nothing to its caller.
    """
    name = host.rstrip(".").lower()
    try:
        address = ip_address(name)
    except ValueError:
        # Not a literal `ipaddress` accepts. A name's rightmost label is never
        # all digits, so one that is belongs to an address written the long way
        # round (`0177.0.0.1`, `0x7f.0.0.1`), which the browser still resolves
        # to the loopback.
        label = name.rpartition(".")[2]
        if not label or label.isdigit():
            return False
        # A single-label name has no public registry behind it.
        return "." in name and not name.endswith(_PRIVATE_SUFFIXES)
    if isinstance(address, IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def employer_apply_url(href: str) -> str | None:
    """The employer's address an apply link leads to, or None.

    The interstitial answers with its destination, and any other address off
    LinkedIn answers as itself. A LinkedIn page that is not the interstitial is
    not the employer's site, so it answers None rather than passing for one.
    An address that never leaves this host is refused the same way.
    """
    parsed = urlparse(href)
    host = parsed.hostname
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if not reaches_the_public_internet(host):
        return None
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return href
    if parsed.path.rstrip("/") != SAFETY_REDIRECT_PATH:
        return None
    destination = parse_qs(parsed.query).get("url", [""])[0]
    return employer_apply_url(destination) if destination else None


def apply_link_missing_section_error() -> dict[str, str]:
    """The ``section_errors`` entry for an external Apply that led nowhere.

    The posting is still external, which is worth keeping, but a type with no
    link and nothing beside it reads as a posting that has none. Being told is
    what lets a caller open the posting itself.
    """
    return {
        "error_type": "apply_link_missing",
        "error_message": (
            "Apply was clicked but LinkedIn opened neither its dialog nor a tab, "
            "so the employer's link could not be read."
        ),
    }
