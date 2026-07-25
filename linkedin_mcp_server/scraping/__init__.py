"""Scraping engine using innerText extraction."""

from .extractor import LinkedInExtractor
from .fields import (
    ANALYTICS_SECTIONS,
    COMPANY_SECTIONS,
    PERSON_SECTIONS,
    parse_analytics_sections,
    parse_company_sections,
    parse_person_sections,
)

__all__ = [
    "ANALYTICS_SECTIONS",
    "COMPANY_SECTIONS",
    "LinkedInExtractor",
    "PERSON_SECTIONS",
    "parse_analytics_sections",
    "parse_company_sections",
    "parse_person_sections",
]
