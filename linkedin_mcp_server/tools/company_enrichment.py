"""Cached, paced company enrichment -- firmographics and open roles.

Two levers, both entirely within LinkedIn, no third-party data:

* **Tier 1, search (cheap, batch):** one company-search page load surfaces
  ~10 companies with industry and location. ``enrich_companies`` walks a name
  list, serves anything already cached and fresh for free, and spends one
  paced action per search to cover the rest. This is the ~10x that turns a
  two-month job into days.

* **Tier 2, company page (dear, deep):** ``enrich_company_deep`` opens a single
  company's About and Jobs tabs for the exact headcount band and its live open
  roles -- the buying signal. Reserved for the short list of high-value targets
  the summary surfaces, not the whole network.

Both are cache-first with the two TTLs in ``company_cache``: firmographics stay
fresh for 90 days, open roles for 14. Both count their LinkedIn page loads
against the same rolling-24h ``Ledger`` the person enrichment uses, so a day's
company research and a day's profile research share one safety budget.
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
    DEFAULT_DAILY_ACTIONS,
    Job,
    JobStore,
    next_bunch_delay,
    step_delay,
)
from linkedin_mcp_server.scraping.company_parse import (
    parse_about,
    parse_jobs,
    parse_search_results,
)

logger = logging.getLogger(__name__)

DEADLINE_FRACTION = 0.75

# The action ledger is shared with person enrichment through one well-known
# job name, so both kinds of work draw down a single rolling-24h budget.
BUDGET_JOB = "__action_budget__"


def _budget_job(store: JobStore, now: datetime) -> Job:
    """Load (or start) the shared action-budget ledger."""
    if store.exists(BUDGET_JOB):
        return store.load(BUDGET_JOB)
    return Job(
        name=BUDGET_JOB,
        started_on=now.date(),
        daily_cap=DEFAULT_DAILY_ACTIONS,
        warmup=False,  # a long-lived shared budget is not a fresh account
    )


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
        annotations={"readOnlyHint": True, "openWorldHint": True},
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
        budget = _budget_job(jobs, now)

        served: dict[str, dict] = {}
        to_fetch: list[str] = []
        for name in company_names:
            if not name.strip():
                continue
            rec = cache.get(name)
            if (
                not refresh
                and rec is not None
                and rec.firmographics_fresh(now, cache.firmographics_ttl)
            ):
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
        planned = min(bunch_searches, remaining, len(to_fetch))
        spent = 0
        stopped = "bunch_complete"

        for i in range(planned):
            if asyncio.get_running_loop().time() >= deadline:
                stopped = "tool_deadline"
                break
            name = to_fetch[i]

            # A prior search this call may already have cached this name in
            # passing; skip the redundant fetch.
            rec = cache.get(name)
            if (
                not refresh
                and rec is not None
                and rec.firmographics_fresh(now, cache.firmographics_ttl)
            ):
                served[name] = _firmographics_view(rec, "cache")
                continue

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
            # names later in the list become free hits.
            for hit in hits:
                cache.record_firmographics(
                    hit["name"],
                    now,
                    source="search",
                    linkedin_url=hit["url"],
                    raw_about=text,
                )
            # The requested name is whichever hit best matches, else the first.
            served[name] = _firmographics_view(
                cache.get(name) or (cache.get(hits[0]["name"]) if hits else None),
                "search",
                fallback_raw=text,
            )

            now = datetime.now().astimezone()
            await ctx.report_progress(
                progress=i + 1,
                total=planned,
                message=f"{spent} searched, {len(served)} known",
            )
            if i < planned - 1:
                await asyncio.sleep(step_delay(rng=rng))

        now = datetime.now().astimezone()
        remaining = budget.remaining_today(now)
        outstanding = sum(1 for n in to_fetch if cache.needs_firmographics(n, now))
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
        annotations={"readOnlyHint": True, "openWorldHint": True},
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

        Opens the company's About tab (exact headcount band, HQ, website) and,
        by default, its Jobs tab (count and a sample of open roles -- the
        active-investment signal). Cache-first: a company whose firmographics
        are fresh (<90d) and whose jobs are fresh (<14d) returns without any
        page load. Costs one action per tab actually fetched.

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

        budget = _budget_job(jobs, now)
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
        sections: set[str] = set()
        if want_firmographics:
            sections.add("about")
        if want_jobs:
            sections.add("jobs")

        try:
            result = await extractor.scrape_company(slug, sections)
        except RateLimitError:
            return {
                "company": company,
                "status": "rate_limited",
                "next_run_after_seconds": 3600,
                **_firmographics_view(rec, "cache"),
            }
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "enrich_company_deep")
        except Exception as e:
            raise_tool_error(e, "enrich_company_deep")  # NoReturn

        secs = result.get("sections", {})
        if want_firmographics and "about" in secs:
            fields = parse_about(secs["about"])
            cache.record_firmographics(
                company,
                now,
                source="company_page",
                industry=fields.get("industry", ""),
                employee_count=fields.get("employee_count", ""),
                headquarters=fields.get("headquarters", ""),
                website=fields.get("website", ""),
                linkedin_url=result.get("url", ""),
                raw_about=secs["about"],
            )
            budget.ledger.record(now)

        if want_jobs and "jobs" in secs:
            parsed = parse_jobs(secs["jobs"])
            cache.record_jobs(
                company,
                now,
                count=parsed.count,
                sample=parsed.sample,
                raw_jobs=secs["jobs"],
            )
            budget.ledger.record(now)

        jobs.save(budget)
        return {
            "company": company,
            "status": "fetched",
            **_firmographics_view(cache.get(company), "company_page"),
        }

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
