"""Read model for the editable parts of the member's own profile.

Values that a change set compares against are read from LinkedIn's own edit
forms, not from the rendered profile: the profile truncates ("…see more"),
duplicates text for screen readers and reflows whitespace, while a form field
holds exactly what is stored. The form also carries ``maxlength``, which is the
character limit LinkedIn enforces today.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import re

# Used only when the form exposes no maxlength. Measured on LinkedIn's edit
# forms; a UI-reported limit always wins (see TextField.max_length).
DEFAULT_LIMITS: dict[str, int] = {
    "headline": 220,
    "about": 2600,
    "experience_title": 100,
    "experience_description": 2000,
    "skill": 80,
    # The intro form's name inputs expose no maxlength; generous on purpose.
    "first_name": 100,
    "last_name": 100,
    "organization": 100,
    # Text fields of existing entries; the form's own limit is passed with each.
    "entry_text": 255,
    "entry_text_multiline": 2000,
}

# Single-line fields; newlines are refused rather than silently joined.
SINGLE_LINE = frozenset(
    {
        "headline",
        "experience_title",
        "skill",
        "first_name",
        "last_name",
        "organization",
        "entry_text",
    }
)


@dataclass(frozen=True, slots=True)
class EntryFieldSpec:
    """One field of a new profile entry, as its add form accepts it.

    ``control`` says how the form takes the value: ``text`` (an input or
    textarea), ``typeahead`` (an input that offers suggestions; free text is
    kept when none matches exactly), ``rich`` (the rich-text description),
    ``select`` (a dropdown, matched by option value or exact text), ``month`` /
    ``year`` (date dropdowns, found by structure), ``date`` (mm/dd/yyyy text),
    ``flag`` (a checkbox). Limits were measured on the add forms on 12 October
    2026; a limit the form states itself always wins.
    """

    name: str
    control: str
    limit: int | None = None
    required: bool = False
    multiline: bool = False


ENTRY_SPECS: dict[str, tuple[EntryFieldSpec, ...]] = {
    "position": (
        EntryFieldSpec("title", "typeahead", 100, required=True),
        EntryFieldSpec("company", "typeahead", 100),
        EntryFieldSpec("location", "typeahead", 100),
        EntryFieldSpec("locationType", "select"),
        EntryFieldSpec("employmentType", "select"),
        EntryFieldSpec("current", "flag"),
        EntryFieldSpec("startMonth", "month"),
        EntryFieldSpec("startYear", "year", required=True),
        EntryFieldSpec("endMonth", "month"),
        EntryFieldSpec("endYear", "year"),
        EntryFieldSpec("description", "rich", 2000, multiline=True),
    ),
    "education": (
        EntryFieldSpec("school", "typeahead", 100, required=True),
        EntryFieldSpec("degree", "typeahead", 100),
        EntryFieldSpec("fieldOfStudy", "typeahead", 100),
        EntryFieldSpec("startMonth", "month"),
        EntryFieldSpec("startYear", "year"),
        EntryFieldSpec("endMonth", "month"),
        EntryFieldSpec("endYear", "year"),
        EntryFieldSpec("grade", "text", 80),
        EntryFieldSpec("activities", "text", 500, multiline=True),
        EntryFieldSpec("description", "text", 1000, multiline=True),
    ),
    "publication": (
        EntryFieldSpec("title", "text", 255, required=True),
        EntryFieldSpec("publisher", "text", 255),
        EntryFieldSpec("date", "date"),
        EntryFieldSpec("url", "text", 500),
        EntryFieldSpec("description", "text", 2000, multiline=True),
    ),
    "certification": (
        EntryFieldSpec("name", "typeahead", 255, required=True),
        EntryFieldSpec("issuer", "typeahead", 255, required=True),
        EntryFieldSpec("startMonth", "month"),
        EntryFieldSpec("startYear", "year"),
        EntryFieldSpec("endMonth", "month"),
        EntryFieldSpec("endYear", "year"),
        EntryFieldSpec("credentialId", "text", 80),
        EntryFieldSpec("credentialUrl", "text", 500),
    ),
    "language": (
        EntryFieldSpec("name", "typeahead", 100, required=True),
        EntryFieldSpec("proficiency", "select"),
    ),
}

# Text fields of existing entries that entryEdits may change, with their
# limits (measured on the edit forms, 12 October 2026) and whether they span
# lines.
EDITABLE_ENTRY_FIELDS: dict[str, dict[str, tuple[int, bool]]] = {
    "education": {
        "grade": (80, False),
        "activities": (500, True),
        "description": (1000, True),
    },
    "publication": {
        "title": (255, False),
        "publisher": (255, False),
        "date": (10, False),
        "url": (500, False),
        "description": (2000, True),
    },
}

# The field that names an entry, used for duplicate checks and verification.
ENTRY_PRIMARY = {
    "position": "title",
    "education": "school",
    "publication": "title",
    "certification": "name",
    "language": "name",
}
# A second field that, with the primary one, identifies a likely duplicate.
ENTRY_SECONDARY = {
    "position": "company",
    "education": "degree",
    "publication": "publisher",
    "certification": "issuer",
    "language": "name",
}
ENTRY_SECTION_LABEL = {
    "position": "Experience",
    "education": "Education",
    "publication": "Publications",
    "certification": "Licenses & certifications",
    "language": "Languages",
}


@dataclass(frozen=True, slots=True)
class EntrySummary:
    """One existing entry of a section, as listed on its details page."""

    id: str
    lines: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "lines": list(self.lines)}


def normalize_text(value: str) -> str:
    """The form of a value we propose, write and compare.

    Line endings are unified, outer whitespace removed, and a run of blank lines
    reduced to one paragraph break, because LinkedIn's editors store none of
    those distinctions: a paragraph break is an empty paragraph, which reads
    back as several newlines. Other inner whitespace is the user's text and is
    kept.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return _BLANK_LINES.sub("\n\n", text)


