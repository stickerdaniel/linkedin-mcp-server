"""Cached, paced company enrichment -- firmographics and open roles.

Two levers, both entirely within LinkedIn, no third-party data:

* **Tier 1, search (cheap, batch):** one company-search page load surfaces
  ~10 companies with industry and location. ``enrich_companies`` walks a name
  list, serves anything already cached and fresh for free, and spends one
  paced action per search to cover the rest. This is the ~10x that turns a
  two-month job into days.

* **Tier 2, company page (dear, deep):** ``enrich_company_deep`` reads a single
  company's About tab for the exact headcount band and its open-roles count
  from LinkedIn's job search filtered by that company (NOT the Page's own
  /jobs/ tab, which is empty for most employers) -- the buying signal. Reserved
  for the short list of high-value targets, not the whole network.

Both are cache-first with the two TTLs in ``company_cache``: firmographics stay
fresh for 90 days, open roles for 14. Both draw down the single shared account
budget (``pacing.load_account_budget``) that the person enrichment also uses,
so a day's company research and a day's profile research cannot together exceed
one account-safety budget -- LinkedIn counts activity per account, not per job.

Note the two tiers deliver different depth: the search tier records a company's
LinkedIn URL and the raw results text but does not itself parse industry or
headcount (those live behind the company page). ``enrich_company_deep`` is what
fills the typed firmographic fields.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import os

from linkedin_mcp_server.company_cache import (
    DEFAULT_FIRMOGRAPHICS_TTL,
    DEFAULT_JOBS_TTL,
    CompanyCache,
    ttl_from_days,
)
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError, RateLimitError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    JobStore,
    load_account_budget,
    next_bunch_delay,
    step_delay,
)
from linkedin_mcp_server.scraping.company_parse import (
    parse_about,
    parse_job_search,
    parse_search_results,
)
from linkedin_mcp_server.scraping.extractor import _RATE_LIMITED_MSG

logger = logging.getLogger(__name__)

DEADLINE_FRACTION = 0.75


def register_company_enrichment_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the cached, paced company-enrichment tools."""

    # TTLs are configurable via env; the data layer stays pure, so the env
    # read happens here at the boundary (as with the rest of this server's
    # config) rather than inside CompanyCache.
    cache = CompanyCache(
        firmographics_ttl=ttl_from_days(
            os.environ.get(EnvironmentKeys.COMPANY_FIRMOGRAPHICS_TTL_DAYS),
            DEFAULT_FIRMOGRAPHICS_TTL,
        ),
        jobs_ttl=ttl_from_days(
            os.environ.get(EnvironmentKeys.COMPANY_JOBS_TTL_DAYS),
            DEFAULT_JOBS_TTL,
        ),
    )
    jobs = JobStore()

    @mcp.tool(
        timeout=tool_timeout,
        title="Enrich Companies (search)",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"company", "bulk", "search"},
        exclude_args=["extractor"],
    )
    async def enrich_companies(
        company_names: list[str],
        ctx: Context,
        bunch_searches: Annotated[int, Field(ge=1, le=20)] = 8,
        refresh: bool = False,
        ignore_schedule: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Firmographics for a list of companies, cache-first and paced.

        Serves any company whose firmographics are already cached and still
        fresh (90-day TTL) without touching LinkedIn. For the rest it spends
        one paced company-search per name -- each search page also reveals
        other companies, which are cached in passing, so overlapping names get
        cheaper as you go.

        Stops early, persisting everything, when the bunch is done, the shared
        rolling-24h action budget is spent, the working window closes, the tool
        deadline nears, or LinkedIn rate-limits.

        Args:
            company_names: Company names or LinkedIn company URLs.
            bunch_searches: Max LinkedIn searches to run this call (1-20,
                default 8). Cache hits do not count toward it.
            refresh: Re-fetch even companies whose cache is still fresh.
            ignore_schedule: Run outside working hours (off by default).

        Returns:
            Per-company firmographics gathered or served from cache, plus how
            many searches were spent and when to call again.
        """
        if not company_names:
            raise ToolError("company_names is empty.")

        now = datetime.now().astimezone()
        budget = load_account_budget(jobs, now)

        # "Already resolved" for the search tier means we hold something worth
        # not re-searching: the company's LinkedIn URL, or fresh firmographics
        # from a deep fetch. (Search itself never yields firmographics, only
        # the URL, so keying serve-from-cache on firmographics alone would
        # re-search every resolved company forever.)
        def _resolved(rec: Any) -> bool:
            return rec is not None and bool(
                rec.linkedin_url
                or rec.firmographics_fresh(now, cache.firmographics_ttl)
            )

        served: dict[str, dict] = {}
        to_fetch: list[str] = []
        for name in company_names:
            if not name.strip():
                continue
            rec = cache.get(name)
            if not refresh and _resolved(rec):
                served[name] = _firmographics_view(rec, "cache")
            else:
                to_fetch.append(name)

        if not to_fetch:
            return {
                "served_from_cache": len(served),
                "fetched": 0,
                "results": served,
                "stopped_because": "all_cached",
            }

        if not ignore_schedule and not budget.schedule.is_open(now):
            opens = budget.schedule.next_open(now)
            return _paced_return(
                served,
                0,
                "outside_working_hours",
                (opens - now).total_seconds(),
                detail=f"Working window reopens {opens.isoformat(timespec='minutes')}.",
            )

        remaining = budget.remaining_today(now)
        if remaining <= 0:
            return _paced_return(
                served,
                0,
                "daily_budget_spent",
                budget.ledger.next_expiry(now),
                detail="Shared 24h action budget spent; refills gradually.",
            )

        extractor = extractor or await get_ready_extractor(
            ctx, tool_name="enrich_companies"
        )
        rng = random.Random()
        deadline = asyncio.get_running_loop().time() + tool_timeout * DEADLINE_FRACTION
        spent = 0
        stopped = "bunch_complete"

        # `bunch_searches` bounds the number of *searches actually run*, not the
        # number of names looked at: a name resolved for free from the cache (or
        # in passing by an earlier search this call) must not burn a search
        # slot. So iterate the whole to_fetch list and stop once `spent` reaches
        # the bunch limit, the deadline nears, or the budget can't afford one
        # more search.
        for name in to_fetch:
            if spent >= bunch_searches:
                break
            if asyncio.get_running_loop().time() >= deadline:
                stopped = "tool_deadline"
                break

            # A prior search this call may already have resolved this name in
            # passing (its URL got cached); serve it free, no search spent.
            rec = cache.get(name)
            if not refresh and _resolved(rec):
                served[name] = _firmographics_view(rec, "cache")
                continue

            if budget.remaining_today(now) <= 0:
                stopped = "daily_budget_spent"
                break

            try:
                result = await extractor.search_companies(name)
            except RateLimitError as e:
                logger.warning("Rate limited during company enrichment: %s", e)
                jobs.save(budget)
                return _paced_return(
                    served,
                    spent,
                    "rate_limited",
                    max(budget.ledger.next_expiry(now), 3600.0),
                    detail="LinkedIn rate-limited; progress saved. Wait ~1h.",
                )
            except Exception as e:
                logger.info("Company search failed for %s: %s", name, e)
                served[name] = {"status": "search_failed", "error": str(e)[:160]}
                budget.ledger.record(now)  # the page load still happened
                spent += 1
                jobs.save(budget)
                continue

            budget.ledger.record(now)
            spent += 1
            jobs.save(budget)

            text = result.get("sections", {}).get("search_results", "")
            refs = result.get("references", {}).get("search_results", [])
            hits = parse_search_results(refs)

            # Cache every company the results page revealed, so overlapping
            # names later in the list become free hits. Store only the URL --
            # NOT the results-page text: that blob is the whole page (all ~10
            # companies), not this company's About, so persisting a copy on
            # every record would bloat the cache and mislead. The page text is
            # still returned at call level below for the LLM to read.
            for hit in hits:
                cache.record_firmographics(
                    hit["name"],
                    now,
                    source="search",
                    linkedin_url=hit["url"],
                )
            # Attribute a hit to the requested name only when it actually
            # matches: cache.get(name) normalises both sides and hits iff one
            # of the just-cached companies shares the query's normalised key.
            # Never fall back to hits[0] -- the top result for "Deloitte
            # Digital" may be "Deloitte", a different company. On no match,
            # hand back the candidates and the raw page for the LLM to judge.
            rec = cache.get(name)
            if rec is not None and rec.linkedin_url:
                served[name] = _firmographics_view(rec, "search", fallback_raw=text)
            else:
                served[name] = {
                    "status": "no_confident_match",
                    "source": "search",
                    "candidates": [h["name"] for h in hits[:10]],
                    "raw_about": text,
                }

            now = datetime.now().astimezone()
            await ctx.report_progress(
                progress=spent,
                total=bunch_searches,
                message=f"{spent} searched, {len(served)} known",
            )
            if spent < bunch_searches:
                await asyncio.sleep(step_delay(rng=rng))

        now = datetime.now().astimezone()
        remaining = budget.remaining_today(now)
        outstanding = sum(1 for n in to_fetch if not _resolved(cache.get(n)))
        if remaining <= 0:
            stopped, wait = "daily_budget_spent", budget.ledger.next_expiry(now)
        elif outstanding == 0:
            stopped, wait = "all_done", None
        else:
            wait = next_bunch_delay(
                remaining, bunch_searches, now, budget.schedule, rng
            )
        jobs.save(budget)
        return _paced_return(served, spent, stopped, wait)

    @mcp.tool(
        timeout=tool_timeout,
        title="Enrich Company (deep)",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"company", "scraping"},
        exclude_args=["extractor"],
    )
    async def enrich_company_deep(
        company: str,
        ctx: Context,
        include_jobs: bool = True,
        refresh: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Deep firmographics plus live open roles for one company.

        Reads the company's About tab (exact headcount band, HQ, website, and
        the numeric company id) and, by default, its live open-roles count from
        LinkedIn's job search filtered by that company -- the active-investment
        signal. (The company Page's own /jobs/ tab is NOT used: it lists only
        roles posted directly on the Page, so it reads "no jobs" even for
        employers hiring thousands.) Cache-first: a company whose firmographics
        are fresh (<90d) and whose jobs are fresh (<14d) returns without any
        page load. Costs one action per surface actually fetched.

        Reserve this for the high-value short list (top companies by contacts
        or decision makers). For breadth, use enrich_companies instead.

        Args:
            company: Company name, slug, or LinkedIn company URL.
            include_jobs: Also read the Jobs tab (default True). Open roles
                carry a shorter 14-day TTL, so they refresh more often.
            refresh: Re-fetch both halves even if cached and fresh.

        Returns:
            The company's cached-and-updated record, with per-half freshness.
        """
        now = datetime.now().astimezone()
        rec = cache.get(company)

        want_firmographics = refresh or cache.needs_firmographics(company, now)
        want_jobs = include_jobs and (refresh or cache.needs_jobs(company, now))
        if not want_firmographics and not want_jobs:
            return {
                "company": company,
                "status": "cache_fresh",
                **_firmographics_view(rec, "cache"),
            }

        budget = load_account_budget(jobs, now)
        needed = int(want_firmographics) + int(want_jobs)
        if budget.remaining_today(now) < needed:
            return {
                "company": company,
                "status": "daily_budget_spent",
                "next_run_after_seconds": round(budget.ledger.next_expiry(now)),
                **_firmographics_view(rec, "cache"),
            }

        extractor = extractor or await get_ready_extractor(
            ctx, tool_name="enrich_company_deep"
        )
        slug = _slug(company)
        urn = rec.company_urn if rec else ""

        try:
            # Firmographics from the About tab; also yields the numeric company
            # URN, which the open-roles lookup below is keyed on.
            if want_firmographics:
                result = await extractor.scrape_company(slug, {"about"})
                secs = result.get("sections", {})
                about = secs.get("about", "")
                fields = parse_about(about)
                urn = _company_urn(result) or urn
                cache.record_firmographics(
                    company,
                    now,
                    source="company_page",
                    industry=fields.get("industry", ""),
                    employee_count=fields.get("employee_count", ""),
                    headquarters=fields.get("headquarters", ""),
                    website=fields.get("website", ""),
                    linkedin_url=result.get("url", ""),
                    company_urn=urn,
                    raw_about=about,
                )
                budget.ledger.record(now)

            # Open roles come from job SEARCH filtered by the company URN -- the
            # company Page's own /jobs/ tab is empty for most employers. Needs
            # the URN, so a jobs-only refresh relies on the one cached earlier.
            if want_jobs and urn:
                jobs_url = (
                    f"https://www.linkedin.com/jobs/search/?f_C={urn}&geoId=92000000"
                )
                extracted = await extractor.extract_page(jobs_url, section_name="jobs")
                text = extracted.text or ""
                # extract_page can hand back an error section or the soft
                # rate-limit sentinel *without raising*. Caching that would
                # stamp jobs fresh for the TTL and serve a failed lookup as
                # real data. Only record on a genuine page; the load happened
                # either way, so it still costs a budget action, and the jobs
                # half stays stale so the next call retries.
                if text and text != _RATE_LIMITED_MSG and not extracted.error:
                    parsed = parse_job_search(text)
                    cache.record_jobs(
                        company,
                        now,
                        count=parsed.count,
                        sample=parsed.sample,
                        raw_jobs=text,
                    )
                budget.ledger.record(now)
        except RateLimitError:
            jobs.save(budget)
            return {
                "company": company,
                "status": "rate_limited",
                "next_run_after_seconds": 3600,
                **_firmographics_view(cache.get(company) or rec, "cache"),
            }
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "enrich_company_deep")
        except Exception as e:
            raise_tool_error(e, "enrich_company_deep")  # NoReturn

        jobs.save(budget)
        out = {
            "company": company,
            "status": "fetched",
            **_firmographics_view(cache.get(company), "company_page"),
        }
        if want_jobs and not urn:
            out["jobs_note"] = (
                "Open roles unavailable: no company URN known (fetch "
                "firmographics first, or the About page exposed no id)."
            )
        return out

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Company Cache",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"company", "bulk"},
    )
    async def get_company_cache(company: str | None = None) -> dict[str, Any]:
        """
        Read a cached company record, or list everything cached.

        Args:
            company: Company name/slug/URL to read. Omit to list all keys.
        """
        if company is None:
            return {"cached_companies": cache.list_keys()}
        rec = cache.get(company)
        if rec is None:
            return {"company": company, "status": "not_cached"}
        now = datetime.now().astimezone()
        view = _firmographics_view(rec, rec.firmographics_source or "cache")
        view["firmographics_fresh"] = rec.firmographics_fresh(
            now, cache.firmographics_ttl
        )
        view["jobs_fresh"] = rec.jobs_fresh(now, cache.jobs_ttl)
        return {"company": company, "status": "cached", **view}


