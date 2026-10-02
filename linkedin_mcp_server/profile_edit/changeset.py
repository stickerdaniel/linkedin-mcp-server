"""Change sets: what a proposal would change, against which baseline.

A change set is created from the profile as read at proposal time and records
three things that make application safe: the exact before/after of every
field, a fingerprint of the baseline it was planned against, and a status that
only moves forward through ``_TRANSITIONS``. Nothing here touches LinkedIn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import hashlib
import json
import re
import secrets
import unicodedata

from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    DEFAULT_LIMITS,
    SINGLE_LINE,
    normalize_text,
)


class ChangeSetStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    DISCARDED = "DISCARDED"
    STALE = "STALE"


_TRANSITIONS: dict[ChangeSetStatus, frozenset[ChangeSetStatus]] = {
    ChangeSetStatus.PENDING_APPROVAL: frozenset(
        {ChangeSetStatus.APPLYING, ChangeSetStatus.DISCARDED, ChangeSetStatus.STALE}
    ),
    ChangeSetStatus.APPLYING: frozenset(
        {
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.PARTIAL_FAILURE,
            ChangeSetStatus.FAILED,
            # Stale is found inside the apply step too: the editor checks the
            # visible value again before typing, after the pre-flight read.
            ChangeSetStatus.STALE,
        }
    ),
    ChangeSetStatus.STALE: frozenset({ChangeSetStatus.DISCARDED}),
    ChangeSetStatus.APPLIED: frozenset(),
    ChangeSetStatus.PARTIAL_FAILURE: frozenset(),
    ChangeSetStatus.FAILED: frozenset(),
    ChangeSetStatus.DISCARDED: frozenset(),
}

# Control characters other than newline and tab are never valid profile text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One intended edit.

    ``key`` names the field uniquely and is what baselines and results are
    keyed on: ``headline``, ``about``, ``experience/<id>/title``,
    ``experience/<id>/description``, ``skills/add/<name>``,
    ``skills/remove/<name>``.
    """

    key: str
    section: str
    kind: str
    label: str
    action: str  # set | add | remove
    before: str | None
    after: str | None
    target: str | None = None
    max_length: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.key,
            "section": self.section,
            "label": self.label,
            "action": self.action,
            "target": self.target,
            "before": self.before,
            "after": self.after,
            "maxLength": self.max_length,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FieldChange:
        return cls(
            key=d["field"],
            section=d["section"],
            kind=d.get("kind", d["section"]),
            label=d["label"],
            action=d["action"],
            before=d["before"],
            after=d["after"],
            target=d.get("target"),
            max_length=d.get("maxLength"),
        )


@dataclass(frozen=True, slots=True)
class TextRequest:
    """A requested value for one text field, with the value it replaces."""

    key: str
    section: str
    kind: str
    label: str
    after: str
    current: str
    max_length: int | None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class SkillsRequest:
    add: Sequence[str] = ()
    remove: Sequence[str] = ()


