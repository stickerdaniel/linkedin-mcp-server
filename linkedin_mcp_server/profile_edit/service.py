"""READ -> PROPOSE -> PREVIEW -> APPLY -> VERIFY, against an editor port.

The service never decides content. It carries values the caller supplied,
refuses anything it cannot target unambiguously, and only writes from a stored,
pending change set whose baseline still matches LinkedIn.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import logging

from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.profile_edit.changeset import (
    ChangeSet,
    ChangeSetStatus,
    FieldChange,
    SkillsRequest,
    TextRequest,
    build_change_set,
    render_diff,
    skill_key,
    stale_fields,
)
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.model import (
    ExperienceForm,
    ExperienceSummary,
    OwnProfile,
    Skill,
    TextField,
    normalize_text,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore

logger = logging.getLogger(__name__)

ExperienceField = Literal["title", "description"]


class ProfileEditorPort(Protocol):
    """What the service needs from LinkedIn. Implemented by linkedin.profile_editor."""

    async def read_identity(self) -> tuple[str, str | None, str | None]: ...
    async def read_headline(self) -> TextField: ...
    async def read_location(self) -> str | None: ...
    async def read_about(self) -> TextField: ...
    async def list_experiences(self) -> list[ExperienceSummary]: ...
    async def read_experience(self, experience_id: str) -> ExperienceForm: ...
    async def list_skills(self) -> list[Skill]: ...
    async def write_headline(self, *, expected: str, value: str) -> None: ...
    async def write_about(self, *, expected: str, value: str) -> None: ...
    async def write_experience(
        self, experience_id: str, *, field: ExperienceField, expected: str, value: str
    ) -> None: ...
    async def add_skill(self, name: str) -> str: ...
    async def remove_skill(self, skill: Skill) -> None: ...
    async def pause(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class ExperienceEdit:
    experience_id: str | None = None
    company: str | None = None
    match_title: str | None = None
    start_date: str | None = None
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Proposal:
    headline: str | None = None
    about: str | None = None
    experiences: Sequence[ExperienceEdit] = ()
    skills_add: Sequence[str] = ()
    skills_remove: Sequence[str] = ()

    def is_empty(self) -> bool:
        return (
            self.headline is None
            and self.about is None
            and not any(
                e.title is not None or e.description is not None
                for e in self.experiences
            )
            and not self.skills_add
            and not self.skills_remove
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _casefold(v: str | None) -> str:
    return " ".join((v or "").casefold().split())


def resolve_experience(
    ref: ExperienceEdit, listed: Sequence[ExperienceSummary]
) -> ExperienceSummary:
    """Find exactly one experience, or refuse. Array position is never used."""

    def candidates(items: Sequence[ExperienceSummary]) -> list[dict[str, Any]]:
        return [e.as_dict() for e in items]

    if ref.experience_id:
        found = [e for e in listed if e.id == ref.experience_id]
        if not found:
            raise ProfileEditError(
                ProfileEditErrorCode.EXPERIENCE_NOT_FOUND,
                experienceId=ref.experience_id,
                candidates=candidates(listed),
            )
        match = found[0]
    else:
        if not (ref.company or ref.match_title):
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR,
                "Each experience edit needs an experienceId, or a company and/or title to match.",
            )
        found = [
            e
            for e in listed
            if (not ref.company or _casefold(ref.company) == _casefold(e.company))
            and (
                not ref.match_title or _casefold(ref.match_title) == _casefold(e.title)
            )
            and (
                not ref.start_date
                or _casefold(ref.start_date) in _casefold(e.date_range)
            )
        ]
        if not found:
            raise ProfileEditError(
                ProfileEditErrorCode.EXPERIENCE_NOT_FOUND,
                company=ref.company,
                title=ref.match_title,
                startDate=ref.start_date,
                candidates=candidates(listed),
            )
        if len(found) > 1:
            raise ProfileEditError(
                ProfileEditErrorCode.AMBIGUOUS_EXPERIENCE,
                candidates=candidates(found),
            )
        match = found[0]
    if not match.editable:
        raise ProfileEditError(
            ProfileEditErrorCode.UNSUPPORTED_FIELD,
            "LinkedIn shows no edit control for this experience.",
            experienceId=match.id,
        )
    return match


@dataclass(slots=True)
class _Reads:
    """Per-operation cache, so one experience form is opened once per step."""

    editor: ProfileEditorPort
    experiences: dict[str, ExperienceForm] = field(default_factory=dict)

    async def experience(self, experience_id: str) -> ExperienceForm:
        if experience_id not in self.experiences:
            self.experiences[experience_id] = await self.editor.read_experience(
                experience_id
            )
        return self.experiences[experience_id]


class ProfileEditService:
    def __init__(
        self,
        editor: ProfileEditorPort | None,
        store: ProfileEditStore,
        *,
        writes_enabled: Callable[[], bool],
        pacing_seconds: float,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self._maybe_editor = editor
        self._store = store
        self._writes_enabled = writes_enabled
        self._pacing = pacing_seconds
        self._clock = clock

    @property
    def _editor(self) -> ProfileEditorPort:
        # None only for the browser-free calls (discard, apply prechecks).
        if self._maybe_editor is None:
            raise RuntimeError("this operation needs a LinkedIn page")
        return self._maybe_editor

    # ── READ ────────────────────────────────────────────────────────────────
    async def get_profile(self) -> dict[str, Any]:
        url, name, location = await self._editor.read_identity()
        headline = await self._editor.read_headline()
        profile = OwnProfile(
            url=url,
            name=name,
            headline=headline,
            about=await self._editor.read_about(),
            location=location or await self._editor.read_location(),
            experiences=await self._editor.list_experiences(),
            skills=await self._editor.list_skills(),
        )
        return profile.as_dict()

    async def get_experiences(self, experience_id: str | None = None) -> dict[str, Any]:
        listed = await self._editor.list_experiences()
        if experience_id is None:
            return {"experiences": [e.as_dict() for e in listed]}
        summary = resolve_experience(
            ExperienceEdit(experience_id=experience_id), listed
        )
        form = await self._editor.read_experience(summary.id)
        return {
            "experience": {
                **summary.as_dict(),
                "title": form.title.value,
                "description": form.description.value,
                "limits": {
                    "title": form.title.limit("experience_title"),
                    "description": form.description.limit("experience_description"),
                },
            }
        }

    async def get_skills(self) -> dict[str, Any]:
        return {"skills": [s.as_dict() for s in await self._editor.list_skills()]}

    # ── PROPOSE ─────────────────────────────────────────────────────────────
    async def propose(self, proposal: Proposal) -> dict[str, Any]:
        if proposal.is_empty():
            raise ProfileEditError(
                ProfileEditErrorCode.VALIDATION_ERROR, "No changes were requested."
            )
        reads = _Reads(self._editor)
        texts: list[TextRequest] = []
        if proposal.headline is not None:
            h = await self._editor.read_headline()
            texts.append(
                TextRequest(
                    "headline",
                    "headline",
                    "headline",
                    "Headline",
                    proposal.headline,
                    h.value,
                    h.max_length,
                )
            )
        if proposal.about is not None:
            a = await self._editor.read_about()
            texts.append(
                TextRequest(
                    "about",
                    "about",
                    "about",
                    "About",
                    proposal.about,
                    a.value,
                    a.max_length,
                )
            )
        if proposal.experiences:
            listed = await self._editor.list_experiences()
            for edit in proposal.experiences:
                exp = resolve_experience(edit, listed)
                form = await reads.experience(exp.id)
                label = " · ".join(
                    x for x in (exp.company, exp.title, exp.date_range) if x
                )
                if edit.title is not None:
                    texts.append(
                        TextRequest(
                            f"experience/{exp.id}/title",
                            "experience",
                            "experience_title",
                            f"{label} — title",
                            edit.title,
                            form.title.value,
                            form.title.max_length,
                            exp.id,
                        )
                    )
                if edit.description is not None:
                    texts.append(
                        TextRequest(
                            f"experience/{exp.id}/description",
                            "experience",
                            "experience_description",
                            f"{label} — description",
                            edit.description,
                            form.description.value,
                            form.description.max_length,
                            exp.id,
                        )
                    )
        current_skills: list[str] = []
        if proposal.skills_add or proposal.skills_remove:
            current_skills = [s.name for s in await self._editor.list_skills()]
        cs = build_change_set(
            texts,
            SkillsRequest(proposal.skills_add, proposal.skills_remove),
            current_skills,
            now=self._clock(),
        )
        self._store.save(cs)
        self._store.audit(
            at=self._clock(),
            tool="propose_profile_changes",
            changeSetId=cs.id,
            status=str(cs.status),
        )
        return self._presented(cs)

    # ── PREVIEW ─────────────────────────────────────────────────────────────
    async def preview(self, change_set_id: str) -> dict[str, Any]:
        cs = self._store.load(change_set_id)
        out = self._presented(cs)
        if cs.status is not ChangeSetStatus.PENDING_APPROVAL:
            out["applicable"] = False
            return out
        stale = stale_fields(cs, await self._current_values(cs))
        if stale:
            cs.transition(ChangeSetStatus.STALE, self._clock())
            self._store.save(cs)
            self._store.audit(
                at=self._clock(),
                tool="preview_profile_changes",
                changeSetId=cs.id,
                status=str(cs.status),
            )
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                changeSetId=cs.id,
                changedFields=stale,
            )
        out["applicable"] = True
        out["profileUnchangedSinceProposal"] = True
        out["writesEnabled"] = self._writes_enabled()
        return out

    # ── DISCARD ─────────────────────────────────────────────────────────────
    def discard(self, change_set_id: str) -> dict[str, Any]:
        cs = self._store.load(change_set_id)
        cs.transition(ChangeSetStatus.DISCARDED, self._clock())
        self._store.save(cs)
        self._store.audit(
            at=self._clock(),
            tool="discard_profile_changes",
            changeSetId=cs.id,
            status=str(cs.status),
        )
        return {"changeSetId": cs.id, "status": str(cs.status)}

    # ── APPLY ───────────────────────────────────────────────────────────────
    def precheck_apply(self, change_set_id: str, *, confirm: bool) -> ChangeSet:
        """Every refusal that needs no browser: run before one is acquired."""
        cs = self._store.load(change_set_id)
        if cs.status is not ChangeSetStatus.PENDING_APPROVAL:
            raise ProfileEditError(
                ProfileEditErrorCode.CHANGE_SET_NOT_PENDING,
                f"This change set is {cs.status}; a change set is applied at most once.",
                changeSetId=cs.id,
                status=str(cs.status),
            )
        if confirm is not True:
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                result="refused",
                error="CONFIRMATION_REQUIRED",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.CONFIRMATION_REQUIRED, changeSetId=cs.id
            )
        if not self._writes_enabled():
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                result="refused",
                error="WRITES_DISABLED",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.WRITES_DISABLED, changeSetId=cs.id
            )
        return cs

    async def apply(self, change_set_id: str, *, confirm: bool) -> dict[str, Any]:
        cs = self.precheck_apply(change_set_id, confirm=confirm)
        try:
            current = await self._current_values(cs)
        except (RateLimitError, AccountRestrictedError) as e:
            raise ProfileEditError(
                ProfileEditErrorCode.AUTHENTICATION_REQUIRED, detail=type(e).__name__
            ) from e
        stale = stale_fields(cs, current)
        if stale:
            cs.transition(ChangeSetStatus.STALE, self._clock())
            self._store.save(cs)
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                status=str(cs.status),
                error="STALE_CHANGE_SET",
            )
            raise ProfileEditError(
                ProfileEditErrorCode.STALE_CHANGE_SET,
                changeSetId=cs.id,
                changedFields=stale,
            )

        cs.snapshot_path = str(
            self._store.save_snapshot(
                self._clock(), cs.id, {k: current[k] for k in cs.baseline}
            )
        )
        cs.transition(ChangeSetStatus.APPLYING, self._clock())
        self._store.save(cs)

        results: list[dict[str, Any]] = []
        stop: ProfileEditError | None = None
        skills = (
            {skill_key(s.name): s for s in await self._editor.list_skills()}
            if any(c.section == "skills" for c in cs.changes)
            else {}
        )
        for i, change in enumerate(cs.changes):
            if stop is not None:
                results.append(
                    {"field": change.key, "status": "NOT_ATTEMPTED", "verified": False}
                )
                continue
            if i:
                await self._editor.pause(self._pacing)
            try:
                results.append(await self._apply_one(change, skills))
            except ProfileEditError as e:
                stop = e
                results.append(
                    {
                        "field": change.key,
                        "status": "FAILED",
                        "verified": False,
                        "error": str(e.code),
                        "message": e.message,
                        **({"details": e.details} if e.details else {}),
                    }
                )
            except (AuthenticationError, RateLimitError, AccountRestrictedError) as e:
                stop = ProfileEditError(
                    ProfileEditErrorCode.AUTHENTICATION_REQUIRED,
                    detail=type(e).__name__,
                )
                results.append(
                    {
                        "field": change.key,
                        "status": "FAILED",
                        "verified": False,
                        "error": str(stop.code),
                        "message": stop.message,
                    }
                )
            except Exception as e:  # recorded, never retried
                logger.exception("Unexpected failure applying %s", change.key)
                stop = ProfileEditError(
                    ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
                    f"Unexpected {type(e).__name__} while applying {change.label}.",
                )
                results.append(
                    {
                        "field": change.key,
                        "status": "FAILED",
                        "verified": False,
                        "error": str(stop.code),
                        "message": stop.message,
                    }
                )
            self._store.audit(
                at=self._clock(),
                tool="apply_profile_changes",
                changeSetId=cs.id,
                section=change.section,
                field=change.key,
                result=results[-1]["status"],
                verified=results[-1]["verified"],
                **({"error": results[-1]["error"]} if "error" in results[-1] else {}),
            )

        done = [r for r in results if r["verified"]]
        if stop is None:
            final = ChangeSetStatus.APPLIED
        elif done:
            final = ChangeSetStatus.PARTIAL_FAILURE
        elif stop.code is ProfileEditErrorCode.STALE_CHANGE_SET:
            final = ChangeSetStatus.STALE
        else:
            final = ChangeSetStatus.FAILED
        cs.results = results
        cs.transition(final, self._clock())
        self._store.save(cs)
        self._store.audit(
            at=self._clock(),
            tool="apply_profile_changes",
            changeSetId=cs.id,
            status=str(final),
        )

        out: dict[str, Any] = {
            "changeSetId": cs.id,
            "status": str(final),
            "results": results,
            "snapshotPath": cs.snapshot_path,
        }
        if final is not ChangeSetStatus.APPLIED:
            out["error"] = str(
                ProfileEditErrorCode.PARTIAL_FAILURE
                if done
                else (stop.code if stop else ProfileEditErrorCode.LINKEDIN_SAVE_FAILED)
            )
            out["recovery"] = (
                "Fields marked UPDATED/ADDED/REMOVED are live and verified. Nothing was rolled back. "
                "The snapshot holds the values from before this apply, for a manual restore; "
                "propose a new change set for anything still to do."
            )
        return out

    async def _apply_one(
        self, change: FieldChange, skills: dict[str, Skill]
    ) -> dict[str, Any]:
        if change.key == "headline":
            await self._editor.write_headline(
                expected=change.before or "", value=change.after or ""
            )
            observed = normalize_text((await self._editor.read_headline()).value)
        elif change.key == "about":
            await self._editor.write_about(
                expected=change.before or "", value=change.after or ""
            )
            observed = normalize_text((await self._editor.read_about()).value)
        elif change.section == "experience" and change.target:
            field_name: ExperienceField = (
                "title" if change.key.endswith("/title") else "description"
            )
            await self._editor.write_experience(
                change.target,
                field=field_name,
                expected=change.before or "",
                value=change.after or "",
            )
            form = await self._editor.read_experience(change.target)
            observed = normalize_text(
                (form.title if field_name == "title" else form.description).value
            )
        elif change.action == "add" and change.after:
            canonical = await self._editor.add_skill(change.after)
            now = {skill_key(s.name): s.name for s in await self._editor.list_skills()}
            if skill_key(change.after) not in now:
                raise ProfileEditError(
                    ProfileEditErrorCode.VERIFICATION_FAILED,
                    field=change.key,
                    expected=change.after,
                    observedSkills=list(now.values()),
                )
            return {
                "field": change.key,
                "status": "ADDED",
                "verified": True,
                "value": canonical,
            }
        elif change.action == "remove" and change.before:
            skill = skills.get(skill_key(change.before))
            if skill is None:
                raise ProfileEditError(
                    ProfileEditErrorCode.SKILL_NOT_FOUND, skill=change.before
                )
            await self._editor.remove_skill(skill)
            now_keys = {skill_key(s.name) for s in await self._editor.list_skills()}
            if skill_key(change.before) in now_keys:
                raise ProfileEditError(
                    ProfileEditErrorCode.VERIFICATION_FAILED,
                    field=change.key,
                    expected="removed",
                )
            return {"field": change.key, "status": "REMOVED", "verified": True}
        else:
            raise ProfileEditError(
                ProfileEditErrorCode.UNSUPPORTED_FIELD, field=change.key
            )
        if observed != (change.after or ""):
            raise ProfileEditError(
                ProfileEditErrorCode.VERIFICATION_FAILED,
                field=change.key,
                expected=change.after,
                observed=observed,
            )
        return {"field": change.key, "status": "UPDATED", "verified": True}

    # ── helpers ─────────────────────────────────────────────────────────────
    async def _current_values(self, cs: ChangeSet) -> dict[str, Any]:
        reads = _Reads(self._editor)
        current: dict[str, Any] = {}
        for key in cs.baseline:
            if key == "headline":
                current[key] = normalize_text(
                    (await self._editor.read_headline()).value
                )
            elif key == "about":
                current[key] = normalize_text((await self._editor.read_about()).value)
            elif key.startswith("experience/"):
                _, exp_id, field_name = key.split("/", 2)
                try:
                    form = await reads.experience(exp_id)
                except ProfileEditError as e:
                    if e.code is ProfileEditErrorCode.EXPERIENCE_NOT_FOUND:
                        continue  # reported by stale_fields as no longer readable
                    raise
                current[key] = normalize_text(
                    (form.title if field_name == "title" else form.description).value
                )
            elif key == "skills":
                current[key] = sorted(
                    skill_key(s.name) for s in await self._editor.list_skills()
                )
        return current

    def _presented(self, cs: ChangeSet) -> dict[str, Any]:
        sections = list(dict.fromkeys(c.section for c in cs.changes))
        warnings = list(cs.warnings)
        for c in cs.changes:
            if c.after and c.max_length and len(c.after) > 0.9 * c.max_length:
                warnings.append(
                    f"{c.label}: {len(c.after)}/{c.max_length} characters, close to LinkedIn's limit."
                )
        return {
            "changeSetId": cs.id,
            "status": str(cs.status),
            "createdAt": cs.created_at,
            "affectedSections": sections,
            "changes": [c.as_dict() for c in cs.changes],
            "diff": render_diff(cs),
            "warnings": warnings,
            "unsupported": [],
            "note": "Nothing has been changed on LinkedIn. apply_profile_changes(changeSetId, confirm=true) applies exactly these changes after the user approves them.",
        }
