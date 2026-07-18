"""Opt-in verification helpers that exercise live LinkedIn behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from patchright.async_api import Page

from linkedin_mcp_server.drivers.browser import (
    close_browser,
    ensure_authenticated,
    get_or_create_browser,
    set_headless,
)
from linkedin_mcp_server.scraping.extractor import ExtractedSection, LinkedInExtractor

_PEOPLE_SEARCH_PATH = "/search/results/people/"


@dataclass
class _PageEvidence:
    text_characters: int
    person_urls: set[str]


class _EvidenceExtractor(LinkedInExtractor):
    """Production extractor that records PII-free evidence for each page."""

    def __init__(self, page: Page) -> None:
        super().__init__(page)
        self.page_evidence: dict[int, _PageEvidence] = {}

    async def extract_page(
        self,
        url: str,
        section_name: str,
        max_scrolls: int | None = None,
    ) -> ExtractedSection:
        extracted = await super().extract_page(url, section_name, max_scrolls)
        page_number = _page_number(url)
        if page_number is not None:
            self.page_evidence[page_number] = _PageEvidence(
                text_characters=len(extracted.text),
                person_urls={
                    reference["url"]
                    for reference in extracted.references
                    if reference["kind"] == "person"
                },
            )
        return extracted


def _page_number(url: str) -> int | None:
    parsed = urlparse(url)
    if parsed.netloc != "www.linkedin.com" or parsed.path != _PEOPLE_SEARCH_PATH:
        return None
    raw_page = parse_qs(parsed.query).get("page", ["1"])[0]
    try:
        return int(raw_page)
    except ValueError:
        return None


async def verify_live_people_search(
    keywords: str,
    *,
    location: str | None,
    max_pages: int,
    headless: bool,
) -> dict[str, Any]:
    """Run production people search and return a PII-free JSON report."""
    if not 2 <= max_pages <= 10:
        raise ValueError("max_pages must be between 2 and 10 for live verification")

    set_headless(headless)
    visited_pages: list[int] = []

    try:
        await ensure_authenticated()
        browser = await get_or_create_browser()
        page = browser.page

        def record_navigation(frame: Any) -> None:
            if frame != page.main_frame:
                return
            page_number = _page_number(frame.url)
            if page_number is not None and (
                not visited_pages or visited_pages[-1] != page_number
            ):
                visited_pages.append(page_number)

        page.on("framenavigated", record_navigation)
        extractor = _EvidenceExtractor(page)
        try:
            result = await extractor.search_people(
                keywords,
                location=location,
                max_pages=max_pages,
            )
        finally:
            page.remove_listener("framenavigated", record_navigation)

        section_text = result.get("sections", {}).get("search_results", "")
        references = result.get("references", {}).get("search_results", [])
        person_urls = [
            reference["url"]
            for reference in references
            if reference.get("kind") == "person"
        ]

        if not section_text:
            raise RuntimeError("Live search returned no search_results text")
        if result.get("section_errors"):
            raise RuntimeError("Live search returned search_results section errors")
        if not person_urls:
            raise RuntimeError("Live search returned no person references")
        if 2 not in visited_pages:
            raise RuntimeError(
                "Live search did not navigate to page=2; use broader keywords"
            )
        page_one = extractor.page_evidence.get(1)
        page_two = extractor.page_evidence.get(2)
        if page_one is None or page_two is None:
            raise RuntimeError("Live search did not extract both pages 1 and 2")
        if not page_two.text_characters or not page_two.person_urls:
            raise RuntimeError("Live search page 2 returned no usable people content")
        new_page_two_people = page_two.person_urls - page_one.person_urls
        if not new_page_two_people:
            raise RuntimeError("Live search page 2 returned no new person references")
        if len(person_urls) != len(set(person_urls)):
            raise RuntimeError("Live search returned duplicate person URLs")

        return {
            "status": "passed",
            "visited_pages": visited_pages,
            "requested_max_pages": max_pages,
            "page_result_characters": {
                str(page_number): evidence.text_characters
                for page_number, evidence in extractor.page_evidence.items()
            },
            "new_person_references_on_page_2": len(new_page_two_people),
            "search_result_characters": len(section_text),
            "unique_person_references": len(person_urls),
            "has_section_errors": bool(result.get("section_errors")),
        }
    finally:
        await close_browser()
