"""Company profile, employee-list and company-search workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

import logging

from linkedin_mcp_server.core.exceptions import LinkedInScraperException
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.scraping.capture import (
    CaptureMode,
    CapturePlan,
    SectionCapture,
)
from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    rate_limited_section_error,
)
from linkedin_mcp_server.scraping.fields import COMPANY_SECTIONS, _company_section_specs
from linkedin_mcp_server.scraping.identifiers import (
    company_page_url,
    normalize_company_identifier,
)
from linkedin_mcp_server.scraping.link_metadata import Reference
from linkedin_mcp_server.scraping.search_urls import build_company_search_url
from linkedin_mcp_server.scraping.session import NAV_DELAY, ScrapingSession

if TYPE_CHECKING:
    from linkedin_mcp_server.callbacks import ProgressCallback

logger = logging.getLogger(__name__)


class CompanyScraper:
    """Own every workflow whose subject is a LinkedIn company page."""

    def __init__(self, session: ScrapingSession, capture: SectionCapture):
        self._session = session
        self._capture = capture

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
                    await self._session.delay(NAV_DELAY)

                section_name = spec.name
                url = base_url + spec.suffix
                try:
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
        except LinkedInScraperException as e:
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
        keywords: str,
    ) -> dict[str, Any]:
        """Search for companies and extract the results page.

        Returns:
            {url, sections: {search_results: text}}
        """
        url = build_company_search_url(keywords)
        extracted = await self._capture.capture(
            url,
            "search_results",
            CapturePlan(CaptureMode.SEARCH_RESULTS),
        )

        sections: dict[str, str] = {}
        references: dict[str, list[Reference]] = {}
        section_errors: dict[str, dict[str, Any]] = {}
        if extracted.text and extracted.text != RATE_LIMITED_SECTION_TEXT:
            sections["search_results"] = extracted.text
            if extracted.references:
                references["search_results"] = extracted.references
        elif extracted.text == RATE_LIMITED_SECTION_TEXT:
            section_errors["search_results"] = rate_limited_section_error()
        elif extracted.error:
            section_errors["search_results"] = extracted.error

        result: dict[str, Any] = {
            "url": url,
            "sections": sections,
        }
        if references:
            result["references"] = references
        if section_errors:
            result["section_errors"] = section_errors
        return result
