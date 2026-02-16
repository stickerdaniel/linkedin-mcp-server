"""Flag enums controlling which LinkedIn pages are visited during scraping."""

import logging
from enum import Flag, auto

logger = logging.getLogger(__name__)


class PersonScrapingFields(Flag):
    """Controls which pages are visited when scraping a person profile."""

    BASIC_INFO = auto()  # /in/{username}/
    EXPERIENCE = auto()  # /in/{username}/details/experience/
    EDUCATION = auto()  # /in/{username}/details/education/
    INTERESTS = auto()  # /in/{username}/details/interests/
    HONORS = auto()  # /in/{username}/details/honors/
    LANGUAGES = auto()  # /in/{username}/details/languages/
    CONTACT_INFO = auto()  # /in/{username}/overlay/contact-info/


class CompanyScrapingFields(Flag):
    """Controls which pages are visited when scraping a company."""

    ABOUT = auto()  # /company/{name}/about/
    POSTS = auto()  # /company/{name}/posts/
    JOBS = auto()  # /company/{name}/jobs/


# Section name -> flag mapping
PERSON_SECTION_MAP: dict[str, PersonScrapingFields] = {
    "experience": PersonScrapingFields.EXPERIENCE,
    "education": PersonScrapingFields.EDUCATION,
    "interests": PersonScrapingFields.INTERESTS,
    "honors": PersonScrapingFields.HONORS,
    "languages": PersonScrapingFields.LANGUAGES,
    "contact_info": PersonScrapingFields.CONTACT_INFO,
}

COMPANY_SECTION_MAP: dict[str, CompanyScrapingFields] = {
    "posts": CompanyScrapingFields.POSTS,
    "jobs": CompanyScrapingFields.JOBS,
}


def parse_person_sections(sections: str | None) -> PersonScrapingFields:
    """Parse comma-separated section names into PersonScrapingFields.

    BASIC_INFO is always included. Empty/None returns BASIC_INFO only.
    Unknown section names are logged as warnings.
    """
    flags = PersonScrapingFields.BASIC_INFO
    if not sections:
        return flags
    for name in sections.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name in PERSON_SECTION_MAP:
            flags |= PERSON_SECTION_MAP[name]
        else:
            logger.warning(
                "Unknown person section %r ignored. Valid: %s",
                name,
                ", ".join(sorted(PERSON_SECTION_MAP)),
            )
    return flags


def parse_company_sections(sections: str | None) -> CompanyScrapingFields:
    """Parse comma-separated section names into CompanyScrapingFields.

    ABOUT is always included. Empty/None returns ABOUT only.
    Unknown section names are logged as warnings.
    """
    flags = CompanyScrapingFields.ABOUT
    if not sections:
        return flags
    for name in sections.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name in COMPANY_SECTION_MAP:
            flags |= COMPANY_SECTION_MAP[name]
        else:
            logger.warning(
                "Unknown company section %r ignored. Valid: %s",
                name,
                ", ".join(sorted(COMPANY_SECTION_MAP)),
            )
    return flags
