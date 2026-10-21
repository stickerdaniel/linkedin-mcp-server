"""Tools for reading and, with explicit approval, editing your own LinkedIn profile.

Only ``apply_profile_changes`` modifies LinkedIn. Every other tool here reads
LinkedIn or manages local change-set records. A change is applied only from a
stored change set, with ``confirm=true``, while the server runs with
``MCP_LINKEDIN_WRITE_ENABLED=true``, and only if the profile still matches what
the change set was planned against.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import logging

from fastmcp import Context, FastMCP
from pydantic import BaseModel, ConfigDict, Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AccountRestrictedError,
    AuthenticationError,
    RateLimitError,
)
from linkedin_mcp_server.dependencies import (
    get_ready_profile_editor,
    handle_auth_error,
)
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.profile_edit import settings
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.changeset import EntryRequest
from linkedin_mcp_server.profile_edit.service import (
    EntryFieldEdit,
    ExperienceEdit,
    OrganizationEdit,
    ProfileEditService,
    Proposal,
)
from linkedin_mcp_server.profile_edit.store import ProfileEditStore

logger = logging.getLogger(__name__)


class ExperienceMatch(BaseModel):
    model_config = ConfigDict(extra="allow")
    company: str | None = None
    title: str | None = None
    startDate: str | None = Field(
        default=None, description="Text from the date range, e.g. 'Jan 2021' or '2021'."
    )


class ExperienceChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    experienceId: str | None = Field(
        default=None, description="id from get_my_experience. Preferred over match."
    )
    match: ExperienceMatch | None = Field(
        default=None,
        description="Used only when experienceId is absent; must match exactly one experience.",
    )
    title: str | None = Field(
        default=None, description="New job title, as approved by the user."
    )
    description: str | None = Field(
        default=None, description="New description, as approved by the user."
    )


class SkillChanges(BaseModel):
    model_config = ConfigDict(extra="allow")
    add: list[str] = Field(default_factory=list, max_length=20)
    remove: list[str] = Field(default_factory=list, max_length=20)


class NewPosition(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str = Field(description="Job title.")
    company: str | None = Field(default=None, description="Organization name.")
    location: str | None = None
    locationType: str | None = Field(
        default=None, description="On-site, Hybrid or Remote (as the form shows it)."
    )
    employmentType: str | None = Field(
        default=None,
        description="e.g. Full-time, Part-time, Internship (as the form shows it).",
    )
    current: bool | None = Field(
        default=None,
        description="Currently in this role. Defaults to true when no end date is given.",
    )
    startMonth: int | None = Field(default=None, ge=1, le=12)
    startYear: int = Field(ge=1900, le=2100)
    endMonth: int | None = Field(default=None, ge=1, le=12)
    endYear: int | None = Field(default=None, ge=1900, le=2100)
    description: str | None = None
    allowDuplicate: bool = False


class NewEducation(BaseModel):
    model_config = ConfigDict(extra="allow")
    school: str
    degree: str | None = None
    fieldOfStudy: str | None = None
    startMonth: int | None = Field(default=None, ge=1, le=12)
    startYear: int | None = Field(default=None, ge=1900, le=2100)
    endMonth: int | None = Field(default=None, ge=1, le=12)
    endYear: int | None = Field(
        default=None, ge=1900, le=2100, description="Or expected graduation year."
    )
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    allowDuplicate: bool = False


class NewPublication(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str
    publisher: str | None = Field(
        default=None, description="Journal, conference or publisher."
    )
    date: str | None = Field(default=None, description="mm/dd/yyyy")
    url: str | None = Field(default=None, description="e.g. the DOI link.")
    description: str | None = None
    allowDuplicate: bool = False


class NewCertification(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    issuer: str = Field(description="Issuing organization.")
    startMonth: int | None = Field(
        default=None, ge=1, le=12, description="Issue month."
    )
    startYear: int | None = Field(
        default=None, ge=1900, le=2100, description="Issue year."
    )
    endMonth: int | None = Field(
        default=None, ge=1, le=12, description="Expiration month."
    )
    endYear: int | None = Field(
        default=None, ge=1900, le=2100, description="Expiration year."
    )
    credentialId: str | None = None
    credentialUrl: str | None = None
    allowDuplicate: bool = False


class NewLanguage(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(description="Language, as LinkedIn names it (e.g. Kazakh).")
    proficiency: str | None = Field(
        default=None,
        description="Elementary, Limited working, Professional working, Full professional or Native or bilingual proficiency (as the form shows it).",
    )
    allowDuplicate: bool = False


class EntryFieldChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["education", "publication"]
    entryId: str = Field(description="LinkedIn's id of the existing entry.")
    field: str = Field(
        description="education: grade, activities, description; publication: title, publisher, date (mm/dd/yyyy), url, description."
    )
    value: str


class OrganizationChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: Literal["position", "education"]
    entryId: str = Field(description="LinkedIn's id of the existing entry.")
    name: str = Field(
        description="The organization's exact LinkedIn page name; only an exact suggestion is selected."
    )


class ProfileChanges(BaseModel):
    # Extra keys are accepted so an unsupported field (location, featured,
    # ...) is reported back as UNSUPPORTED_FIELD instead of failing schema
    # validation with no explanation.
    model_config = ConfigDict(extra="allow")
    firstName: str | None = None
    lastName: str | None = None
    headline: str | None = None
    about: str | None = None
    experiences: list[ExperienceChange] = Field(default_factory=list, max_length=20)
    skills: SkillChanges | None = None
    newPositions: list[NewPosition] = Field(default_factory=list, max_length=10)
    newEducation: list[NewEducation] = Field(default_factory=list, max_length=10)
    newPublications: list[NewPublication] = Field(default_factory=list, max_length=20)
    organizations: list[OrganizationChange] = Field(default_factory=list, max_length=10)
    newCertifications: list[NewCertification] = Field(
        default_factory=list, max_length=10
    )
    newLanguages: list[NewLanguage] = Field(default_factory=list, max_length=10)
    entryEdits: list[EntryFieldChange] = Field(default_factory=list, max_length=10)


def _entry_requests(changes: ProfileChanges) -> list[EntryRequest]:
    out: list[EntryRequest] = []
    for kind, items in (
        ("position", changes.newPositions),
        ("education", changes.newEducation),
        ("publication", changes.newPublications),
        ("certification", changes.newCertifications),
        ("language", changes.newLanguages),
    ):
        for item in items:
            fields = item.model_dump(exclude={"allowDuplicate"}, exclude_none=True)
            for k in item.model_extra or {}:
                fields.pop(k, None)
            out.append(EntryRequest(kind, fields, allow_duplicate=item.allowDuplicate))
    return out


def _unsupported_fields(changes: ProfileChanges) -> list[str]:
    """Every key the server cannot act on, with its path, at any depth."""
    found = [str(k) for k in changes.model_extra or {}]
    for name, items in (
        ("newPositions", changes.newPositions),
        ("newEducation", changes.newEducation),
        ("newPublications", changes.newPublications),
        ("newCertifications", changes.newCertifications),
        ("newLanguages", changes.newLanguages),
        ("entryEdits", changes.entryEdits),
    ):
        for i, item in enumerate(items):
            found += [f"{name}[{i}].{k}" for k in item.model_extra or {}]
    for i, exp in enumerate(changes.experiences):
        found += [f"experiences[{i}].{k}" for k in exp.model_extra or {}]
        if exp.match is not None:
            found += [
                f"experiences[{i}].match.{k}" for k in exp.match.model_extra or {}
            ]
    if changes.skills is not None:
        found += [f"skills.{k}" for k in changes.skills.model_extra or {}]
    return sorted(found)


def _service(editor: Any | None) -> ProfileEditService:
    return ProfileEditService(
        editor,
        ProfileEditStore(settings.edits_root()),
        writes_enabled=settings.writes_enabled,
        pacing_seconds=settings.WRITE_PACING_SECONDS,
    )


async def _run(
    ctx: Context,
    tool: str,
    body: Callable[[ProfileEditService], Awaitable[dict[str, Any]]],
    *,
    needs_browser: bool = True,
) -> dict[str, Any]:
    try:
        editor = (
            await get_ready_profile_editor(ctx, tool_name=tool)
            if needs_browser
            else None
        )
        return await body(_service(editor))
    except ProfileEditError as e:
        return e.to_result()
    except AuthenticationError as e:
        try:
            await handle_auth_error(e, ctx)
        except Exception as relogin_exc:
            raise_tool_error(relogin_exc, tool)
    except (RateLimitError, AccountRestrictedError) as e:
        # A checkpoint or restriction is never worked around; the user clears it.
        return ProfileEditError(
            ProfileEditErrorCode.AUTHENTICATION_REQUIRED, detail=str(e)[:200]
        ).to_result()
    except Exception as e:
        raise_tool_error(e, tool)  # NoReturn


def register_profile_edit_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register own-profile reading and approval-gated editing tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Editable Profile",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "profile-edit"},
    )
    async def get_my_editable_profile(ctx: Context) -> dict[str, Any]:
        """
        Read your own profile as structured, editable fields. Read-only.

        Returns name, headline, location, about, experiences (each with a
        stable experienceId taken from LinkedIn's position id), skills with
        their order, and the character limits LinkedIn's forms enforce.
        Headline and about come from LinkedIn's edit forms, so they are exact.
        Use this before propose_profile_changes. For the raw text of profile
        sections use get_my_profile instead.
        """
        return await _run(ctx, "get_my_editable_profile", lambda s: s.get_profile())

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Experience",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "profile-edit"},
    )
    async def get_my_experience(
        ctx: Context, experienceId: str | None = None
    ) -> dict[str, Any]:
        """
        Read your experience entries. Read-only.

        Without experienceId: every position with its experienceId and display
        fields. With experienceId: that position's exact title and full
        description from its edit form, plus their character limits.

        Args:
            ctx: FastMCP context
            experienceId: Optional id from this tool or get_my_editable_profile
        """
        return await _run(
            ctx, "get_my_experience", lambda s: s.get_experiences(experienceId)
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get My Skills",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "profile-edit"},
    )
    async def get_my_skills(ctx: Context) -> dict[str, Any]:
        """Read your skills in LinkedIn's order. Read-only."""
        return await _run(ctx, "get_my_skills", lambda s: s.get_skills())

    @mcp.tool(
        timeout=tool_timeout,
        title="Describe Profile Form",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile-edit"},
    )
    async def describe_profile_form(ctx: Context, form: str) -> dict[str, Any]:
        """
        Diagnostics: open one of your profile's edit forms and list its controls. Read-only.

        Nothing is typed or saved; the form is left by navigating away. Use it
        to see why a write reports SELECTOR_NOT_FOUND or to update locators.
        Text field values are reported only as lengths.

        Args:
            ctx: FastMCP context
            form: intro | position | education | publication
        """

        async def body(s: ProfileEditService) -> dict[str, Any]:
            return await s.describe_form(form)

        return await _run(ctx, "describe_profile_form", body)

    @mcp.tool(
        timeout=tool_timeout,
        title="Propose Profile Changes",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile-edit"},
    )
    async def propose_profile_changes(
        ctx: Context, changes: ProfileChanges
    ) -> dict[str, Any]:
        """
        Creates a previewable profile change set. Does not modify LinkedIn.

        Reads the current values of the targeted fields, validates every
        requested value (including LinkedIn's character limits; over-long text
        is refused, never truncated), and stores a change set with before and
        after values and a fingerprint of the profile it was planned against.
        Show the returned diff to the user. Nothing is applied until the user
        approves and apply_profile_changes is called with confirm=true.

        Only send values the user has written or approved. This tool carries
        content; it must not be used to invent jobs, employers, dates,
        qualifications, skills or achievements. Supported: first and last
        name, headline, about, title and description of an existing
        experience, adding and removing skills, adding new positions,
        education, publications, certifications and languages, editing
        text fields of existing education and publication entries
        (entryEdits), and linking an existing position or education entry to
        an organization's LinkedIn page (organizations). Anything else returns UNSUPPORTED_FIELD.
        A new entry that resembles an existing one is refused unless
        allowDuplicate is set. Use rehearse_profile_changes to fill the forms
        without saving before applying.

        Args:
            ctx: FastMCP context
            changes: firstName, lastName, headline, about, experiences
                [{experienceId | match, title, description}], skills {add,
                remove}, newPositions, newEducation, newPublications
        """
        extra = _unsupported_fields(changes)
        if extra:
            return ProfileEditError(
                ProfileEditErrorCode.UNSUPPORTED_FIELD,
                "These fields cannot be edited by this server: "
                + ", ".join(extra)
                + ".",
                fields=extra,
            ).to_result()
        proposal = Proposal(
            headline=changes.headline,
            about=changes.about,
            experiences=[
                ExperienceEdit(
                    experience_id=e.experienceId,
                    company=e.match.company if e.match else None,
                    match_title=e.match.title if e.match else None,
                    start_date=e.match.startDate if e.match else None,
                    title=e.title,
                    description=e.description,
                )
                for e in changes.experiences
            ],
            skills_add=changes.skills.add if changes.skills else [],
            skills_remove=changes.skills.remove if changes.skills else [],
            first_name=changes.firstName,
            last_name=changes.lastName,
            new_entries=_entry_requests(changes),
            organizations=[
                OrganizationEdit(o.kind, o.entryId, o.name)
                for o in changes.organizations
            ],
            entry_edits=[
                EntryFieldEdit(e.kind, e.entryId, e.field, e.value)
                for e in changes.entryEdits
            ],
        )
        return await _run(ctx, "propose_profile_changes", lambda s: s.propose(proposal))

    @mcp.tool(
        timeout=tool_timeout,
        title="Preview Profile Changes",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile-edit"},
    )
    async def preview_profile_changes(ctx: Context, changeSetId: str) -> dict[str, Any]:
        """
        Show a change set's exact before/after values. Does not modify LinkedIn.

        Re-reads the targeted fields: if anything changed since the proposal
        (for example an edit made by hand on linkedin.com), returns
        STALE_CHANGE_SET with the changed fields and the change set can no
        longer be applied; propose again.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
        """
        return await _run(
            ctx, "preview_profile_changes", lambda s: s.preview(changeSetId)
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Rehearse Profile Changes",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"profile-edit"},
    )
    async def rehearse_profile_changes(
        ctx: Context, changeSetId: str
    ) -> dict[str, Any]:
        """
        Fill LinkedIn's forms with a pending change set and leave without saving. Does not modify LinkedIn.

        Proves each form accepts the approved values (fields found, limits,
        dropdown options, suggestions) before anything is saved. The change
        set stays pending. Skill changes are not rehearsed.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
        """
        return await _run(
            ctx, "rehearse_profile_changes", lambda s: s.rehearse(changeSetId)
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Apply Profile Changes",
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
        tags={"profile-edit", "actions"},
    )
    async def apply_profile_changes(
        ctx: Context, changeSetId: str, confirm: bool = False
    ) -> dict[str, Any]:
        """
        Applies a previously created and explicitly approved change set to the authenticated user's own LinkedIn profile. This modifies external state.

        Call only after the user has seen the preview and said to apply it.
        Requires confirm=true and a server started with
        MCP_LINKEDIN_WRITE_ENABLED=true. Applies exactly the stored changes,
        one field at a time, re-reading each from LinkedIn to verify it; a
        field counts as done only when the saved value is observed. Refuses a
        change set that is not pending (each is applied at most once) or whose
        profile changed since it was proposed (STALE_CHANGE_SET). Stops at the
        first failure and reports PARTIAL_FAILURE with per-field results; it
        never retries a failed write and never rolls back automatically.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
            confirm: must be true; the user's explicit approval
        """
        try:
            _service(None).precheck_apply(changeSetId, confirm=confirm)
        except ProfileEditError as e:
            return e.to_result()
        return await _run(
            ctx,
            "apply_profile_changes",
            lambda s: s.apply(changeSetId, confirm=confirm),
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Discard Profile Changes",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": False,
        },
        tags={"profile-edit"},
    )
    async def discard_profile_changes(ctx: Context, changeSetId: str) -> dict[str, Any]:
        """
        Discard a pending change set so it can never be applied. Does not modify LinkedIn.

        Args:
            ctx: FastMCP context
            changeSetId: id returned by propose_profile_changes
        """

        async def body(s: ProfileEditService) -> dict[str, Any]:
            return s.discard(changeSetId)

        return await _run(ctx, "discard_profile_changes", body, needs_browser=False)
