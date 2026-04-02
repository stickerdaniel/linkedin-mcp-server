"""Scraping engine using innerText extraction."""

from .extractor import LinkedInExtractor
from .fields import (
    ALL_PERSON_SECTION_NAMES,
    COMPANY_SECTIONS,
    PERSON_SECTIONS,
    parse_company_sections,
    parse_person_sections,
)

__all__ = [
    "ALL_PERSON_SECTION_NAMES",
    "COMPANY_SECTIONS",
    "LinkedInExtractor",
    "PERSON_SECTIONS",
    "parse_company_sections",
    "parse_person_sections",
]
