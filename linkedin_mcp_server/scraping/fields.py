"""Section config dicts controlling which LinkedIn pages are visited during scraping."""

import logging

logger = logging.getLogger(__name__)

# Maps section name -> (url_suffix, is_overlay)
PERSON_SECTIONS: dict[str, tuple[str, bool]] = {
    "main_profile": ("/", False),
    "experience": ("/details/experience/", False),
    "education": ("/details/education/", False),
    "skills": ("/details/skills/", False),
    "certifications": ("/details/certifications/", False),
    "volunteer": ("/details/volunteering-experiences/", False),
    "projects": ("/details/projects/", False),
    "publications": ("/details/publications/", False),
    "courses": ("/details/courses/", False),
    "recommendations": ("/details/recommendations/", False),
    "organizations": ("/details/organizations/", False),
    "interests": ("/details/interests/", False),
    "honors": ("/details/honors/", False),
    "languages": ("/details/languages/", False),
    "contact_info": ("/overlay/contact-info/", True),
    "posts": ("/recent-activity/all/", False),
}

ALL_PERSON_SECTION_NAMES: list[str] = [
    name for name in PERSON_SECTIONS if name != "main_profile"
]

COMPANY_SECTIONS: dict[str, tuple[str, bool]] = {
    "about": ("/about/", False),
    "posts": ("/posts/", False),
    "jobs": ("/jobs/", False),
}


def parse_person_sections(
    sections: str | None,
) -> tuple[set[str], list[str]]:
    """Parse comma-separated section names into a set of requested sections.

    "main_profile" is always included. Empty/None returns {"main_profile"} only.
    Unknown section names are logged as warnings and returned.

    Returns:
        Tuple of (requested_sections, unknown_section_names).
    """
    requested: set[str] = {"main_profile"}
    unknown: list[str] = []
    if not sections:
        return requested, unknown
    for name in sections.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name in PERSON_SECTIONS:
            requested.add(name)
        else:
            unknown.append(name)
            logger.warning(
                "Unknown person section %r ignored. Valid: %s",
                name,
                ", ".join(sorted(PERSON_SECTIONS)),
            )
    return requested, unknown


def parse_company_sections(
    sections: str | None,
) -> tuple[set[str], list[str]]:
    """Parse comma-separated section names into a set of requested sections.

    "about" is always included. Empty/None returns {"about"} only.
    Unknown section names are logged as warnings and returned.

    Returns:
        Tuple of (requested_sections, unknown_section_names).
    """
    requested: set[str] = {"about"}
    unknown: list[str] = []
    if not sections:
        return requested, unknown
    for name in sections.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name in COMPANY_SECTIONS:
            requested.add(name)
        else:
            unknown.append(name)
            logger.warning(
                "Unknown company section %r ignored. Valid: %s",
                name,
                ", ".join(sorted(COMPANY_SECTIONS)),
            )
    return requested, unknown