def _slug(company: str) -> str:
    """A /company/<slug> value from a name, slug, or full URL."""
    c = company.strip().rstrip("/")
    if "/company/" in c:
        c = c.split("/company/", 1)[1].split("/")[0].split("?")[0]
    return c


def _company_urn(result: dict[str, Any]) -> str:
    """The numeric company id from an About scrape's references, if present.

    The About page carries a ``company_urn`` reference whose ``value`` is the
    id LinkedIn uses in the ``currentCompany``/``f_C`` facets -- what the
    open-roles job search is keyed on.
    """
    for ref in result.get("references", {}).get("about", []):
        if ref.get("kind") == "company_urn" and ref.get("value"):
            return str(ref["value"])
    return ""


def _firmographics_view(
    rec: Any, source: str, fallback_raw: str = ""
) -> dict[str, Any]:
    if rec is None:
        return {"status": "unknown", "source": source, "raw_about": fallback_raw}
    return {
        "display_name": rec.display_name,
        "industry": rec.industry,
        "employee_count": rec.employee_count,
        "headquarters": rec.headquarters,
        "website": rec.website,
        "linkedin_url": rec.linkedin_url,
        "open_roles_count": rec.open_roles_count,
        "open_roles_sample": rec.open_roles_sample,
        "source": source,
        "firmographics_fetched_at": rec.firmographics_fetched_at,
        "jobs_fetched_at": rec.jobs_fetched_at,
    }


def _paced_return(
    served: dict[str, dict],
    spent: int,
    stopped: str,
    wait: float | None,
    detail: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "fetched": spent,
        "results": served,
        "known": len(served),
        "stopped_because": stopped,
    }
    if wait is not None:
        out["next_run_after_seconds"] = round(wait)
    if detail:
        out["detail"] = detail
    return out
