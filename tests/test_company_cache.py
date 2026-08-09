"""Tests for the TTL'd company cache and its parsers.

No browser, no clock beyond an explicit ``now``.
"""

from datetime import datetime, timedelta

import pytest

from linkedin_mcp_server.company_cache import (
    CompanyCache,
    CompanyRecord,
    normalize_company_name,
)
from linkedin_mcp_server.scraping.company_parse import (
    parse_about,
    parse_jobs,
    parse_search_results,
)

NOW = datetime(2026, 8, 9, 10, 0)


class TestNormalize:
    def test_collapses_legal_suffix_and_tld(self):
        assert normalize_company_name("Salesforce.com, Inc.") == normalize_company_name(
            "Salesforce"
        )

    def test_drops_dash_descriptor(self):
        assert normalize_company_name("Redtag – Salesforce implementation") == "redtag"

    def test_strips_accents_and_case(self):
        assert normalize_company_name("Nestlé S.A.") == normalize_company_name("nestle")

    def test_empty_is_empty(self):
        assert normalize_company_name("") == ""


class TestRecordFreshness:
    def test_missing_stamp_is_never_fresh(self):
        rec = CompanyRecord(key="x")
        assert not rec.firmographics_fresh(NOW, timedelta(days=90))
        assert not rec.has_firmographics()

    def test_fresh_within_ttl(self):
        rec = CompanyRecord(key="x", firmographics_fetched_at=NOW.isoformat())
        later = NOW + timedelta(days=89)
        assert rec.firmographics_fresh(later, timedelta(days=90))

    def test_stale_past_ttl(self):
        rec = CompanyRecord(key="x", firmographics_fetched_at=NOW.isoformat())
        later = NOW + timedelta(days=91)
        assert not rec.firmographics_fresh(later, timedelta(days=90))

    def test_jobs_and_firmographics_expire_independently(self):
        rec = CompanyRecord(
            key="x",
            firmographics_fetched_at=NOW.isoformat(),
            jobs_fetched_at=NOW.isoformat(),
        )
        # 30 days on: firmographics (90d) still fresh, jobs (14d) stale.
        later = NOW + timedelta(days=30)
        assert rec.firmographics_fresh(later, timedelta(days=90))
        assert not rec.jobs_fresh(later, timedelta(days=14))

    def test_corrupt_stamp_is_not_fresh(self):
        rec = CompanyRecord(key="x", firmographics_fetched_at="not-a-date")
        assert not rec.firmographics_fresh(NOW, timedelta(days=90))


class TestCache:
    def test_roundtrip(self, tmp_path):
        cache = CompanyCache(tmp_path)
        cache.record_firmographics(
            "Salesforce",
            NOW,
            source="company_page",
            industry="Software",
            employee_count="10,001+ employees",
        )
        rec = cache.get("salesforce.com")  # normalises to the same key
        assert rec is not None
        assert rec.industry == "Software"

    def test_needs_firmographics_when_absent_or_stale(self, tmp_path):
        cache = CompanyCache(tmp_path, firmographics_ttl=timedelta(days=90))
        assert cache.needs_firmographics("Acme", NOW)
        cache.record_firmographics("Acme", NOW, source="search", industry="Retail")
        assert not cache.needs_firmographics("Acme", NOW + timedelta(days=89))
        assert cache.needs_firmographics("Acme", NOW + timedelta(days=91))

    def test_a_cheap_search_hit_never_blanks_a_deep_field(self, tmp_path):
        """A later search must not erase headquarters a deep fetch found."""
        cache = CompanyCache(tmp_path)
        cache.record_firmographics(
            "Acme",
            NOW,
            source="company_page",
            industry="Retail",
            headquarters="Cairo, Egypt",
        )
        cache.record_firmographics(
            "Acme",
            NOW,
            source="search",
            linkedin_url="https://x/company/acme",
        )
        rec = cache.get("Acme")
        assert rec.headquarters == "Cairo, Egypt"  # preserved
        assert rec.linkedin_url.endswith("/acme")  # added

    def test_jobs_recorded_separately(self, tmp_path):
        cache = CompanyCache(tmp_path)
        cache.record_jobs(
            "Acme", NOW, count=12, sample=["Salesforce Admin", "RevOps Lead"]
        )
        rec = cache.get("Acme")
        assert rec.open_roles_count == 12
        assert rec.has_jobs()
        assert not rec.has_firmographics()

    def test_unreadable_file_is_ignored_not_fatal(self, tmp_path):
        cache = CompanyCache(tmp_path / "companies")
        cache.record_firmographics("Acme", NOW, source="search", industry="Retail")
        # Corrupt the specific cache file, not whatever else shares the dir.
        cache._path("acme").write_text("{ this is not json", "utf-8")
        assert cache.get("Acme") is None

    def test_key_with_nothing_usable_is_rejected(self, tmp_path):
        cache = CompanyCache(tmp_path)
        with pytest.raises(ValueError, match="usable"):
            cache._path("")

    def test_list_keys_empty_before_writes(self, tmp_path):
        assert CompanyCache(tmp_path / "unborn").list_keys() == []


class TestParseAbout:
    def test_pulls_labelled_fields(self):
        text = (
            "Acme Corp\nOverview\n"
            "Industry\nRetail\n"
            "Company size\n1,001-5,000 employees\n"
            "Headquarters\nCairo, Egypt\n"
            "Website\nhttps://acme.example\n"
            "Founded\n1999\n"
        )
        out = parse_about(text)
        assert out["industry"] == "Retail"
        assert out["employee_count"] == "1,001-5,000 employees"
        assert out["headquarters"] == "Cairo, Egypt"
        assert out["website"] == "https://acme.example"

    def test_size_plus_band(self):
        assert parse_about("10,001+ employees")["employee_count"] == "10,001+ employees"

    def test_returns_only_confident_keys(self):
        # No labels, no employee line -> nothing claimed.
        assert parse_about("Just some prose about the company.") == {}

    def test_empty(self):
        assert parse_about("") == {}


class TestParseJobs:
    def test_reads_the_heading_count(self):
        text = "Jobs\n47 open jobs\nSalesforce Administrator\nAccount Executive\n"
        out = parse_jobs(text)
        assert out.count == 47
        assert "Salesforce Administrator" in out.sample

    def test_falls_back_to_seen_roles_when_no_heading(self):
        text = "Careers\nSalesforce Developer\nRevenue Operations Manager\n"
        out = parse_jobs(text)
        assert out.count == 2

    def test_empty(self):
        assert parse_jobs("") == (None, [])


class TestParseSearchResults:
    def test_extracts_company_slugs(self):
        refs = [
            {"url": "https://www.linkedin.com/company/copado/", "text": "Copado"},
            {"url": "https://www.linkedin.com/company/gearset", "text": "Gearset"},
            {"url": "https://www.linkedin.com/in/someone", "text": "Not a company"},
            {"url": "https://www.linkedin.com/company/copado/", "text": "Copado dup"},
        ]
        hits = parse_search_results(refs)
        assert [h["slug"] for h in hits] == ["copado", "gearset"]
        assert hits[0]["url"] == "https://www.linkedin.com/company/copado"

    def test_empty(self):
        assert parse_search_results([]) == []
