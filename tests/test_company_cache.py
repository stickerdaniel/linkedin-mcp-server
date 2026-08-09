"""Tests for the TTL'd company cache and its parsers.

No browser, no clock beyond an explicit ``now``.
"""

from datetime import datetime, timedelta

import pytest

from linkedin_mcp_server.company_cache import (
    CompanyCache,
    CompanyRecord,
    normalize_company_name,
    ttl_from_days,
)
from linkedin_mcp_server.scraping.company_parse import (
    parse_about,
    parse_job_search,
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


class TestTTLFromDays:
    default = timedelta(days=90)

    def test_parses_days(self):
        assert ttl_from_days("30", self.default) == timedelta(days=30)

    def test_accepts_fractional_days(self):
        assert ttl_from_days("0.5", self.default) == timedelta(hours=12)

    def test_unset_falls_back(self):
        assert ttl_from_days(None, self.default) == self.default
        assert ttl_from_days("   ", self.default) == self.default

    def test_non_numeric_falls_back(self):
        assert ttl_from_days("forever", self.default) == self.default

    def test_non_positive_falls_back_not_always_stale(self):
        # 0 or negative would mean every lookup re-fetches -- the cache off.
        assert ttl_from_days("0", self.default) == self.default
        assert ttl_from_days("-5", self.default) == self.default

    def test_configured_ttl_flows_through_the_cache(self, tmp_path):
        cache = CompanyCache(
            tmp_path, firmographics_ttl=ttl_from_days("7", self.default)
        )
        now = datetime(2026, 8, 9, 10, 0)
        cache.record_firmographics("Acme", now, source="search", industry="Retail")
        assert not cache.needs_firmographics("Acme", now + timedelta(days=6))
        assert cache.needs_firmographics("Acme", now + timedelta(days=8))


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
        assert rec is not None
        assert rec.headquarters == "Cairo, Egypt"  # preserved
        assert rec.linkedin_url.endswith("/acme")  # added

    def test_a_search_write_does_not_refresh_or_downgrade_a_deep_record(self, tmp_path):
        """A bare search hit (URL only) recurring across the network must not
        reset a deep record's 90-day clock or flip its source to 'search',
        which would keep stale firmographics 'fresh' forever."""
        cache = CompanyCache(tmp_path)
        day0 = NOW
        cache.record_firmographics(
            "Acme",
            day0,
            source="company_page",
            industry="Retail",
            headquarters="Cairo",
        )
        day80 = NOW + timedelta(days=80)
        cache.record_firmographics(
            "Acme", day80, source="search", linkedin_url="https://x/company/acme"
        )
        rec = cache.get("Acme")
        assert rec is not None
        assert rec.firmographics_source == "company_page"  # not downgraded
        assert rec.firmographics_fetched_at == day0.isoformat()  # not re-stamped
        # ...so it correctly reads as stale past the 90-day TTL from day 0.
        assert cache.needs_firmographics("Acme", NOW + timedelta(days=91))

    def test_a_bare_search_hit_is_not_treated_as_having_firmographics(self, tmp_path):
        cache = CompanyCache(tmp_path)
        cache.record_firmographics(
            "NewCo", NOW, source="search", linkedin_url="https://x/company/newco"
        )
        rec = cache.get("NewCo")
        assert rec is not None
        assert rec.linkedin_url  # URL recorded
        assert not rec.has_firmographics()  # but no firmographic content
        assert cache.needs_firmographics("NewCo", NOW)  # deep fetch still wanted

    def test_jobs_recorded_separately(self, tmp_path):
        cache = CompanyCache(tmp_path)
        cache.record_jobs(
            "Acme", NOW, count=12, sample=["Salesforce Admin", "RevOps Lead"]
        )
        rec = cache.get("Acme")
        assert rec is not None
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

    def test_follower_count_above_the_band_is_not_mistaken_for_size(self):
        # Real pages show "See all N employees" (a follower count) in the top
        # card, ABOVE the "Company size" band. The band must win.
        text = (
            "Acme\n5,678 employees on LinkedIn\n"
            "Industry\nRetail\n"
            "Company size\n1,001-5,000 employees\n"
        )
        assert parse_about(text)["employee_count"] == "1,001-5,000 employees"

    def test_bare_employee_count_without_a_band_is_ignored(self):
        # No range and no '+', so it's a follower count, not a size band.
        assert "employee_count" not in parse_about("1,234 employees on LinkedIn")

    def test_returns_only_confident_keys(self):
        # No labels, no employee line -> nothing claimed.
        assert parse_about("Just some prose about the company.") == {}

    def test_empty(self):
        assert parse_about("") == {}


class TestParseJobSearch:
    def test_reads_the_results_count(self):
        text = "Jobs in Worldwide\n47 results\nSalesforce Administrator\n"
        out = parse_job_search(text)
        assert out.count == 47
        assert "Salesforce Administrator" in out.sample

    def test_plus_capped_count_read_as_floor(self):
        assert parse_job_search("2,000+ results\n").count == 2000

    def test_no_matching_jobs_is_zero(self):
        assert parse_job_search("No matching jobs found").count == 0

    def test_never_invents_a_count_without_a_results_header(self):
        # Text with role-ish lines but no "N results" -> count stays None.
        assert parse_job_search("Some page\nSenior Manager\n").count is None

    def test_job_card_ui_chrome_is_excluded_from_sample(self):
        # These carry a role word but are UI chrome, not titles (seen live).
        text = (
            "12 results\n"
            "Product Manager\n"
            "Save Product Manager  at Gearset\n"
            "What's the opportunity for a Product Manager at Gearset?\n"
            "Account Executive with verification\n"
        )
        out = parse_job_search(text)
        assert out.count == 12
        assert out.sample == ["Product Manager"]

    def test_empty(self):
        assert parse_job_search("") == (None, [])


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

    def test_page_by_ad_prefix_is_stripped(self):
        # LinkedIn's promoted top result is labelled "Page by <Company>" but
        # links to the real company (verified live: Page by Salesforce ->
        # /company/salesforce/). The label must not defeat name matching.
        refs = [
            {"url": "/company/salesforce/", "text": "Page by Salesforce"},
        ]
        hits = parse_search_results(refs)
        assert hits[0]["name"] == "Salesforce"
        assert hits[0]["slug"] == "salesforce"

    def test_follower_blurb_falls_back_to_slug(self):
        # A follower/relationship blurb also carries a /company/ link but is
        # not a company name; the slug is the reliable identity.
        refs = [
            {
                "url": "/company/docusign/",
                "text": "Vamsi & 1 other connection follow this page",
            },
        ]
        hits = parse_search_results(refs)
        assert hits[0]["name"] == "docusign"  # slug, not the blurb

    def test_empty(self):
        assert parse_search_results([]) == []


# ---------------------------------------------------------------------------
# Regression tests against REAL LinkedIn innerText, captured live from the
# signed-in MCP on 2026-08-09 (Microsoft, Gearset, a not-found page, and empty
# jobs tabs). These are the layouts the synthetic tests could not anticipate --
# the follower/associated-members counts that sit above the size band, the
# "page isn't available" body, and a jobs tab with no openings. Trimmed to the
# firmographic-bearing lines; the structure (labels, ordering, decoy counts) is
# verbatim. If LinkedIn changes its markup, these break first.
# ---------------------------------------------------------------------------
_MS_ABOUT_LIVE = (
    "Microsoft \n"
    "Software Development Redmond, Washington 29M followers 10K+ employees\n"
    "Ranked on LinkedIn Top Companies\n"
    "Overview\n"
    "Microsoft operates in 190 countries and is made up of approximately "
    "228,000 passionate employees worldwide.\n"
    "Website\nhttps://news.microsoft.com/\nVerified page \n"
    "Industry\nSoftware Development\n"
    "Company size\n10,001+ employees\n"
    "233,215 associated members \n"
    "Headquarters\nRedmond, Washington\n"
    "Specialties\nBusiness Software, Developer Tools"
)
_GEARSET_ABOUT_LIVE = (
    "Gearset \n"
    "Software Development DevOps Software Cambridge 15K followers 201-500 employees\n"
    "Website\nhttp://www.gearset.com/\n"
    "Industry\nSoftware Development\n"
    "Company size\n201-500 employees\n"
    "370 associated members \n"
    "Founded\n2015"
)
_NOT_FOUND_LIVE = (
    "This LinkedIn Page isn't available\n"
    "The page you're searching for no longer exists."
)
# Real job-SEARCH-by-company text (Accenture, f_C=1033), captured live
# 2026-08-09. This is the actual open-roles source; note the "2,000+ results"
# header and the title/"title with verification"/company/location cadence.
_ACCENTURE_JOBSEARCH_LIVE = (
    "Jobs in Worldwide\n"
    "2,000+ results\n"
    "Set alert\n"
    "Principal Director of Content Operations\n"
    "Principal Director of Content Operations with verification\n"
    "Accenture\n"
    "Montreal, QC (On-site)\n"
    "19 connections work here\n"
    "Viewed\n"
    "Droga5 Senior Copywriter\n"
    "Droga5 Senior Copywriter with verification\n"
    "Accenture\n"
    "New York, NY (On-site)\n"
    "1 week ago"
)


class TestParseRealLinkedInPages:
    def test_real_about_ignores_follower_and_associated_counts(self):
        """The follower count and 'N associated members' both precede/follow
        the size band on the real page; only the band must be taken."""
        ms = parse_about(_MS_ABOUT_LIVE)
        assert ms["industry"] == "Software Development"
        assert ms["headquarters"] == "Redmond, Washington"
        assert ms["website"] == "https://news.microsoft.com/"
        # NOT "29M", "10K+", or "233,215 associated members".
        assert ms["employee_count"] == "10,001+ employees"

    def test_real_partner_about(self):
        g = parse_about(_GEARSET_ABOUT_LIVE)
        assert g["industry"] == "Software Development"
        assert g["employee_count"] == "201-500 employees"  # not 15K, not 370
        assert g["founded"] == "2015"

    def test_real_not_found_page_yields_nothing(self):
        # A deleted/renamed company page must not fabricate any field.
        assert parse_about(_NOT_FOUND_LIVE) == {}
        assert parse_job_search(_NOT_FOUND_LIVE) == (None, [])

    def test_real_job_search_reads_count_and_titles(self):
        out = parse_job_search(_ACCENTURE_JOBSEARCH_LIVE)
        assert out.count == 2000  # "2,000+ results" floor
        # Real titles come through; the "with verification" dup line and the
        # company/location lines do not.
        assert "Principal Director of Content Operations" in out.sample
        assert not any("with verification" in s for s in out.sample)
        assert "Montreal, QC (On-site)" not in out.sample
