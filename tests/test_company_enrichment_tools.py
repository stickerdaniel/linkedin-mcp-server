"""Tests for the cached, paced company-enrichment tools.

The cache and parsers are covered in test_company_cache.py; these cover the
tool loop: cache-first behaviour, the search batch lever, the deep jobs fetch,
and that a rate limit never loses progress.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from patchright._impl._errors import TargetClosedError
from patchright.async_api import Error as PatchrightError

from linkedin_mcp_server.company_cache import CompanyCache
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.core.exceptions import AuthenticationError, RateLimitError
from linkedin_mcp_server.exceptions import (
    AuthenticationStartedError,
    BrowserBusyError,
)
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    Job,
    JobStore,
    Ledger,
    Schedule,
    request_arrived_at,
)
from linkedin_mcp_server.tools.enrichment import RETRY_AFTER_QUEUED_OUT

from test_company_cache import _NOT_FOUND_LIVE
from test_search_parse import COMPANY_PAGE
from test_tools import get_tool_fn

# Always open: no weekend, no lunch, so tests never depend on the wall clock.
OPEN_ALL = Schedule(work_start=0, work_end=24, days_off=())


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
        name=ACCOUNT_BUDGET_JOB,
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


_ABOUT_TEXT = (
    "Acme\nIndustry\nRetail\n"
    "Company size\n1,001-5,000 employees\n"
    "Headquarters\nCairo, Egypt\n"
    "Founded\n1999\n"
    "Company type\nPrivately Held\n"
    "Specialties\nWidgets, Gadgets\n"
)


def _about_result(slug="acme"):
    return {
        "url": f"https://www.linkedin.com/company/{slug}/",
        "sections": {"about": _ABOUT_TEXT},
        "references": {
            "about": [
                {
                    "kind": "company_urn",
                    "url": "/search/results/people/?currentCompany=%5B%229999%22%5D",
                    "value": "9999",
                }
            ]
        },
    }


def _search_extractor(hits):
    """A mock whose search_companies returns the given company references,
    and whose scrape_company serves one About page for any slug."""
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
    mock.scrape_company = AsyncMock(
        side_effect=lambda slug, sections: _about_result(slug)
    )
    return mock


def _spent(jobs):
    return jobs.load(ACCOUNT_BUDGET_JOB).ledger.spent(datetime.now().astimezone())


CLOSED_TARGET = "Target page, context or browser has been closed"

# The incident shape: the scraper swallowed a dead browser into section_errors
# for every section and returned with nothing loaded.
NOTHING_LOADED = {
    "url": "https://www.linkedin.com/company/copado/",
    "sections": {},
    "section_errors": {
        "about": {"error_type": "scraping", "error_message": CLOSED_TARGET}
    },
}


@pytest.fixture(
    params=[
        NOTHING_LOADED,
        TargetClosedError(CLOSED_TARGET),
        PatchrightError(CLOSED_TARGET),
    ],
    ids=["empty-result", "TargetClosedError", "Error-with-closed-message"],
)
def dead(request):
    """A dead-browser answer in each shape scrape_company / search_companies
    can give: the swallowed result or a raised error."""
    return request.param


def _mock_of(failure):
    if isinstance(failure, Exception):
        return AsyncMock(side_effect=failure)
    return AsyncMock(return_value=failure)


def _relaunch_to(monkeypatch, extractor):
    """Fake the re-acquisition a dead browser triggers, handing back the
    given extractor; returns the mock so the test can count relaunches."""
    relaunch = AsyncMock(return_value=extractor)
    monkeypatch.setattr(
        "linkedin_mcp_server.tools.company_enrichment.get_ready_extractor", relaunch
    )
    return relaunch


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

    async def test_a_free_hit_does_not_consume_a_search_slot(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """bunch_searches bounds searches run, not names looked at: a name
        resolved for free (in passing) must not use up a slot that a later
        unresolved name needs."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, _ = wired

        # Searching "a" reveals a+b (b becomes a free hit); "c" is its own page.
        def per_name(name):
            slugs = {"a": ["a", "b"], "c": ["c"]}.get(name.lower(), [name.lower()])
            refs = [
                {"url": f"https://www.linkedin.com/company/{s}", "text": s}
                for s in slugs
            ]
            return {
                "sections": {"search_results": "x"},
                "references": {"search_results": refs},
            }

        extractor = MagicMock()
        extractor.search_companies = AsyncMock(side_effect=lambda n: per_name(n))

        fn = await get_tool_fn(mcp, "enrich_companies")
        # bunch_searches=2. Old bug: a(search)+b(free) exhausts 2 slots, c
        # never tried. Fixed: b is free, so c still gets its search.
        out = await fn(
            ["a", "b", "c"], mock_context, bunch_searches=2, extractor=extractor
        )

        assert out["fetched"] == 2  # a and c searched; b free
        assert extractor.search_companies.await_count == 2
        assert set(out["results"]) == {"a", "b", "c"}  # all three resolved

    async def test_stops_when_shared_budget_is_spent(self, mcp, wired, mock_context):
        cache, jobs = wired
        now = datetime.now().astimezone()
        budget = jobs.load(ACCOUNT_BUDGET_JOB)
        budget.daily_cap = 3
        budget.ledger = Ledger(actions=[now.timestamp()] * 3)
        jobs.save(budget)

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["NewCo"], mock_context, extractor=_search_extractor([]))

        assert out["stopped_because"] == "daily_budget_spent"
        assert out["fetched"] == 0

    async def test_a_company_url_is_recorded_without_a_search(
        self, mcp, wired, mock_context
    ):
        """The docstring accepts a URL, and a URL already says what a search
        would find: the slug. Searching it anyway spent a navigation and,
        matched against the URL's own words, came back with no confident
        match for a company the caller had named exactly."""
        cache, jobs = wired
        extractor = MagicMock()
        extractor.search_companies = AsyncMock(return_value={"sections": {}})

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(
            ["https://www.linkedin.com/company/copado/"],
            mock_context,
            extractor=extractor,
        )

        extractor.search_companies.assert_not_awaited()
        assert out["fetched"] == 0
        assert out["results"]["copado"]["linkedin_url"] == (
            "https://www.linkedin.com/company/copado"
        )
        assert cache.get("copado").linkedin_url == (
            "https://www.linkedin.com/company/copado"
        )
        now = datetime.now().astimezone()
        assert jobs.load(ACCOUNT_BUDGET_JOB).ledger.spent(now) == 0

    async def test_bunch_searches_is_clamped_to_the_configured_ceiling(
        self, mcp, wired, mock_context, monkeypatch, caplog
    ):
        """``BUNCH_SEARCHES_MAX`` has to reach the tool, as ``BUNCH_SIZE_MAX``
        reaches run_enrichment_bunch; a ceiling only the docs know is none."""
        monkeypatch.setenv(EnvironmentKeys.BUNCH_SEARCHES_MAX, "2")
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        extractor = MagicMock()
        extractor.search_companies = AsyncMock(
            side_effect=lambda n: {
                "sections": {"search_results": "x"},
                "references": {
                    "search_results": [
                        {"url": f"https://www.linkedin.com/company/{n}", "text": n}
                    ]
                },
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        with caplog.at_level(logging.INFO):
            out = await fn(
                ["a", "b", "c", "d"],
                mock_context,
                bunch_searches=5,
                extractor=extractor,
            )

        assert out["fetched"] == 2
        assert extractor.search_companies.await_count == 2
        assert any(
            "Clamping bunch_searches=5" in r.getMessage() for r in caplog.records
        )

    async def test_rate_limit_saves_progress(self, mcp, wired, mock_context):
        _, jobs = wired
        extractor = MagicMock()
        extractor.search_companies = AsyncMock(side_effect=RateLimitError("slow down"))

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["NewCo"], mock_context, extractor=extractor)

        assert out["stopped_because"] == "rate_limited"
        assert out["next_run_after_seconds"] >= 3600
        # The throttled search was still a request LinkedIn saw. The middleware
        # leaves the recording to this tool, so an unrecorded 429 is a page
        # load the daily cap never learns about.
        now = datetime.now().astimezone()
        assert jobs.load(ACCOUNT_BUDGET_JOB).ledger.spent(now) == 1

    async def test_no_confident_match_does_not_serve_a_different_company(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """When the query normalizes differently from every hit, return the
        candidates + raw page -- never mislabel the top hit as the answer."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        # Query "Wonka Industries", but the page only returns unrelated firms.
        extractor = _search_extractor(["acme-corp", "globex"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Wonka Industries"], mock_context, extractor=extractor)

        served = out["results"]["Wonka Industries"]
        assert served["status"] == "no_confident_match"
        assert "Acme-Corp" in served["candidates"]
        assert "linkedin_url" not in served  # not attributed to a wrong company

    async def test_search_records_what_the_result_card_shows(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """One search page yields each company's industry, location and
        follower count from its card, served under source "search" and kept
        for later free hits -- without stamping the record fresh, which is
        reserved for an About read."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, _ = wired
        refs = [
            {
                "kind": "company",
                "url": "/company/fintech-americas/",
                "text": "Fintech Americas",
            },
            {
                "kind": "company",
                "url": "/company/fintechfutures/",
                "text": "FinTech Futures",
            },
        ]
        extractor = MagicMock()
        extractor.search_companies = AsyncMock(
            return_value={
                "sections": {"search_results": COMPANY_PAGE},
                "references": {"search_results": refs},
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Fintech Americas"], mock_context, extractor=extractor)

        served = out["results"]["Fintech Americas"]
        assert served["source"] == "search"
        assert served["industry"] == "Financial Services"
        assert served["headquarters"] == "Miami Beach, Florida"
        assert served["followers"] == 27000
        assert served["firmographics_fetched_at"] == ""

        # Seen in passing: the second card is cached with its own fields.
        rec = cache.get("FinTech Futures")
        assert rec is not None
        assert rec.industry == "Technology, Information and Media"
        assert rec.headquarters == "London, England"
        assert rec.followers == 102000
        assert rec.firmographics_source == "search"
        assert cache.needs_firmographics("FinTech Futures", datetime.now().astimezone())

    async def test_empty_input_rejected(self, mcp, mock_context):
        from fastmcp.exceptions import ToolError

        fn = await get_tool_fn(mcp, "enrich_companies")
        with pytest.raises(ToolError, match="empty"):
            await fn([], mock_context)

    async def test_about_is_off_by_default(self, mcp, wired, mock_context, monkeypatch):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, extractor=extractor)

        extractor.scrape_company.assert_not_awaited()
        assert out["about_loaded"] == 0
        assert _spent(jobs) == 1

    async def test_about_loads_the_about_tab_after_the_search(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """about=True: one search resolves the slug, then one About load on
        that slug fills every facet; both navigations hit the ledger."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        extractor.scrape_company.assert_awaited_once()
        assert extractor.scrape_company.await_args.args[0] == "copado"
        assert out["fetched"] == 1
        assert out["about_loaded"] == 1
        assert _spent(jobs) == 2  # search + About, one unit each

        served = out["results"]["Copado"]
        assert served["source"] == "company_page"
        assert served["industry"] == "Retail"
        assert served["employee_count"] == "1,001-5,000 employees"
        assert served["founded"] == "1999"
        assert served["company_type"] == "Privately Held"
        assert served["specialties"] == "Widgets, Gadgets"
        rec = cache.get("Copado")
        assert rec.has_firmographics()
        assert rec.company_urn == "9999"  # so a later jobs lookup can run

    async def test_about_counts_toward_the_bunch(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """A bunch of one navigation affords the search but not the About
        load; the search view stands and the name stays outstanding."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(
            ["Copado"], mock_context, about=True, bunch_searches=1, extractor=extractor
        )

        extractor.scrape_company.assert_not_awaited()
        assert out["fetched"] == 1
        assert out["about_loaded"] == 0
        assert _spent(jobs) == 1
        assert out["results"]["Copado"]["source"] == "search"
        assert "next_run_after_seconds" in out  # Copado still owed its About

    async def test_about_on_a_url_only_record_skips_the_search(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """A company an earlier call resolved to a URL (no facets) needs only
        the About load, not another search."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        cache.record_firmographics(
            "Copado",
            datetime.now().astimezone(),
            source="search",
            linkedin_url="https://www.linkedin.com/company/copado",
        )
        extractor = _search_extractor(["copado"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        extractor.search_companies.assert_not_awaited()
        extractor.scrape_company.assert_awaited_once()
        assert (out["fetched"], out["about_loaded"]) == (0, 1)
        assert _spent(jobs) == 1
        assert out["results"]["Copado"]["industry"] == "Retail"

    async def test_about_serves_fresh_facets_from_cache(self, mcp, wired, mock_context):
        cache, _ = wired
        cache.record_firmographics(
            "Copado",
            datetime.now().astimezone(),
            source="company_page",
            industry="Retail",
            linkedin_url="https://www.linkedin.com/company/copado",
        )
        extractor = _search_extractor(["copado"])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert out["stopped_because"] == "all_cached"
        extractor.scrape_company.assert_not_awaited()

    async def test_about_rate_limit_saves_progress(
        self, mcp, wired, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = AsyncMock(side_effect=RateLimitError("slow"))

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert out["stopped_because"] == "rate_limited"
        assert out["fetched"] == 1  # the search that ran is not forgotten
        assert cache.get("Copado").linkedin_url  # its URL was persisted
        # The About navigation LinkedIn refused is one it counted: charged.
        assert out["about_loaded"] == 1
        assert _spent(jobs) == 2

    async def test_search_rate_limit_charges_the_refused_navigation(
        self, mcp, wired, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.search_companies = AsyncMock(side_effect=RateLimitError("slow"))

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado", "Acme"], mock_context, extractor=extractor)

        assert out["stopped_because"] == "rate_limited"
        assert out["fetched"] == 1
        assert _spent(jobs) == 1
        extractor.search_companies.assert_awaited_once()  # stopped, no second

    async def test_about_failure_keeps_the_search_view_and_charges_the_load(
        self, mcp, wired, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = AsyncMock(side_effect=RuntimeError("boom"))

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        served = out["results"]["Copado"]
        assert served["source"] == "search"
        assert served["about_error"] == "boom"
        assert out["about_loaded"] == 1
        assert _spent(jobs) == 2  # the page load happened either way

    async def test_about_failure_without_a_search_still_charges_the_load(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """A URL-only record skips the search, so no view was served before
        the About load. Its failure must still land in the results and on
        the ledger, and must not take the rest of the call down with it."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        cache.record_firmographics(
            "Copado",
            datetime.now().astimezone(),
            source="search",
            linkedin_url="https://www.linkedin.com/company/copado",
        )
        extractor = _search_extractor(["globex"])
        extractor.scrape_company = AsyncMock(side_effect=RuntimeError("boom"))

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(
            ["Copado", "Globex"], mock_context, about=True, extractor=extractor
        )

        served = out["results"]["Copado"]
        assert served["about_error"] == "boom"
        assert served["linkedin_url"] == "https://www.linkedin.com/company/copado"
        assert out["about_loaded"] == 2  # Copado's failed load, Globex's failed load
        assert out["fetched"] == 1  # Globex's search ran after Copado failed
        assert out["results"]["Globex"]["about_error"] == "boom"
        assert _spent(jobs) == 3  # Copado About + Globex search + Globex About

    async def test_about_with_labels_but_no_values_is_not_reloaded(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """An About page whose rows are there but parse to nothing still
        counts as read: the second call serves it from cache instead of
        spending another navigation on the same page."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/copado/",
                "sections": {"about": "Copado\nOverview\nA company.\nCompany size\n"},
                "references": {},
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        first = await fn(["Copado"], mock_context, about=True, extractor=extractor)
        assert first["about_loaded"] == 1
        assert first["stopped_because"] == "all_done"
        assert cache.get("Copado").has_firmographics()

        second = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert second["stopped_because"] == "all_cached"
        extractor.scrape_company.assert_awaited_once()
        assert _spent(jobs) == 2  # search + one About, nothing more

    async def test_about_without_any_row_label_is_a_failed_load(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """A rendered page with none of the About row labels is not an About
        page, however much text it carries. Recording it would stamp nothing
        fresh for 90 days; instead it is a failed load: charged, surfaced as
        about_error, and left stale for the next call to retry."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/copado/",
                "sections": {"about": "Copado\nnothing parseable here"},
                "references": {},
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        first = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert first["results"]["Copado"]["about_error"] == (
            "About page carried no firmographic rows"
        )
        assert first["about_loaded"] == 1
        assert _spent(jobs) == 2  # search + the About load that showed junk
        rec = cache.get("Copado")
        assert rec is not None and rec.linkedin_url
        assert not rec.has_firmographics()

        second = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert second["about_loaded"] == 1  # retried, not served from cache
        assert extractor.scrape_company.await_count == 2

    async def test_a_real_not_found_page_is_not_stamped_fresh(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """LinkedIn's live "page isn't available" body renders as the About
        section of a deleted or renamed company. It must not be cached as a
        read About."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, _ = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/copado/",
                "sections": {"about": _NOT_FOUND_LIVE},
                "references": {},
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert "about_error" in out["results"]["Copado"]
        assert not cache.get("Copado").has_firmographics()

    async def test_an_expired_session_during_about_stops_the_bunch(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """An AuthenticationError from an About load is not a per-company
        failure to file under about_error: every remaining navigation would
        fail the same way. The load that found it is charged, progress is
        saved, and the tool routes to re-login instead of walking the list."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado", "acme"])
        extractor.scrape_company = AsyncMock(
            side_effect=AuthenticationError("session expired")
        )
        handle = AsyncMock(side_effect=AuthenticationStartedError("login opened"))
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.handle_auth_error", handle
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        with pytest.raises(ToolError, match="login opened"):
            await fn(["Copado", "Acme"], mock_context, about=True, extractor=extractor)

        handle.assert_awaited_once()
        assert isinstance(handle.call_args[0][0], AuthenticationError)
        extractor.scrape_company.assert_awaited_once()  # Acme was not attempted
        assert extractor.search_companies.await_count == 1
        assert _spent(jobs) == 2  # Copado's search + the About that hit the wall
        assert cache.get("Copado").linkedin_url  # the search's result was kept

    async def test_an_expired_session_during_search_stops_the_bunch(
        self, mcp, wired, mock_context, monkeypatch
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor([])
        extractor.search_companies = AsyncMock(
            side_effect=AuthenticationError("session expired")
        )
        handle = AsyncMock(side_effect=AuthenticationStartedError("login opened"))
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.handle_auth_error", handle
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        with pytest.raises(ToolError, match="login opened"):
            await fn(["Copado", "Acme"], mock_context, extractor=extractor)

        handle.assert_awaited_once()
        extractor.search_companies.assert_awaited_once()  # Acme was not attempted
        assert _spent(jobs) == 1  # the search that hit the wall

    async def test_a_search_that_outlives_the_deadline_skips_the_about(
        self, wired, mock_context, monkeypatch
    ):
        """The deadline is checked at the top of the loop, before the search.
        A search that carries past it must not be followed by an About load
        the tool has no time left for, and the stop must be reported as the
        deadline rather than as a finished bunch."""
        from linkedin_mcp_server.tools.company_enrichment import (
            register_company_enrichment_tools,
        )

        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])
        page = extractor.search_companies.return_value

        async def slow_search(name):
            await asyncio.sleep(0.2)  # past 75% of the 0.1s timeout
            return page

        extractor.search_companies = AsyncMock(side_effect=slow_search)
        server = FastMCP("test")
        register_company_enrichment_tools(server, tool_timeout=0.1)

        fn = await get_tool_fn(server, "enrich_companies")
        out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        extractor.scrape_company.assert_not_awaited()
        assert out["stopped_because"] == "tool_deadline"
        assert (out["fetched"], out["about_loaded"]) == (1, 0)
        assert out["results"]["Copado"]["source"] == "search"
        assert _spent(jobs) == 1

    async def test_a_call_queued_past_its_deadline_loads_nothing(
        self, mcp, wired, mock_context
    ):
        """Queued 200 s behind another session, past the 210 s the frontend
        proxy waits: nothing is searched and nothing is charged."""
        _, jobs = wired
        extractor = _search_extractor(["copado"])

        arrival = request_arrived_at.set(time.monotonic() - 200)
        try:
            fn = await get_tool_fn(mcp, "enrich_companies")
            out = await fn(["Copado"], mock_context, extractor=extractor)
        finally:
            request_arrived_at.reset(arrival)

        extractor.search_companies.assert_not_awaited()
        assert out["stopped_because"] == "tool_deadline"
        assert out["next_run_after_seconds"] == RETRY_AFTER_QUEUED_OUT
        assert out["fetched"] == 0
        assert _spent(jobs) == 0

    async def test_time_spent_queued_shortens_the_deadline(
        self, wired, mock_context, monkeypatch
    ):
        from linkedin_mcp_server.tools.company_enrichment import (
            register_company_enrichment_tools,
        )

        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])
        page = extractor.search_companies.return_value

        async def slow_search(name):
            await asyncio.sleep(0.3)
            return page

        extractor.search_companies = AsyncMock(side_effect=slow_search)
        server = FastMCP("test")
        # 75% of 13.6 s is 10.2 s from arrival; 10 s of that already went by
        # in the queue, so the 0.3 s search is all there is time for.
        register_company_enrichment_tools(server, tool_timeout=13.6)

        arrival = request_arrived_at.set(time.monotonic() - 10)
        try:
            fn = await get_tool_fn(server, "enrich_companies")
            out = await fn(["Copado"], mock_context, about=True, extractor=extractor)
        finally:
            request_arrived_at.reset(arrival)

        extractor.scrape_company.assert_not_awaited()
        assert out["stopped_because"] == "tool_deadline"
        assert (out["fetched"], out["about_loaded"]) == (1, 0)
        assert _spent(jobs) == 1

    async def test_a_rate_limited_about_stops_the_bunch_and_is_not_stamped_fresh(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """scrape_company swallows a rate-limited About into section_errors
        and returns no section. That is LinkedIn throttling, not an empty
        page: the bunch must stop there rather than keep navigating, the
        navigation is charged, and the record is left stale so the next
        call retries instead of serving nothing for 90 days."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado", "acme"])
        extractor.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/copado/",
                "sections": {},
                "section_errors": {
                    "about": {"error_type": "rate_limit", "error_message": "blocked"}
                },
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        first = await fn(
            ["Copado", "Acme"], mock_context, about=True, extractor=extractor
        )

        assert first["stopped_because"] == "rate_limited"
        assert (first["fetched"], first["about_loaded"]) == (1, 1)
        assert _spent(jobs) == 2  # search + the About load LinkedIn refused
        extractor.scrape_company.assert_awaited_once()  # Acme was not attempted
        rec = cache.get("Copado")
        assert rec is not None and rec.linkedin_url
        assert not rec.has_firmographics()

        second = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert second["stopped_because"] == "rate_limited"  # retried, not cached
        assert extractor.scrape_company.await_count == 2
        assert extractor.search_companies.await_count == 1  # URL was kept

    async def test_a_dead_browser_during_about_is_relaunched_and_retried_once(
        self, mcp, wired, mock_context, monkeypatch, caplog, dead
    ):
        """Measured: the daemon's Chrome died mid-run and every About after
        it came back with no section at all, each charged "because the page
        load still happened". Nothing loaded is nothing to charge: the browser
        is re-acquired (which relaunches it) and the same About is retried
        once, at its normal cost."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = _mock_of(dead)
        relaunched = _search_extractor(["copado"])
        relaunch = _relaunch_to(monkeypatch, relaunched)

        fn = await get_tool_fn(mcp, "enrich_companies")
        with caplog.at_level(logging.WARNING):
            out = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert "browser gone under Copado; relaunching and retrying once" in (
            caplog.text
        )
        relaunch.assert_awaited_once()
        assert out["stopped_because"] == "all_done"
        assert out["about_loaded"] == 1
        assert _spent(jobs) == 2  # search + one About, not two
        assert out["results"]["Copado"]["source"] == "company_page"
        assert cache.get("Copado").has_firmographics()
        assert extractor.scrape_company.await_count == 1
        assert relaunched.scrape_company.await_count == 1

    async def test_a_browser_still_dead_after_an_about_relaunch_stops_the_bunch(
        self, mcp, wired, mock_context, monkeypatch, dead
    ):
        """The retry failing the same way is the stop: the search that ran
        is charged, the About is not, the record stays stale for the next
        call to retry, and the next name is not attempted."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.scrape_company = _mock_of(dead)
        relaunch = _relaunch_to(monkeypatch, extractor)

        fn = await get_tool_fn(mcp, "enrich_companies")
        first = await fn(
            ["Copado", "Globex"], mock_context, about=True, extractor=extractor
        )

        assert first["stopped_because"] == "browser_unavailable"
        assert first["about_loaded"] == 0
        assert _spent(jobs) == 1  # the search, not the About that never loaded
        relaunch.assert_awaited_once()
        assert extractor.scrape_company.await_count == 2  # once, retried once
        assert "about_error" not in first["results"]["Copado"]
        assert "Globex" not in first["results"]  # never attempted
        assert extractor.search_companies.await_count == 1
        if dead is NOTHING_LOADED:
            assert first["section_errors"] == NOTHING_LOADED["section_errors"]
        rec = cache.get("Copado")
        assert rec is not None and rec.linkedin_url
        assert not rec.has_firmographics()

        extractor.scrape_company = AsyncMock(side_effect=lambda s, _: _about_result(s))
        second = await fn(["Copado"], mock_context, about=True, extractor=extractor)

        assert second["about_loaded"] == 1  # retried, not served from cache
        assert extractor.search_companies.await_count == 1  # URL was kept

    async def test_a_dead_browser_during_search_is_relaunched_and_retried_once(
        self, mcp, wired, mock_context, monkeypatch, caplog, dead
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        if dead is NOTHING_LOADED:
            dead = {
                "sections": {},
                "section_errors": {
                    "search_results": {
                        "error_type": "scraping",
                        "error_message": CLOSED_TARGET,
                    }
                },
            }
        extractor = _search_extractor(["copado"])
        extractor.search_companies = _mock_of(dead)
        relaunched = _search_extractor(["copado", "globex"])
        relaunch = _relaunch_to(monkeypatch, relaunched)

        fn = await get_tool_fn(mcp, "enrich_companies")
        with caplog.at_level(logging.WARNING):
            out = await fn(["Copado", "Globex"], mock_context, extractor=extractor)

        assert "browser gone under Copado; relaunching and retrying once" in (
            caplog.text
        )
        relaunch.assert_awaited_once()
        assert out["stopped_because"] == "all_done"
        assert out["fetched"] == 1
        assert _spent(jobs) == 1  # the retry of Copado; Globex was on its page
        assert out["results"]["Copado"]["source"] == "search"
        assert out["results"]["Globex"]["source"] == "cache"
        assert extractor.search_companies.await_count == 1
        assert relaunched.search_companies.await_count == 1

    async def test_a_browser_still_dead_after_a_search_relaunch_stops_the_bunch(
        self, mcp, wired, mock_context, monkeypatch, dead
    ):
        """A generic search failure is served as search_failed and charged;
        a dead browser says nothing about the company and loaded nothing."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        errors = {
            "search_results": {"error_type": "scraping", "error_message": CLOSED_TARGET}
        }
        if dead is NOTHING_LOADED:
            dead = {"sections": {}, "section_errors": errors}
        extractor = _search_extractor(["copado"])
        extractor.search_companies = _mock_of(dead)
        relaunch = _relaunch_to(monkeypatch, extractor)

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado", "Globex"], mock_context, extractor=extractor)

        assert out["stopped_because"] == "browser_unavailable"
        assert out["fetched"] == 0
        assert _spent(jobs) == 0
        assert out["results"] == {}  # neither name is known or failed
        relaunch.assert_awaited_once()
        assert [c.args[0] for c in extractor.search_companies.await_args_list] == [
            "Copado",
            "Copado",
        ]  # retried once, Globex never tried
        assert cache.get("Copado") is None
        if isinstance(dead, dict):
            assert out["section_errors"] == errors
        else:
            assert "section_errors" not in out

    async def test_a_failed_relaunch_serves_nothing_and_charges_nothing(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """get_ready_extractor turns a browser that will not start into the
        client-facing ToolError; that must not fall into the generic search
        handler, which would serve search_failed and charge the load."""
        _, jobs = wired
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.get_ready_extractor",
            AsyncMock(side_effect=ToolError("browser would not start")),
        )
        extractor = _search_extractor(["copado"])
        extractor.search_companies = AsyncMock(
            side_effect=TargetClosedError(CLOSED_TARGET)
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        with pytest.raises(ToolError, match="would not start"):
            await fn(["Copado"], mock_context, extractor=extractor)

        assert _spent(jobs) == 0

    @pytest.mark.parametrize(
        ("failure", "expected"),
        [
            (BrowserBusyError("profile held"), ToolError),
            (RuntimeError("profile held"), RuntimeError),
        ],
        ids=["LinkedInMCPError", "raw"],
    )
    @pytest.mark.parametrize("about", [False, True], ids=["search", "about"])
    async def test_a_relaunch_failure_of_any_kind_does_not_drain_the_list(
        self, mcp, wired, mock_context, monkeypatch, failure, expected, about
    ):
        """Reproduced: get_ready_extractor raised BrowserBusyError, which is
        not a ToolError, so it fell into the generic handler: the name was
        served as search_failed and charged, the next name was searched on
        the same dead extractor, the relaunch failed again, and so on until
        the bunch was exhausted. Whatever the relaunch raises is the
        client-facing error; nothing is served, charged, or retried."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        relaunch = AsyncMock(side_effect=failure)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.get_ready_extractor",
            relaunch,
        )
        extractor = _search_extractor(["copado", "globex", "initech"])
        gone = TargetClosedError(CLOSED_TARGET)
        if about:
            extractor.scrape_company = AsyncMock(side_effect=gone)
        else:
            extractor.search_companies = AsyncMock(side_effect=gone)
        names = ["Copado", "Globex", "Initech"]

        fn = await get_tool_fn(mcp, "enrich_companies")
        with pytest.raises(expected, match="profile held"):
            await fn(names, mock_context, about=about, extractor=extractor)

        relaunch.assert_awaited_once()
        if about:
            # The search that resolved Copado is the only navigation charged.
            assert _spent(jobs) == 1
            assert extractor.search_companies.await_count == 1
            assert extractor.scrape_company.await_count == 1
            assert not cache.get("Copado").has_firmographics()
        else:
            assert _spent(jobs) == 0
            assert extractor.search_companies.await_count == 1
            assert all(cache.get(n) is None for n in names)

    async def test_an_empty_search_page_without_a_closed_target_is_charged(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """error_type is the exception's class name, so a navigation timeout
        files the same empty shape as a dead browser. It is not one: the
        navigation happened, so it is charged and served as before, and the
        next name is searched -- no relaunch of a live browser."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        relaunch = _relaunch_to(monkeypatch, None)
        timed_out = {
            "sections": {},
            "section_errors": {
                "search_results": {
                    "error_type": "TimeoutError",
                    "error_message": "Timeout 30000ms exceeded.",
                }
            },
        }
        extractor = _search_extractor(["globex"])
        live = extractor.search_companies.return_value
        extractor.search_companies = AsyncMock(side_effect=[timed_out, live])

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado", "Globex"], mock_context, extractor=extractor)

        relaunch.assert_not_awaited()
        assert out["stopped_because"] == "bunch_complete"  # Copado outstanding
        assert out["fetched"] == 2
        assert _spent(jobs) == 2
        assert out["results"]["Copado"]["status"] == "no_confident_match"
        assert out["results"]["Globex"]["source"] == "search"
        assert [c.args[0] for c in extractor.search_companies.await_args_list] == [
            "Copado",
            "Globex",
        ]

    async def test_an_about_that_did_not_load_without_a_closed_target_is_charged(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """The About counterpart: a timed-out About is a failed load as
        before -- charged, surfaced as about_error, left stale for a retry --
        not a dead browser to relaunch."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        cache, jobs = wired
        relaunch = _relaunch_to(monkeypatch, None)
        extractor = _search_extractor(["copado", "globex"])
        extractor.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/copado/",
                "sections": {},
                "section_errors": {
                    "about": {
                        "error_type": "TimeoutError",
                        "error_message": "Timeout 30000ms exceeded.",
                    }
                },
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(
            ["Copado", "Globex"], mock_context, about=True, extractor=extractor
        )

        relaunch.assert_not_awaited()
        assert out["stopped_because"] == "bunch_complete"  # both left stale
        assert out["about_loaded"] == 2  # both Abouts were attempted
        assert _spent(jobs) == 3  # one search, two About loads
        assert out["results"]["Copado"]["about_error"] == "Timeout 30000ms exceeded."
        assert out["results"]["Globex"]["about_error"] == "Timeout 30000ms exceeded."
        assert not cache.get("Copado").has_firmographics()

    async def test_a_search_page_that_loaded_is_charged_even_with_a_warning(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """A page with content plus a section_errors note (a dropped filter,
        say) is a navigation that happened: served and charged as today."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])
        page = extractor.search_companies.return_value
        page["section_errors"] = {
            "search_results": {"error_type": "scraping", "error_message": "note"}
        }

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado"], mock_context, extractor=extractor)

        assert out["stopped_because"] == "all_done"
        assert out["fetched"] == 1
        assert _spent(jobs) == 1
        assert out["results"]["Copado"]["source"] == "search"

    async def test_a_soft_rate_limit_with_nothing_loaded_is_a_rate_limit(
        self, mcp, wired, mock_context, monkeypatch
    ):
        """search_companies files a soft rate limit under section_errors and
        returns with no section. That is a rate limit, not a dead browser: the
        refused navigation is charged and the bunch backs off."""
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        _, jobs = wired
        extractor = _search_extractor(["copado"])
        extractor.search_companies = AsyncMock(
            return_value={
                "sections": {},
                "section_errors": {
                    "search_results": {
                        "error_type": "rate_limit",
                        "error_message": "throttled",
                    }
                },
            }
        )

        fn = await get_tool_fn(mcp, "enrich_companies")
        out = await fn(["Copado", "Globex"], mock_context, extractor=extractor)

        assert out["stopped_because"] == "rate_limited"
        assert _spent(jobs) == 1
        assert extractor.search_companies.await_count == 1


class TestBunchSearchesCeiling:
    """``bunch_searches`` is clamped at call time to BUNCH_SEARCHES_MAX.

    The pydantic ``le`` bound was fixed at import; the environment has to
    reach the running tool.
    """

    def _one_page_each(self):
        """A search that reveals only the company asked for, so every name
        costs its own navigation."""

        def per_name(name):
            slug = name.lower()
            return {
                "sections": {"search_results": "x"},
                "references": {
                    "search_results": [
                        {
                            "url": f"https://www.linkedin.com/company/{slug}",
                            "text": slug,
                        }
                    ]
                },
            }

        extractor = MagicMock()
        extractor.search_companies = AsyncMock(side_effect=lambda n: per_name(n))
        return extractor

    async def test_bunch_searches_is_clamped_to_the_configured_ceiling(
        self, mcp, wired, mock_context, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        monkeypatch.setenv(EnvironmentKeys.BUNCH_SEARCHES_MAX, "1")
        extractor = self._one_page_each()

        fn = await get_tool_fn(mcp, "enrich_companies")
        with caplog.at_level(logging.INFO):
            out = await fn(
                ["a", "b", "c"], mock_context, bunch_searches=3, extractor=extractor
            )

        assert out["fetched"] == 1
        assert extractor.search_companies.await_count == 1
        assert any(
            "Clamping bunch_searches=3" in r.getMessage() for r in caplog.records
        )

    async def test_bunch_searches_garbage_ceiling_falls_back_with_a_warning(
        self, mcp, wired, mock_context, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.company_enrichment.step_delay", lambda **k: 0
        )
        monkeypatch.setenv(EnvironmentKeys.BUNCH_SEARCHES_MAX, "many")
        extractor = self._one_page_each()
        names = [f"co{i}" for i in range(22)]

        fn = await get_tool_fn(mcp, "enrich_companies")
        with caplog.at_level(logging.WARNING):
            out = await fn(names, mock_context, bunch_searches=22, extractor=extractor)

        assert out["fetched"] == 20
        assert any(
            EnvironmentKeys.BUNCH_SEARCHES_MAX in r.getMessage() for r in caplog.records
        )


class TestEnrichCompanyDeep:
    def _deep_extractor(self):
        """Mock the two calls the tool now makes: scrape_company(about) -- which
        yields the firmographics and the company URN reference -- and
        extract_page(job-search URL) for the open-roles count."""
        from linkedin_mcp_server.scraping.contracts import ExtractedSection

        mock = MagicMock()
        mock.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/acme/",
                "sections": {"about": _ABOUT_TEXT},
                "references": {
                    "about": [
                        {
                            "kind": "company_urn",
                            "url": "/search/results/people/?currentCompany=%5B%229999%22%5D",
                            "value": "9999",
                        }
                    ]
                },
            }
        )
        mock.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text="Jobs in Worldwide\n42 results\nSalesforce Administrator\n",
                references=[],
            )
        )
        return mock

    async def test_fetches_firmographics_and_open_roles(self, mcp, wired, mock_context):
        cache, _ = wired
        extractor = self._deep_extractor()

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["status"] == "fetched"
        assert out["industry"] == "Retail"
        assert out["employee_count"] == "1,001-5,000 employees"
        assert out["founded"] == "1999"
        assert out["company_type"] == "Privately Held"
        assert out["specialties"] == "Widgets, Gadgets"
        assert out["open_roles_count"] == 42  # from the job SEARCH, not the tab
        # Open roles came from job-search-by-URN, not the company /jobs/ tab.
        jobs_url = extractor.extract_page.await_args.args[0]
        assert "/jobs/search/?f_C=9999" in jobs_url
        rec = cache.get("Acme")
        assert rec.has_firmographics() and rec.has_jobs()
        assert rec.company_urn == "9999"  # cached for later jobs-only refresh

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
        extractor.extract_page.assert_not_awaited()

    async def test_a_raised_rate_limit_still_costs_the_page_load(
        self, mcp, wired, mock_context
    ):
        _, jobs = wired
        extractor = self._deep_extractor()
        extractor.scrape_company = AsyncMock(side_effect=RateLimitError("HTTP 429"))

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["next_run_after_seconds"] == 3600
        now = datetime.now().astimezone()
        assert jobs.load(ACCOUNT_BUDGET_JOB).ledger.spent(now) == 1

    async def test_a_rate_limited_jobs_page_is_not_cached_as_fresh(
        self, mcp, wired, mock_context
    ):
        """extract_page can return the soft rate-limit sentinel WITHOUT raising.
        Caching it would serve a failed lookup as fresh for the jobs TTL, so the
        jobs half must stay stale (unrecorded) instead."""
        from linkedin_mcp_server.scraping.contracts import (
            RATE_LIMITED_SECTION_TEXT,
            ExtractedSection,
        )

        cache, _ = wired
        extractor = self._deep_extractor()
        extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        )

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        rec = cache.get("Acme")
        assert rec.has_firmographics()  # About succeeded
        assert not rec.has_jobs()  # rate-limited jobs NOT stamped fresh
        assert cache.needs_jobs("Acme", datetime.now().astimezone())  # retried next
        # And the caller is told, exactly as when the throttle raised.
        assert out["status"] == "rate_limited"
        assert out["next_run_after_seconds"] == 3600
        assert out["industry"] == "Retail"

    async def test_a_failed_jobs_page_is_not_reported_as_fetched(
        self, mcp, wired, mock_context
    ):
        """An error section or an empty page skips the cache write; the
        status has to say so, or stale open roles read as a refresh."""
        from linkedin_mcp_server.scraping.contracts import ExtractedSection

        cache, _ = wired
        fn = await get_tool_fn(mcp, "enrich_company_deep")

        extractor = self._deep_extractor()
        extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text="",
                references=[],
                error={"error_type": "TimeoutError", "error_message": "slow"},
            )
        )
        out = await fn("Acme", mock_context, extractor=extractor)
        assert out["status"] == "jobs_failed"
        assert "slow" in out["jobs_note"]
        assert not cache.get("Acme").has_jobs()

        extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(text="", references=[])
        )
        out = await fn("Acme", mock_context, extractor=extractor, refresh=True)
        assert out["status"] == "jobs_failed"
        assert "empty page" in out["jobs_note"]
        assert not cache.get("Acme").has_jobs()

    async def test_an_unparsed_count_is_stamped_fresh_but_said_so(
        self, mcp, wired, mock_context
    ):
        """The "N results" header is matched in English only, so on any other
        locale the count is None on every fetch. Refusing the stamp would make
        jobs never fresh there; the page is recorded and the view says the
        count could not be read, so a None is not mistaken for zero."""
        from linkedin_mcp_server.scraping.contracts import ExtractedSection

        cache, _ = wired
        extractor = self._deep_extractor()
        extractor.extract_page = AsyncMock(
            return_value=ExtractedSection(
                text=(
                    "Jobs in Weltweit\n42 Ergebnisse\n"
                    "Salesforce Administrator\nSalesforce Administrator\n"
                ),
                references=[],
            )
        )

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["status"] == "fetched"
        assert out["open_roles_count"] is None
        assert out["open_roles_sample"] == ["Salesforce Administrator"]
        assert "count unparsed" in out["jobs_note"]
        rec = cache.get("Acme")
        assert rec.has_jobs()  # stamped: the page loaded, only the header is foreign

        # The same note travels with the cached record.
        read = await get_tool_fn(mcp, "get_company_cache")
        assert "count unparsed" in (await read("Acme"))["jobs_note"]

    async def test_stale_jobs_refetch_uses_cached_urn_without_about(
        self, mcp, wired, mock_context
    ):
        """Firmographics fresh, jobs stale -> re-fetch ONLY open roles, using
        the URN cached from the earlier deep fetch (no About re-scrape)."""
        cache, _ = wired
        now = datetime.now().astimezone()
        cache.record_firmographics(
            "Acme",
            now,
            source="company_page",
            industry="Retail",
            company_urn="9999",
        )
        old = (now - timedelta(days=30)).isoformat()
        rec = cache.get("Acme")
        rec.open_roles_count = 5
        rec.jobs_fetched_at = old
        cache.save(rec)

        extractor = self._deep_extractor()
        fn = await get_tool_fn(mcp, "enrich_company_deep")
        await fn("Acme", mock_context, extractor=extractor)

        extractor.scrape_company.assert_not_awaited()  # firmographics still fresh
        extractor.extract_page.assert_awaited_once()  # only open roles refreshed
        assert "f_C=9999" in extractor.extract_page.await_args.args[0]

    async def test_about_failure_still_charges_the_ledger(
        self, mcp, wired, mock_context
    ):
        """scrape_company swallows an auth-walled/crashed About into
        section_errors and returns no section; _load_about raises. The
        navigation still happened, so it must be charged and the record left
        stale, not silently dropped from the budget."""
        cache, jobs = wired
        extractor = self._deep_extractor()
        extractor.scrape_company = AsyncMock(
            return_value={
                "url": "https://www.linkedin.com/company/acme/",
                "sections": {},
                "section_errors": {
                    "about": {"error_type": "scraping", "error_message": "crashed"}
                },
            }
        )

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        with pytest.raises(ToolError):
            await fn("Acme", mock_context, extractor=extractor)

        assert _spent(jobs) == 1  # the About load was charged despite failing
        extractor.extract_page.assert_not_awaited()
        rec = cache.get("Acme")
        assert rec is None or not rec.has_firmographics()  # not stamped fresh

    @pytest.mark.parametrize("shape", ["soft", "hard"])
    async def test_a_rate_limited_about_is_charged_and_reported(
        self, mcp, wired, mock_context, shape
    ):
        """The soft rate-limit shape (section_errors, no raise) and the hard
        one (RateLimitError) both end the call as rate_limited, with the
        refused About navigation charged and the record left stale."""
        cache, jobs = wired
        extractor = self._deep_extractor()
        if shape == "soft":
            extractor.scrape_company = AsyncMock(
                return_value={
                    "url": "https://www.linkedin.com/company/acme/",
                    "sections": {},
                    "section_errors": {
                        "about": {
                            "error_type": "rate_limit",
                            "error_message": "blocked",
                        }
                    },
                }
            )
        else:
            extractor.scrape_company = AsyncMock(side_effect=RateLimitError("slow"))

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["status"] == "rate_limited"
        assert _spent(jobs) == 1  # the refused About navigation was charged
        extractor.extract_page.assert_not_awaited()  # stopped at About
        rec = cache.get("Acme")
        assert rec is None or not rec.has_firmographics()

    async def test_a_rate_limited_job_search_is_charged(self, mcp, wired, mock_context):
        _, jobs = wired
        extractor = self._deep_extractor()
        extractor.extract_page = AsyncMock(side_effect=RateLimitError("slow"))

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        out = await fn("Acme", mock_context, extractor=extractor)

        assert out["status"] == "rate_limited"
        assert _spent(jobs) == 2  # About plus the refused job search

    async def test_include_jobs_false_skips_the_job_search(
        self, mcp, wired, mock_context
    ):
        cache, _ = wired
        extractor = self._deep_extractor()

        fn = await get_tool_fn(mcp, "enrich_company_deep")
        await fn("Acme", mock_context, include_jobs=False, extractor=extractor)

        extractor.scrape_company.assert_awaited_once()  # About still fetched
        extractor.extract_page.assert_not_awaited()  # no open-roles lookup


