"""Company profile, employee-list and company-search workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

import logging

from patchright._impl._errors import TargetClosedError

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    FilterValidationError,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.facets import FacetResolver
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, _company_section_specs
from linkedin_mcp_server.scraping.identifiers import (
    company_page_url,
    normalize_company_identifier,
)
from linkedin_mcp_server.scraping.link_metadata import Reference, dedupe_references
from linkedin_mcp_server.scraping.search_pages import paginate_search, search_rows
from linkedin_mcp_server.scraping.search_parse import parse_company_cards
from linkedin_mcp_server.scraping.search_urls import (
    build_company_search_url,
    company_size_letters,
    industry_ids,
    require_company_criteria,
)
from linkedin_mcp_server.scraping.session import ScrapingSession, nav_delay

if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)


class CompanyScraper:
    """Own every workflow whose subject is a LinkedIn company page."""

    def __init__(
        self,
        session: ScrapingSession,
        capture: SectionCapture,
        facets: FacetResolver,
    ):
        self._session = session
        self._capture = capture
        self._facets = facets

    async def scrape_company(
        self,
        company_name: str,
        requested: set[str],
        callbacks: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Scrape a company profile with configurable sections.

        Returns:
            {url, sections: {name: text}}
        """
        requested = requested | {"about"}
        company_name = normalize_company_identifier(company_name)
        base_url = company_page_url(company_name)
        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        rate_limited = False

        requested_ordered = [
            spec
            for spec in _company_section_specs(COMPANY_SECTIONS)
            if spec.name in requested
        ]
        total = len(requested_ordered)

        if callbacks:
            await callbacks.on_start("company profile", base_url)

        try:
            for i, spec in enumerate(requested_ordered):
                if i > 0:
                    await self._session.pace(nav_delay())

                section_name = spec.name
                url = base_url + spec.suffix
                try:
                    if CaptureMode.OVERLAY in spec.plan.mode:
                        extracted = await self._capture._extract_overlay(
                            url,
                            section_name,
                            plan=spec.plan,
                        )
                    else:
                        extracted = await self._capture.capture(
                            url, section_name, spec.plan
                        )

                    if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
                        sections[section_name] = extracted.text
                        if extracted.references:
                            references[section_name] = extracted.references
                    elif extracted.text == RATE_LIMITED_SECTION_TEXT:
                        section_errors[section_name] = rate_limited_section_error()
                        rate_limited = True
                    elif extracted.error:
                        section_errors[section_name] = extracted.error
                except LinkedInScraperException:
                    raise
                except TargetClosedError:
                    # Not a property of the section; see scrape_person.
                    raise
                except Exception as e:
                    logger.warning("Error scraping section %s: %s", section_name, e)
                    section_errors[section_name] = build_issue_diagnostics(
                        e,
                        context="scrape_company",
                        target_url=url,
                        section_name=section_name,
                    )

                # "Scraped" = processed/attempted, not necessarily successful.
                # Per-section failures are captured in section_errors.
                if callbacks:
                    percent = round((i + 1) / total * 95)
                    await callbacks.on_progress(
                        f"Scraped {section_name} ({i + 1}/{total})", percent
                    )

                if rate_limited:
                    break
        except (LinkedInScraperException, TargetClosedError) as e:
            # The closed target is re-raised past the section loop above, so
            # it reaches the caller only through this handler.
            if callbacks:
                await callbacks.on_error(e)
            raise

        result: dict[str, Any] = {
            "url": f"{base_url}/",
            "sections": sections,
        }
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors

        if callbacks:
            await callbacks.on_complete("company profile", result)

        return result

    async def get_company_employees(
        self,
        company_name: str,
        keywords: str | None = None,
    ) -> dict[str, Any]:
        """List employees at a company from the /people/ page.

        Returns:
            {url, sections: {employees: text}, references: {employees: [...]}}
        """
        company_name = normalize_company_identifier(company_name)
        url = company_page_url(company_name, "/people/")
        if keywords:
            url += f"?keywords={quote_plus(keywords)}"
        extracted = await self._capture.capture(
            url,
            "employees",
            CapturePlan(CaptureMode.COMPANY_PEOPLE),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["employees"] = extracted.text
            if extracted.references:
                references["employees"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["employees"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["employees"] = extracted.error

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result

    async def search_companies(
        self,
        keywords: str | None = None,
        industry: list[str] | None = None,
        size: list[str] | None = None,
        hq_location: str | None = None,
        has_jobs: bool | None = None,
        max_pages: int = 1,
    ) -> dict[str, Any]:
        """Search for companies and extract the results pages.

        Facets narrow the result set on LinkedIn's side, so a shortlist
        built here costs one navigation per page rather than one per
        company; ``enrich_companies`` then only pays for the companies that
        survived the filter.

        Args:
            keywords: Free-text query ("fintech", "electric vehicles").
                Optional when at least one of ``industry``, ``size`` or
                ``hq_location`` is given.
            industry: Optional ``industryCompanyVertical`` facet. Each element is
                either a numeric LinkedIn industry id (always accepted, e.g.
                ``"4"``) or one of the names in ``COMPANY_INDUSTRY_IDS``
                (case-insensitive, e.g. ``"Software Development"``). The name
                table is partial; an unknown name raises
                ``FilterValidationError`` listing the names it does know.
            size: Optional ``companySize`` facet. Each element is a headcount
                bucket as LinkedIn labels it (``"self-employed"``, ``"1-10"``,
                ``"11-50"``, ``"51-200"``, ``"201-500"``, ``"501-1000"``,
                ``"1001-5000"``, ``"5001-10000"``, ``"10001+"``) or the
                facet letter it maps to (``"A"``-``"I"`` in that order, see
                ``COMPANY_SIZE_LETTERS``). Anything else raises
                ``FilterValidationError``.
            hq_location: Optional headquarters filter, a free-text country or
                city name resolved to LinkedIn's numeric geo id via the site's
                own location dropdown (see ``FacetResolver.resolve_geo_urn``)
                and sent as ``companyHqGeo``. An unrecognized name raises
                ``FilterValidationError`` rather than silently returning
                worldwide results.
            has_jobs: When true, only companies with live job listings
                (``hasJobs="true"``, the JSON-string form LinkedIn normalises
                a bare ``true`` to).
            max_pages: Maximum result pages to load (10 companies per page).
                Stops early once a page adds no new companies. Default 1.

        Returns:
            {url, sections: {search_results: text}, companies: [...],
            result_count} -- pages joined by ``\\n---\\n``; ``companies`` holds
            one row per card parsed from each page's text
            (``search_parse.parse_company_cards``), deduped by URL, and
            ``result_count`` the first page's "About N results" header or None.
        """
        ids = industry_ids(industry)
        size_letters = company_size_letters(size)
        require_company_criteria(
            keywords=keywords,
            industry_ids=ids,
            size_letters=size_letters,
            hq_location=hq_location,
        )

        geo_id: str | None = None
        if hq_location:
            geo_id = await self._facets.resolve_geo_urn(hq_location)
            if not geo_id:
                raise FilterValidationError(
                    f"Could not resolve hq_location {hq_location!r} to a "
                    f"LinkedIn region. Use a country or city name as it "
                    f"appears in LinkedIn's location dropdown."
                )

        base_url = build_company_search_url(
            keywords,
            industry_ids=ids,
            size_letters=size_letters,
            geo_id=geo_id,
            has_jobs=bool(has_jobs),
        )

        paged = await paginate_search(
            self._capture,
            self._session,
            base_url,
            kind="company",
            max_pages=max_pages,
            pace_first=self._facets.navigated,
        )

        companies, result_count = search_rows(
            parse_company_cards, paged.pages, "company"
        )
        result: dict[str, Any] = {
            "url": base_url,
            "sections": {"search_results": "\n---\n".join(paged.page_texts)}
            if paged.page_texts
            else {},
            "companies": companies,
            "result_count": result_count,
        }
        if paged.page_references:
            result["references"] = {
                "search_results": dedupe_references(paged.page_references)
            }
        if paged.section_errors:
            result["section_errors"] = paged.section_errors
        return result
