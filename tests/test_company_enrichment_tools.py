"""Tests for the cached, paced company-enrichment tools.

The cache and parsers are covered in test_company_cache.py; these cover the
tool loop: cache-first behaviour, the search batch lever, the deep jobs fetch,
and that a rate limit never loses progress.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP

from linkedin_mcp_server.company_cache import CompanyCache
from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.pacing import Job, JobStore, Ledger, Schedule

from test_tools import get_tool_fn

# Always open: no weekend, no lunch, so tests never depend on the wall clock.
OPEN_ALL = Schedule(
    work_start=0, work_end=23, days_off=(), lunch_start=None, lunch_end=None
)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point both the company cache and the shared budget store at tmp_path."""
    import linkedin_mcp_server.tools.company_enrichment as ce

    cache = CompanyCache(tmp_path / "companies")
    jobs = JobStore(tmp_path / "jobs")
    monkeypatch.setattr(ce, "CompanyCache", lambda *a, **k: cache)
    monkeypatch.setattr(ce, "JobStore", lambda *a, **k: jobs)

    # A shared budget whose schedule is always open, so tests do not depend on
    # the wall clock's hour.
    budget = Job(
        name=ce.BUDGET_JOB,
        started_on=datetime(2020, 1, 1).date(),
        warmup=False,
        daily_cap=100,
        schedule=OPEN_ALL,
    )
    jobs.save(budget)
    return cache, jobs


@pytest.fixture
def mcp(wired):
    from linkedin_mcp_server.tools.company_enrichment import (
        register_company_enrichment_tools,
    )

    server = FastMCP("test")
    register_company_enrichment_tools(server)
    return server


def _search_extractor(hits):
    """A mock whose search_companies returns the given company references."""
    mock = MagicMock()
    refs = [
        {"url": f"https://www.linkedin.com/company/{s}", "text": s.title()}
        for s in hits
    ]
    mock.search_companies = AsyncMock(
        return_value={
            "sections": {"search_results": "1-50 employees\nSoftware"},
            "references": {"search_results": refs},
        }
    )
    return mock


