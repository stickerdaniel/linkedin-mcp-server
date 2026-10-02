"""An in-memory LinkedIn profile behind the ProfileEditorPort, for service tests.

It behaves like the real editor where safety depends on it: a write checks the
expected "before" value, saves only on success, and reads return what was
saved. Failures are injected per operation to model LinkedIn's bad days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from linkedin_mcp_server.profile_edit.changeset import skill_key
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    ExperienceForm,
    ExperienceSummary,
    Skill,
    TextField,
    normalize_text,
)


@dataclass
class FakePosition:
    id: str
    title: str
    company: str
    date_range: str
    description: str = ""


@dataclass
class FakeEditor:
    headline: str = "Senior Software Developer"
    about: str = "I build web applications."
    positions: list[FakePosition] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    catalogue: set[str] = field(
        default_factory=lambda: {"ReactJS", "TypeScript", "Node.js", "Python"}
    )
    fail: dict[str, BaseException] = field(default_factory=dict)
    # Saves that LinkedIn "accepts" but stores differently (verification must catch it).
    mangle: dict[str, str] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    # The signed-in account; change it to model a different login.
    account_url: str = "https://www.linkedin.com/in/jane/"
    pauses: list[float] = field(default_factory=list)

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise self.fail.pop(op)

    def _position(self, pid: str) -> FakePosition:
        for p in self.positions:
            if p.id == pid:
                return p
        raise ProfileEditError(
            ProfileEditErrorCode.EXPERIENCE_NOT_FOUND, experienceId=pid
        )

    # reads
    async def account(self) -> str:
        return self.account_url

    async def read_identity(self) -> tuple[str, str | None, str | None]:
        self._maybe_fail("read_identity")
        return "https://www.linkedin.com/in/jane/", "Jane Doe", None

    async def read_headline(self) -> TextField:
        self._maybe_fail("read_headline")
        return TextField(self.headline, 220)

    async def read_location(self) -> str | None:
        return "Edinburgh"

    async def read_about(self) -> TextField:
        self._maybe_fail("read_about")
        return TextField(self.about, 2600)

    async def list_experiences(self) -> list[ExperienceSummary]:
        return [
            ExperienceSummary(p.id, p.title, p.company, None, p.date_range)
            for p in self.positions
        ]

    async def read_experience(self, experience_id: str) -> ExperienceForm:
        p = self._position(experience_id)
        return ExperienceForm(
            p.id, TextField(p.title, 100), TextField(p.description, 2000), p.company
        )

    async def list_skills(self) -> list[Skill]:
        return [Skill(s, i + 1, str(1000 + i)) for i, s in enumerate(self.skills)]

    # writes
    def _check(self, op: str, current: str, expected: str) -> None:
        self._maybe_fail(op)
        if normalize_text(current) != normalize_text(expected):
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                field=op,
                expected=expected,
                actual=current,
            )

    async def write_headline(self, *, expected: str, value: str) -> None:
        self._check("write_headline", self.headline, expected)
        self.headline = self.mangle.get("headline", value)
        self.writes.append("headline")

    async def write_about(self, *, expected: str, value: str) -> None:
        self._check("write_about", self.about, expected)
        self.about = self.mangle.get("about", value)
        self.writes.append("about")

    async def write_experience(
        self,
        experience_id: str,
        *,
        field: Literal["title", "description"],
        expected: str,
        value: str,
    ) -> None:
        p = self._position(experience_id)
        self._check(f"write_experience_{field}", getattr(p, field), expected)
        setattr(p, field, value)
        self.writes.append(f"experience/{experience_id}/{field}")

    async def add_skill(self, name: str) -> str:
        self._maybe_fail("add_skill")
        canonical = next(
            (c for c in self.catalogue if skill_key(c) == skill_key(name)), None
        )
        if canonical is None:
            raise ProfileEditError(ProfileEditErrorCode.SKILL_NOT_FOUND, skill=name)
        self.skills.append(canonical)
        self.writes.append(f"skill+{canonical}")
        return canonical

    async def remove_skill(self, skill: Skill) -> None:
        self._maybe_fail("remove_skill")
        self.skills = [s for s in self.skills if skill_key(s) != skill_key(skill.name)]
        self.writes.append(f"skill-{skill.name}")

    async def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)


def similar_positions() -> list[FakePosition]:
    """Two roles with the same title at the same company, and one elsewhere."""
    return [
        FakePosition(
            "101",
            "Senior Software Developer",
            "IPG Automotive",
            "Jan 2023 - Present",
            "Simulation tooling.",
        ),
        FakePosition(
            "102",
            "Senior Software Developer",
            "IPG Automotive",
            "Mar 2021 - Dec 2022",
            "Data pipelines.",
        ),
        FakePosition(
            "103", "Lead Developer", "Liftango", "2019 - 2021", "Routing platform."
        ),
    ]
