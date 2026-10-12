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
    ENTRY_PRIMARY,
    ENTRY_SECONDARY,
    ENTRY_SECTION_LABEL,
    ENTRY_SPECS,
    SINGLE_LINE,
    EntrySummary,
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
            # A record left at APPLYING by a process that died mid-apply can be
            # closed by discarding it; its per-field results are kept.
            ChangeSetStatus.DISCARDED,
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


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """A new entry to add to a section (position, education, publication)."""

    kind: str
    fields: Mapping[str, Any]
    # Add even when an entry with the same primary/secondary text exists.
    allow_duplicate: bool = False


_DATE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def entry_values(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical stored form of an entry's values (stable key order)."""
    return {k: fields[k] for k in sorted(fields) if fields[k] is not None}


def _validate_entry(
    req: EntryRequest, label: str, problems: list[dict[str, Any]]
) -> dict[str, Any]:
    specs = {s.name: s for s in ENTRY_SPECS[req.kind]}
    out: dict[str, Any] = {}
    for name, raw in req.fields.items():
        spec = specs.get(name)
        where = f"{label}.{name}"
        if spec is None:
            problems.append(
                {
                    "field": where,
                    "reason": f"not a {req.kind} field",
                    "allowed": sorted(specs),
                }
            )
            continue
        if raw is None:
            continue
        if spec.control == "flag":
            if not isinstance(raw, bool):
                problems.append({"field": where, "reason": "must be true or false"})
                continue
            out[name] = raw
        elif spec.control in {"month", "year"}:
            if isinstance(raw, bool) or not isinstance(raw, int):
                problems.append({"field": where, "reason": "must be a number"})
                continue
            lo, hi = (1, 12) if spec.control == "month" else (1900, 2100)
            if not lo <= raw <= hi:
                problems.append({"field": where, "reason": f"must be {lo}-{hi}"})
                continue
            out[name] = raw
        else:
            if not isinstance(raw, str):
                problems.append({"field": where, "reason": "must be text"})
                continue
            value = normalize_text(raw)
            if not value:
                continue
            if _CONTROL.search(value):
                problems.append(
                    {"field": where, "reason": "contains control characters"}
                )
            if not spec.multiline and "\n" in value:
                problems.append({"field": where, "reason": "must be a single line"})
            if spec.limit and (p := _length_problem(where, value, spec.limit)):
                problems.append(p)
            if spec.control == "date":
                m = _DATE.match(value)
                if not m or not (1 <= int(m[1]) <= 12 and 1 <= int(m[2]) <= 31):
                    problems.append({"field": where, "reason": "must be mm/dd/yyyy"})
            if name == "url" and not re.match(r"^https?://\S+$", value):
                problems.append({"field": where, "reason": "must be an http(s) URL"})
            out[name] = value
    for spec in ENTRY_SPECS[req.kind]:
        if spec.required and spec.name not in out:
            problems.append({"field": f"{label}.{spec.name}", "reason": "is required"})
    # Date consistency. A month needs its year; an end needs a start; an end
    # is not before its start; a current position has no end.
    for side in ("start", "end"):
        if f"{side}Month" in out and f"{side}Year" not in out:
            problems.append(
                {"field": f"{label}.{side}Month", "reason": f"needs {side}Year"}
            )
    if "endYear" in out and "startYear" not in out:
        problems.append({"field": f"{label}.endYear", "reason": "needs startYear"})
    if "endYear" in out and "startYear" in out:
        start = (out["startYear"], out.get("startMonth", 1))
        end = (out["endYear"], out.get("endMonth", 12))
        if end < start:
            problems.append(
                {"field": f"{label}.endYear", "reason": "is before the start"}
            )
    if req.kind == "position":
        current = out.get("current", "endYear" not in out)
        if current and ("endYear" in out or "endMonth" in out):
            problems.append(
                {
                    "field": f"{label}.current",
                    "reason": "a current position has no end date",
                }
            )
        out["current"] = current
    return out


def _looks_like(
    kind: str, values: Mapping[str, Any], existing: Sequence[EntrySummary]
) -> EntrySummary | None:
    """An existing entry whose text holds this entry's primary (and secondary) value."""
    primary = skill_key(str(values.get(ENTRY_PRIMARY[kind], "")))
    secondary = skill_key(str(values.get(ENTRY_SECONDARY[kind], "")))
    for e in existing:
        text = skill_key(" \n ".join(e.lines))
        if primary and primary in text and (not secondary or secondary in text):
            return e
    return None


def entry_label(kind: str, values: Mapping[str, Any]) -> str:
    primary = values.get(ENTRY_PRIMARY[kind], "")
    secondary = values.get(ENTRY_SECONDARY[kind])
    return f"New {kind}: {primary}" + (f" · {secondary}" if secondary else "")


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
    # The profile URL of the account the change set was planned against.
    account: str | None = None

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
            "account": self.account,
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
            account=d.get("account"),
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
    entries: Sequence[EntryRequest] = (),
    existing_entries: Mapping[str, Sequence[EntrySummary]] | None = None,
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
        if not after and t.kind in {
            "headline",
            "experience_title",
            "first_name",
            "last_name",
        }:
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

    planned_entries: list[tuple[EntryRequest, dict[str, Any]]] = []
    existing_entries = existing_entries or {}
    for n, req in enumerate(entries):
        if req.kind not in ENTRY_SPECS:
            problems.append(
                {"field": f"entries[{n}]", "reason": f"unknown kind {req.kind!r}"}
            )
            continue
        values = _validate_entry(req, f"{req.kind}[{n}]", problems)
        if any(
            r.kind == req.kind and entry_values(v) == entry_values(values)
            for r, v in planned_entries
        ):
            problems.append(
                {"field": f"{req.kind}[{n}]", "reason": "requested more than once"}
            )
            continue
        existing = existing_entries.get(req.kind, ())
        if (dup := _looks_like(req.kind, values, existing)) is not None:
            if not req.allow_duplicate:
                problems.append(
                    {
                        "field": f"{req.kind}[{n}]",
                        "reason": "looks like an existing entry; pass allowDuplicate to add anyway",
                        "existing": dup.as_dict(),
                    }
                )
                continue
            warnings.append(
                f"{entry_label(req.kind, values)} resembles an existing entry."
            )
        planned_entries.append((req, values))

    if problems:
        raise ProfileEditError(ProfileEditErrorCode.VALIDATION_ERROR, problems=problems)

    for n, (req, values) in enumerate(planned_entries):
        baseline[f"{req.kind}/entries"] = sorted(
            e.id for e in existing_entries.get(req.kind, ())
        )
        stored = entry_values(values)
        changes.append(
            FieldChange(
                key=f"{req.kind}/new/{n}",
                section=req.kind,
                kind="entry",
                label=entry_label(req.kind, stored),
                action="create",
                before=None,
                after=json.dumps(stored, ensure_ascii=False, sort_keys=True),
                target=req.kind,
            )
        )

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
    entries: Sequence[EntryRequest] = (),
    existing_entries: Mapping[str, Sequence[EntrySummary]] | None = None,
) -> ChangeSet:
    changes, baseline, warnings = plan_changes(
        texts, skills, current_skills, entries, existing_entries
    )
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
        lines.append(ENTRY_SECTION_LABEL.get(section, section).upper())
        for c in (c for c in cs.changes if c.section == section):
            if c.action == "create":
                lines.append(f"+ {c.label}")
                for k, v in json.loads(c.after or "{}").items():
                    lines.append(f"    {k}: {v}")
            elif c.action == "add":
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
