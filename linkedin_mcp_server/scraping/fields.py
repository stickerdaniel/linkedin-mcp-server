"""Section config dicts controlling which LinkedIn pages are visited during scraping."""

import logging

logger = logging.getLogger(__name__)

# Maps section name -> (url_suffix, is_overlay)
PERSON_SECTIONS: dict[str, tuple[str, bool]] = {
    "main_profile": ("/", False),
    "experience": ("/details/experience/", False),
    "education": ("/details/education/", False),
    "interests": ("/details/interests/", False),
    "honors": ("/details/honors/", False),
    "languages": ("/details/languages/", False),
    "certifications": ("/details/certifications/", False),
    "skills": ("/details/skills/", False),
    "projects": ("/details/projects/", False),
    "contact_info": ("/overlay/contact-info/", True),
    "posts": ("/recent-activity/all/", False),
    "comments": ("/recent-activity/comments/", False),
}

COMPANY_SECTIONS: dict[str, tuple[str, bool]] = {
    "about": ("/about/", False),
    "posts": ("/posts/", False),
    "jobs": ("/jobs/", False),
}

# Maps section name -> url_suffix under https://www.linkedin.com/analytics.
# These are the authenticated user's own analytics dashboards ("Private to
# you"); they have no per-username variant. /analytics/creator/ (overview)
# is omitted because LinkedIn redirects it to the content page.
ANALYTICS_SECTIONS: dict[str, str] = {
    "content": "/creator/content/",
    "audience": "/creator/audience/",
    "top_posts": "/creator/top-posts/",
    "profile_views": "/profile-views/",
    "search_appearances": "/search-appearances/",
}

# Sections whose page honours the ?timeRange= query param (verified live
# 2026-07-07 on content and audience; the other pages use fixed windows).
ANALYTICS_TIME_RANGE_SECTIONS: frozenset[str] = frozenset({"content", "audience"})


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


def parse_analytics_sections(
    sections: str | None,
) -> tuple[set[str], list[str]]:
    """Parse comma-separated section names into a set of requested sections.

    Unlike the profile parsers there is no always-included baseline: the
    tool exists to pull the analytics dashboards, so empty/None selects ALL
    sections. Unknown section names are logged as warnings and returned.

    Returns:
        Tuple of (requested_sections, unknown_section_names).
    """
    if not sections:
        return set(ANALYTICS_SECTIONS), []
    requested: set[str] = set()
    unknown: list[str] = []
    for name in sections.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name in ANALYTICS_SECTIONS:
            requested.add(name)
        else:
            unknown.append(name)
            logger.warning(
                "Unknown analytics section %r ignored. Valid: %s",
                name,
                ", ".join(sorted(ANALYTICS_SECTIONS)),
            )
    if not requested:
        requested = set(ANALYTICS_SECTIONS)
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
