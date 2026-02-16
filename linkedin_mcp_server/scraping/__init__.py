"""Scraping engine using innerText extraction."""

from .extractor import LinkedInExtractor
from .fields import (
    CompanyScrapingFields,
    PersonScrapingFields,
    parse_company_sections,
    parse_person_sections,
)

__all__ = [
    "CompanyScrapingFields",
    "LinkedInExtractor",
    "PersonScrapingFields",
    "parse_company_sections",
    "parse_person_sections",
]
