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
}

# Single-line fields; newlines are refused rather than silently joined.
SINGLE_LINE = frozenset({"headline", "experience_title", "skill"})


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