class TestGetCompanyCache:
    async def test_lists_and_reads(self, mcp, wired):
        cache, _ = wired
        now = datetime.now().astimezone()
        cache.record_firmographics(
            "Acme", now, source="company_page", industry="Retail"
        )

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


class TestQueryCompanyCache:
    def _seed(self, cache):
        now = datetime.now().astimezone()
        cache.record_firmographics(
            "Acme",
            now,
            source="company_page",
            industry="Retail",
            headquarters="Cairo, Egypt",
            employee_count="51-200 employees",
            founded="1999",
        )
        cache.record_jobs("Acme", now, count=3, sample=["Admin"])
        cache.record_firmographics(
            "Globex",
            now,
            source="company_page",
            industry="Software Development",
            headquarters="Redmond, Washington",
            employee_count="10,001+ employees",
            founded="2015",
        )
        cache.record_jobs("Globex", now, count=0, sample=[])
        # Search-only: URL known, no facets, no jobs.
        cache.record_firmographics(
            "Initech", now, source="search", linkedin_url="https://x/company/initech"
        )

    async def test_no_criteria_lists_everything_in_the_view_shape(self, mcp, wired):
        cache, _ = wired
        self._seed(cache)

        fn = await get_tool_fn(mcp, "query_company_cache")
        out = await fn()

        assert out["count"] == 3
        names = [c["display_name"] for c in out["companies"]]
        assert names == ["Acme", "Globex", "Initech"]
        assert out["companies"][0]["founded"] == "1999"

    async def test_industry_substring(self, mcp, wired):
        cache, _ = wired
        self._seed(cache)

        fn = await get_tool_fn(mcp, "query_company_cache")
        out = await fn(industry="software")

        assert [c["display_name"] for c in out["companies"]] == ["Globex"]

    async def test_headcount_excludes_the_unfetched(self, mcp, wired):
        cache, _ = wired
        self._seed(cache)

        fn = await get_tool_fn(mcp, "query_company_cache")
        out = await fn(min_employees=100)

        # Initech has no band and is excluded, not assumed to qualify.
        assert {c["display_name"] for c in out["companies"]} == {"Acme", "Globex"}
        out = await fn(max_employees=1000)
        assert [c["display_name"] for c in out["companies"]] == ["Acme"]

    async def test_hiring_and_founded(self, mcp, wired):
        cache, _ = wired
        self._seed(cache)

        fn = await get_tool_fn(mcp, "query_company_cache")
        assert [c["display_name"] for c in (await fn(hiring=True))["companies"]] == [
            "Acme"
        ]
        assert [c["display_name"] for c in (await fn(hiring=False))["companies"]] == [
            "Globex"
        ]
        out = await fn(founded_after=2000, headquarters="redmond")
        assert [c["display_name"] for c in out["companies"]] == ["Globex"]
        out = await fn(founded_before=2000)
        assert [c["display_name"] for c in out["companies"]] == ["Acme"]

    async def test_limit_caps_the_page_but_not_the_count(self, mcp, wired):
        cache, _ = wired
        self._seed(cache)

        fn = await get_tool_fn(mcp, "query_company_cache")
        out = await fn(limit=1)

        assert out["count"] == 3
        assert len(out["companies"]) == 1

    async def test_spends_no_budget(self, mcp, wired):
        cache, jobs = wired
        self._seed(cache)
        before = _spent(jobs)

        fn = await get_tool_fn(mcp, "query_company_cache")
        await fn(industry="retail")

        assert _spent(jobs) == before

    async def test_empty_cache(self, mcp, wired):
        fn = await get_tool_fn(mcp, "query_company_cache")
        assert await fn(industry="retail") == {"count": 0, "companies": []}

    async def test_inverted_headcount_range_is_an_error(self, mcp, wired):
        """A range nothing can satisfy is a mistake, not an empty result."""
        from fastmcp.exceptions import ToolError

        cache, _ = wired
        self._seed(cache)

        fn = await get_tool_fn(mcp, "query_company_cache")
        with pytest.raises(ToolError, match="min_employees"):
            await fn(min_employees=500, max_employees=100)

    async def test_founded_year_out_of_range_is_rejected(self, mcp, wired):
        """A year outside 1000-2100 matches no stored value, so accepting
        it would return an empty page that reads like a real answer."""
        # FastMCP wraps the pydantic error raised by Field() constraints in
        # its own ValidationError, which does not subclass pydantic's.
        from fastmcp.exceptions import ValidationError

        with pytest.raises(ValidationError, match="founded_after"):
            await mcp.call_tool("query_company_cache", {"founded_after": 20015})
        with pytest.raises(ValidationError, match="founded_before"):
            await mcp.call_tool("query_company_cache", {"founded_before": 999})
