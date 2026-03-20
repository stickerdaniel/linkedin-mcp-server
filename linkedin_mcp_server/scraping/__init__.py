"""Scraping engine using innerText extraction."""

from .extractor import LinkedInExtractor
from .fields import (
    COMPANY_SECTIONS,
    PERSON_SECTIONS,
    parse_company_sections,
    parse_person_sections,
)

__all__ = [
    "COMPANY_SECTIONS",
    "LinkedInExtractor",
    "PERSON_SECTIONS",
    "parse_company_sections",
    "parse_person_sections",
]