@dataclass(slots=True)
class ChangeSet:
    id: str
    created_at: str
    status: ChangeSetStatus
    changes: list[FieldChange]
    baseline: dict[str, Any]
    fingerprint: str
    field_hashes: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)
    snapshot_path: str | None = None

    def transition(self, to: ChangeSetStatus, at: str) -> None:
        if to not in _TRANSITIONS[self.status]:
            raise ProfileEditError(
                ProfileEditErrorCode.CHANGE_SET_NOT_PENDING,
                f"A change set in status {self.status} cannot move to {to}.",
                changeSetId=self.id,
                status=str(self.status),
            )
        self.history.append({"from": str(self.status), "to": str(to), "at": at})
        self.status = to

    def as_dict(self) -> dict[str, Any]:
        return {
            "changeSetId": self.id,
            "createdAt": self.created_at,
            "status": str(self.status),
            "changes": [{**c.as_dict(), "kind": c.kind} for c in self.changes],
            "baseline": self.baseline,
            "fingerprint": self.fingerprint,
            "fieldHashes": self.field_hashes,
            "warnings": self.warnings,
            "results": self.results,
            "history": self.history,
            "snapshotPath": self.snapshot_path,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ChangeSet:
        return cls(
            id=d["changeSetId"],
            created_at=d["createdAt"],
            status=ChangeSetStatus(d["status"]),
            changes=[FieldChange.from_dict(c) for c in d["changes"]],
            baseline=dict(d["baseline"]),
            fingerprint=d["fingerprint"],
            field_hashes=dict(d["fieldHashes"]),
            warnings=list(d.get("warnings", [])),
            results=list(d.get("results", [])),
            history=list(d.get("history", [])),
            snapshot_path=d.get("snapshotPath"),
        )


def new_change_set_id() -> str:
    return f"cs_{secrets.token_hex(8)}"


def _hash(value: Any) -> str:
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint(baseline: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """Whole-baseline hash plus one hash per field, so a mismatch can be named."""
    return _hash(dict(baseline)), {k: _hash(v) for k, v in baseline.items()}


def skill_key(name: str) -> str:
    """Case- and width-insensitive identity of a skill name."""
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def _check_text(
    kind: str, label: str, value: str, problems: list[dict[str, Any]]
) -> None:
    if _CONTROL.search(value):
        problems.append({"field": label, "reason": "contains control characters"})
    if kind in SINGLE_LINE and "\n" in value:
        problems.append({"field": label, "reason": "must be a single line"})


def _length_problem(label: str, value: str, limit: int) -> dict[str, Any] | None:
    if len(value) <= limit:
        return None
    return {
        "field": label,
        "reason": "too long",
        "proposedLength": len(value),
        "allowedLength": limit,
        "overflow": len(value) - limit,
    }


def plan_changes(
    texts: Sequence[TextRequest],
    skills: SkillsRequest,
    current_skills: Sequence[str],
) -> tuple[list[FieldChange], dict[str, Any], list[str]]:
    """Validate requests against current values; return changes, baseline, warnings.

    Over-long values are refused with their lengths, never truncated. A value
    equal to the current one is dropped with a warning rather than proposed.
    """
    problems: list[dict[str, Any]] = []
    warnings: list[str] = []
    changes: list[FieldChange] = []
    baseline: dict[str, Any] = {}
    seen: set[str] = set()

    for t in texts:
        if t.key in seen:
            problems.append({"field": t.label, "reason": "requested more than once"})
            continue
        seen.add(t.key)
        after = normalize_text(t.after)
        before = normalize_text(t.current)
        _check_text(t.kind, t.label, after, problems)
        if not after and t.kind in {"headline", "experience_title"}:
            problems.append({"field": t.label, "reason": "cannot be empty"})
        limit = t.max_length or DEFAULT_LIMITS[t.kind]
        if (p := _length_problem(t.label, after, limit)) is not None:
            problems.append(p)
        if after == before:
            warnings.append(
                f"{t.label}: proposed value is identical to the current one; skipped."
            )
            continue
        if not after:
            warnings.append(f"{t.label}: the proposal clears this field.")
        baseline[t.key] = before
        changes.append(
            FieldChange(
                key=t.key,
                section=t.section,
                kind=t.kind,
                label=t.label,
                action="set",
                before=before,
                after=after,
                target=t.target,
                max_length=limit,
            )
        )

    by_key = {skill_key(s): s for s in current_skills}
    add_keys: set[str] = set()
    adds: list[str] = []
    for raw in skills.add:
        name = normalize_text(raw)
        k = skill_key(name)
        if not name or k in add_keys:
            continue
        add_keys.add(k)
        _check_text("skill", f"skill '{name}'", name, problems)
        if (
            p := _length_problem(f"skill '{name}'", name, DEFAULT_LIMITS["skill"])
        ) is not None:
            problems.append(p)
        if k in by_key:
            warnings.append(
                f"Skill '{by_key[k]}' is already on the profile; not added again."
            )
            continue
        adds.append(name)
    removes: list[str] = []
    for raw in skills.remove:
        k = skill_key(raw)
        if k in add_keys:
            problems.append(
                {"field": f"skill '{raw}'", "reason": "both added and removed"}
            )
            continue
        if k not in by_key:
            raise ProfileEditError(
                ProfileEditErrorCode.SKILL_NOT_FOUND,
                f"'{raw}' is not one of the profile's skills.",
                skill=raw,
                currentSkills=list(current_skills),
            )
        if by_key[k] not in removes:
            removes.append(by_key[k])

    if problems:
        raise ProfileEditError(ProfileEditErrorCode.VALIDATION_ERROR, problems=problems)

    if adds or removes:
        baseline["skills"] = sorted(skill_key(s) for s in current_skills)
    for name in adds:
        changes.append(
            FieldChange(
                key=f"skills/add/{skill_key(name)}",
                section="skills",
                kind="skill",
                label=f"Skill: {name}",
                action="add",
                before=None,
                after=name,
                target=name,
            )
        )
    for name in removes:
        changes.append(
            FieldChange(
                key=f"skills/remove/{skill_key(name)}",
                section="skills",
                kind="skill",
                label=f"Skill: {name}",
                action="remove",
                before=name,
                after=None,
                target=name,
            )
        )
    return changes, baseline, warnings


def build_change_set(
    texts: Sequence[TextRequest],
    skills: SkillsRequest,
    current_skills: Sequence[str],
    *,
    now: str,
    change_set_id: str | None = None,
) -> ChangeSet:
    changes, baseline, warnings = plan_changes(texts, skills, current_skills)
    if not changes:
        raise ProfileEditError(
            ProfileEditErrorCode.VALIDATION_ERROR,
            "Nothing to change: every requested value already matches the profile.",
            warnings=warnings,
        )
    fp, hashes = fingerprint(baseline)
    return ChangeSet(
        id=change_set_id or new_change_set_id(),
        created_at=now,
        status=ChangeSetStatus.PENDING_APPROVAL,
        changes=changes,
        baseline=baseline,
        fingerprint=fp,
        field_hashes=hashes,
        warnings=warnings,
    )


def stale_fields(cs: ChangeSet, current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fields whose current value differs from the change set's baseline."""
    stale: list[dict[str, Any]] = []
    for key, expected in cs.baseline.items():
        if key not in current:
            stale.append({"field": key, "reason": "no longer readable"})
        elif _hash(current[key]) != cs.field_hashes[key]:
            stale.append({"field": key, "expected": expected, "actual": current[key]})
    return stale


def render_diff(cs: ChangeSet) -> str:
    """The human-readable diff shown to the user before approval."""
    lines: list[str] = []
    for section in dict.fromkeys(c.section for c in cs.changes):
        lines.append(section.upper())
        for c in (c for c in cs.changes if c.section == section):
            if c.action == "add":
                lines.append(f"+ {c.after}")
            elif c.action == "remove":
                lines.append(f"- {c.before}")
            else:
                if c.section == "experience":
                    lines.append(f"[{c.label}]")
                lines.append(f"before: {c.before or '(empty)'}")
                lines.append(f"after:  {c.after or '(empty)'}")
        lines.append("")
    return "\n".join(lines).rstrip()