class TestEnrichCompanies:
    async def test_serves_fresh_cache_without_touching_linkedin(
        self, mcp, wired, mock_context
    ):
        cache, _ = wired
        now = datetime.now().astimezone()
        cache.record_firmographics(
            "Copado", now, source="company_page", industry="Software"
        )
        extractor = _search_extractor([])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, extractor=extractor)

        assert out["served_from_cache"] == 1
        assert out["fetched"] == 0
        extractor.search_companies.assert_not_awaited()

    async def test_searches_for_a_miss_and_caches_the_page(
        self, mcp, wired, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, _ = wired
        # One search page reveals three companies; all three should be cached.
        extractor = _search_extractor(["copado", "gearset", "flosum"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, extractor=extractor)

        assert out["fetched"] == 1
        assert cache.get("Copado") is not None
        # Companies seen in passing are cached too -> future free hits.
        assert cache.get("Gearset") is not None
        assert cache.get("Flosum") is not None

    async def test_second_name_on_the_same_page_is_a_free_hit(
        self, mcp, wired, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, _ = wired
        extractor = _search_extractor(["copado", "gearset"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        # Gearset was revealed by Copado's search, so only one search runs.
        out = await fn(["Copado", "Gearset"], mock_context, extractor=extractor)

        assert out["fetched"] == 1
        assert extractor.search_companies.await_count == 1

    async def test_stops_when_shared_budget_is_spent(self, mcp, wired, mock_context):
        cache, jobs = wired
        import linkedin_mcp_server.tools.company_enrichment as ce

        now = datetime.now().astimezone()
        budget = jobs.load(ce.BUDGET_JOB)
        budget.daily_cap = 3
        budget.ledger = Ledger(actions=[now.timestamp()] * 3)
        jobs.save(budget)

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["NewCo"], mock_context, extractor=_search_extractor([]))

        assert out["stopped_because"] == "daily_budget_spent"
        assert out["fetched"] == 0

    async def test_rate_limit_saves_progress(self, mcp, wired, mock_context):
        extractor = MagicMock()
        extractor.search_companies = AsyncMock(side_effect=RateLimitError("slow down"))

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["NewCo"], mock_context, extractor=extractor)

        assert out["stopped_because"] == "rate_limited"
        assert out["next_run_after_seconds"] >= 3600

    async def test_empty_input_rejected(self, mcp, mock_context):
        from fastmcp.exceptions import ToolError

        fn = await get_tool_fn(mcp, "enrich_companies")
        with pytest.raises(ToolError, match="empty"):
            await fn([], mock_context)


class TestEnrichCompanyDeep:
    def _deep_extractor(self):
        mock = MagicMock()
        mock.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/acme/",
                "sections": {
                    "about": (
                        "Acme\nIndustry\nRetail\n"
                        "Company size\n1,001-5,000 employees\n"
                        "Headquarters\nCairo, Egypt\n"
                    ),
                    "jobs": "12 open jobs\nSalesforce Administrator\nRevOps Manager\n",
                },
            }
        )
        return mock

    async def test_fetches_about_and_jobs_and_caches_both(
        self, mcp, wired, mock_context
    ):
        cache, _ = wired
        extractor = self._deep_extractor()

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["status"] == "fetched"
        assert out["industry"] == "Retail"
        assert out["employee_count"] == "1,001-5,000 employees"
        assert out["open_roles_count"] == 12
        # Both halves persisted.
        rec = cache.get("Acme")
        assert rec.has_firmographics() and rec.has_jobs()

    async def test_cache_fresh_skips_the_fetch(self, mcp, wired, mock_context):
        cache, _ = wired
        now = datetime.now().astimezone()
        cache.record_firmographics(
            "Acme", now, source="company_page", industry="Retail"
        )
        cache.record_jobs("Acme", now, count=5, sample=["X"])
        extractor = self._deep_extractor()

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["status"] == "cache_fresh"
        extractor.scrape_company.assert_not_awaited()

    async def test_stale_jobs_refetch_only_jobs(self, mcp, wired, mock_context):
        """Firmographics fresh, jobs stale -> only the Jobs tab is requested."""
        cache, _ = wired
        now = datetime.now().astimezone()
        cache.record_firmographics(
            "Acme", now, source="company_page", industry="Retail"
        )
        # Jobs recorded 30 days ago -> past the 14-day TTL.
        old = (now - timedelta(days=30)).isoformat()
        rec = cache.get("Acme")
        rec.open_roles_count = 5
        rec.jobs_fetched_at = old
        cache.save(rec)

        extractor = self._deep_extractor()
        fn = await get_tool_fn(mcp, "enrich_company_deep")
        await fn("Acme", mock_context, extractor=extractor)

        called_sections = extractor.scrape_company.await_args.args[1]
        assert "jobs" in called_sections
        assert "about" not in called_sections

    async def test_include_jobs_false_skips_jobs(self, mcp, wired, mock_context):
        cache, _ = wired
        extractor = self._deep_extractor()

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        await fn("Acme", mock_context, include_jobs=False, extractor=extractor)

        called_sections = extractor.scrape_company.await_args.args[1]
        assert "about" in called_sections
        assert "jobs" not in called_sections


class TestGetCompanyCache:
    async def test_lists_and_reads(self, mcp, wired):
        cache, _ = wired
        now = datetime.now().astimezone()
        cache.record_firmographics("Acme", now, source="search", industry="Retail")

        fn = await get_tool_fn(mcp, "get_company_cache")
        listing = await fn()
        assert "acme" in listing["cached_companies"]

        detail = await fn("Acme")
        assert detail["status"] == "cached"
        assert detail["industry"] == "Retail"
        assert detail["firmographics_fresh"] is True

    async def test_unknown_company(self, mcp, wired):
        fn = await get_tool_fn(mcp, "get_company_cache")
        assert (await fn("Nope"))["status"] == "not_cached"
