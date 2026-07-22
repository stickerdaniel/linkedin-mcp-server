"""Scraping engine using innerText extraction."""

from .extractor import LinkedInExtractor
from .fields import (
    COMPANY_SECTIONS,
    PERSON_SECTIONS,
    parse_company_sections,
    parse_person_sections,
)
from .skills_parser import parse_skills, skill_names_from_aria_labels

__all__ = [
    "COMPANY_SECTIONS",
    "LinkedInExtractor",
    "PERSON_SECTIONS",
    "parse_company_sections",
    "parse_person_sections",
    "parse_skills",
    "skill_names_from_aria_labels",
]
