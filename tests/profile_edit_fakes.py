"""An in-memory LinkedIn profile behind the ProfileEditorPort, for service tests.

It behaves like the real editor where safety depends on it: a write checks the
expected "before" value, saves only on success, and reads return what was
saved. Failures are injected per operation to model LinkedIn's bad days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from linkedin_mcp_server.profile_edit.changeset import skill_key
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    ENTRY_PRIMARY,
    EntrySummary,
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
    first_name: str = "Jane"
    last_name: str = "Doe"
    # kind -> list of (id, values); positions added this way are separate
    # from `positions` (the editable ones), which list_entries also reports.
    entries: dict[str, list[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    # Kinds whose create "saves" nothing, to model a save LinkedIn dropped.
    drop_creates: set[str] = field(default_factory=set)
    rehearsals: list[str] = field(default_factory=list)
    # (kind, entry id) -> organization text; `pages` are the names LinkedIn suggests.
    organizations: dict[tuple[str, str], str] = field(default_factory=dict)
    pages: set[str] = field(default_factory=lambda: {"Example University, Exampleland"})
    # (kind, entry id, field) -> text of existing entries' fields.
    entry_fields: dict[tuple[str, str, str], str] = field(default_factory=dict)
    _next_id: int = 500

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

    async def write_headline(
        self, *, expected: str, value: str, dry_run: bool = False
    ) -> None:
        self._check("write_headline", self.headline, expected)
        if dry_run:
            self.rehearsals.append("headline")
            return
        self.headline = self.mangle.get("headline", value)
        self.writes.append("headline")

    async def write_about(
        self, *, expected: str, value: str, dry_run: bool = False
    ) -> None:
        self._check("write_about", self.about, expected)
        if dry_run:
            self.rehearsals.append("about")
            return
        self.about = self.mangle.get("about", value)
        self.writes.append("about")

    async def write_experience(
        self,
        experience_id: str,
        *,
        field: Literal["title", "description"],
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        p = self._position(experience_id)
        self._check(f"write_experience_{field}", getattr(p, field), expected)
        if dry_run:
            self.rehearsals.append(f"experience/{experience_id}/{field}")
            return
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

    async def read_organization(self, kind: str, entry_id: str) -> TextField:
        if (kind, entry_id) not in self.organizations:
            raise ProfileEditError(ProfileEditErrorCode.EXPERIENCE_NOT_FOUND)
        return TextField(self.organizations[(kind, entry_id)])

    async def write_organization(
        self,
        kind: str,
        entry_id: str,
        *,
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        self._check(
            f"write_org_{entry_id}", self.organizations[(kind, entry_id)], expected
        )
        page = next((p for p in self.pages if skill_key(p) == skill_key(value)), None)
        if page is None:
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR, offered=sorted(self.pages)
            )
        if dry_run:
            self.rehearsals.append(f"org/{entry_id}")
            return
        self.organizations[(kind, entry_id)] = page
        self.writes.append(f"org/{entry_id}")

    async def read_entry_field(self, kind: str, entry_id: str, field: str) -> TextField:
        if (kind, entry_id, field) not in self.entry_fields:
            raise ProfileEditError(ProfileEditErrorCode.EXPERIENCE_NOT_FOUND)
        return TextField(self.entry_fields[(kind, entry_id, field)])

    async def write_entry_field(
        self,
        kind: str,
        entry_id: str,
        field: str,
        *,
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        key = (kind, entry_id, field)
        self._check(f"write_{kind}_{field}", self.entry_fields[key], expected)
        if dry_run:
            self.rehearsals.append(f"{kind}/{entry_id}/{field}")
            return
        self.entry_fields[key] = self.mangle.get(field, value)
        self.writes.append(f"{kind}/{entry_id}/{field}")

    async def describe_form(self, form: str) -> dict[str, Any]:
        return {"form": form, "dialog": True, "controls": []}

    # names
    async def read_name(self) -> tuple[TextField, TextField]:
        self._maybe_fail("read_name")
        return TextField(self.first_name), TextField(self.last_name)

    async def write_name(
        self,
        which: Literal["first", "last"],
        *,
        expected: str,
        value: str,
        dry_run: bool = False,
    ) -> None:
        attr = f"{which}_name"
        self._check(f"write_name_{which}", getattr(self, attr), expected)
        if dry_run:
            self.rehearsals.append(f"name/{which}")
            return
        setattr(self, attr, self.mangle.get(attr, value))
        self.writes.append(f"name/{which}")

    # entries
    async def list_entries(self, kind: str) -> list[EntrySummary]:
        self._maybe_fail(f"list_entries_{kind}")
        out = [
            EntrySummary(i, tuple(str(v) for v in values.values()))
            for i, values in self.entries.get(kind, [])
        ]
        if kind == "position":
            out += [
                EntrySummary(p.id, (p.title, p.company, p.date_range))
                for p in self.positions
            ]
        return out

    async def create_entry(
        self, kind: str, values: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        self._maybe_fail(f"create_{kind}")
        if dry_run:
            self.rehearsals.append(f"{kind}:{values[ENTRY_PRIMARY[kind]]}")
            return dict(values)
        if kind not in self.drop_creates:
            self._next_id += 1
            self.entries.setdefault(kind, []).append((str(self._next_id), dict(values)))
        self.writes.append(f"{kind}+{values[ENTRY_PRIMARY[kind]]}")
        return dict(values)


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