_BLANK_LINES = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_ANY_BREAKS = re.compile(r"\n(?:[ \t]*\n)*")


def same_saved_text(observed: str, approved: str) -> bool:
    """Whether a re-read rich-text value is the approved one as LinkedIn stores it.

    LinkedIn's rich-text editors may read a single line break back as a
    paragraph break (measured 12 October 2026: a line before each "•" came back
    with a blank line). Words and their order must match exactly; only how many
    line breaks separate two lines is not compared.
    """
    a, b = normalize_text(observed), normalize_text(approved)
    return a == b or _ANY_BREAKS.sub("\n", a) == _ANY_BREAKS.sub("\n", b)


@dataclass(frozen=True, slots=True)
class TextField:
    value: str
    max_length: int | None = None

    def limit(self, kind: str) -> int:
        return self.max_length or DEFAULT_LIMITS[kind]


@dataclass(frozen=True, slots=True)
class ExperienceSummary:
    """One position as listed on the experience page.

    ``id`` is LinkedIn's own position id, taken from the position's edit link,
    so it survives reordering and identical titles. ``editable`` is false when
    LinkedIn shows no edit link for it, in which case this server will not
    touch it.
    """

    id: str
    title: str
    company: str | None = None
    employment_type: str | None = None
    date_range: str | None = None
    location: str | None = None
    description_preview: str | None = None
    editable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExperienceForm:
    """The editable values of one position, read from its edit form."""

    id: str
    title: TextField
    description: TextField
    company: str | None = None


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    position: int
    ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "position": self.position}


@dataclass(frozen=True, slots=True)
class OwnProfile:
    """The structured profile returned by get_my_editable_profile."""

    url: str
    name: str | None
    headline: TextField
    about: TextField
    location: str | None
    experiences: list[ExperienceSummary] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "headline": self.headline.value,
            "location": self.location,
            "about": self.about.value,
            "experiences": [e.as_dict() for e in self.experiences],
            "skills": [s.as_dict() for s in self.skills],
            "limits": {
                "headline": self.headline.limit("headline"),
                "about": self.about.limit("about"),
            },
        }
