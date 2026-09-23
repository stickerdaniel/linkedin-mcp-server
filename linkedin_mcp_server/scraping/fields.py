"""Section config dicts controlling which LinkedIn pages are visited during scraping."""

from dataclasses import dataclass

import logging

from linkedin_mcp_server.scraping.capture import CaptureMode, CapturePlan

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
}

# A page admin asking for /posts/ can be served the page-management dashboard
# instead of the member-facing feed. The dashboard paginates posts rather than
# exposing the infinite scroll that `CaptureMode.ACTIVITY` walks, so body
# scrolling cannot load the complete feed.
#
# `viewAsMember=true` opts out of that redirect and `feedView=all` selects the
# all-posts feed once there, which is the infinite scroll ACTIVITY already
# walks. Both are needed: the first defeats the dashboard, the second defeats
# the member page's default filtered view.
#
# This is the whole reason the suffix carries a query string, and
# `capture_plan_for_url` reads `urlparse(url).path`, so ACTIVITY still matches.
COMPANY_POSTS_SUFFIX = "/posts/?viewAsMember=true&feedView=all"

COMPANY_SECTIONS: dict[str, tuple[str, bool]] = {
    "about": ("/about/", False),
    "posts": (COMPANY_POSTS_SUFFIX, False),
    "jobs": ("/jobs/", False),
}


@dataclass(frozen=True)
class _SectionSpec:
    name: str
    suffix: str
    plan: CapturePlan


_PERSON_SECTION_MODES = {
    "experience": CaptureMode.DETAILS,
    "education": CaptureMode.DETAILS,
    "interests": CaptureMode.DETAILS,
    "honors": CaptureMode.DETAILS,
    "languages": CaptureMode.DETAILS,
    "certifications": CaptureMode.DETAILS,
    "skills": CaptureMode.DETAILS,
    "projects": CaptureMode.DETAILS,
    "contact_info": CaptureMode.OVERLAY,
    "posts": CaptureMode.ACTIVITY,
}
_COMPANY_SECTION_MODES = {"posts": CaptureMode.ACTIVITY}


def _person_section_specs(
    sections: dict[str, tuple[str, bool]],
    max_scrolls: int | None = None,
) -> tuple[_SectionSpec, ...]:
    return tuple(
        _SectionSpec(
            name,
            suffix,
            CapturePlan(
                CaptureMode.OVERLAY
                if is_overlay
                else _PERSON_SECTION_MODES.get(name, CaptureMode.STANDARD),
                max_scrolls,
            ),
        )
        for name, (suffix, is_overlay) in sections.items()
    )


def _company_section_specs(
    sections: dict[str, tuple[str, bool]] = COMPANY_SECTIONS,
) -> tuple[_SectionSpec, ...]:
    return tuple(
        _SectionSpec(
            name,
            suffix,
            CapturePlan(
                CaptureMode.OVERLAY
                if is_overlay
                else _COMPANY_SECTION_MODES.get(name, CaptureMode.STANDARD)
            ),
        )
        for name, (suffix, is_overlay) in sections.items()
    )


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
