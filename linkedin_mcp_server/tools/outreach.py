"""Draft-first LinkedIn outreach workflow tools."""

from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from linkedin_mcp_server.callbacks import MCPContextProgressCallback
from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error


class OutreachBrief(BaseModel):
    lead_id: str
    brief: str
    source_urls: list[str] = Field(default_factory=list)
    person: dict[str, Any] = Field(default_factory=dict)
    company: dict[str, Any] = Field(default_factory=dict)
    outreach_notes: list[str] = Field(default_factory=list)


class OutreachDraft(BaseModel):
    draft: str
    connection_note: str
    max_chars: int
    requires_human_approval: bool = True
    send_status: str = "draft_only"


class FollowUpPlan(BaseModel):
    schedule: list[str]
    drafts: list[str]
    requires_human_approval: bool = True
    send_status: str = "draft_only"


class OutreachReview(BaseModel):
    readiness: str
    flags: list[str]
    suggestions: list[str]
    requires_human_approval: bool = True


def _compact_text(text: str, limit: int = 1200) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def _section_excerpt(payload: dict[str, Any], section: str, limit: int = 1200) -> str:
    sections = payload.get("sections", {})
    if not isinstance(sections, dict):
        return ""
    text = sections.get(section, "")
    return _compact_text(text, limit) if isinstance(text, str) else ""


def _lead_name(lead_brief: str) -> str:
    first_line = next(
        (line.strip() for line in lead_brief.splitlines() if line.strip()), ""
    )
    if ":" in first_line:
        first_line = first_line.split(":", 1)[1].strip()
    words = first_line.split()
    if not words:
        return "there"
    return words[0].strip(",")


def _trim_to_chars(text: str, max_chars: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "..."


def register_outreach_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register human-approved outreach planning tools."""

    @mcp.tool(
        timeout=tool_timeout,
        title="Research Lead",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"outreach", "sales"},
        output_schema=OutreachBrief.model_json_schema(),
        exclude_args=["extractor"],
    )
    async def research_lead(
        linkedin_username: str,
        ctx: Context,
        company_name: str | None = None,
        product_value_proposition: str | None = None,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """Collect person and optional company context into an outreach brief."""
        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="research_lead"
            )
            cb = MCPContextProgressCallback(ctx)
            person = await extractor.scrape_person(
                linkedin_username,
                {"main_profile", "experience", "education", "skills"},
                callbacks=cb,
            )
            company: dict[str, Any] = {}
            if company_name:
                company = await extractor.scrape_company(
                    company_name,
                    {"about"},
                    callbacks=cb,
                )

            person_summary = _section_excerpt(person, "main_profile")
            experience = _section_excerpt(person, "experience", limit=900)
            skills = _section_excerpt(person, "skills", limit=700)
            company_summary = _section_excerpt(company, "about", limit=900)

            notes = [
                "Use one specific observation from the profile before pitching.",
                "Keep the ask small and easy to decline.",
                "Do not imply a relationship or knowledge that is not in the sourced text.",
            ]
            if product_value_proposition:
                notes.append(
                    "Tie the value proposition to a concrete role, company, or skill signal."
                )

            brief_parts = [
                f"Lead: {linkedin_username}",
                f"Profile: {person_summary}",
            ]
            if experience:
                brief_parts.append(f"Experience: {experience}")
            if skills:
                brief_parts.append(f"Skills: {skills}")
            if company_summary:
                brief_parts.append(f"Company: {company_summary}")
            if product_value_proposition:
                brief_parts.append(
                    f"Offer: {_compact_text(product_value_proposition, 500)}"
                )

            return {
                "lead_id": f"person:{linkedin_username}",
                "brief": "\n".join(brief_parts),
                "source_urls": [
                    url
                    for url in (person.get("url"), company.get("url"))
                    if isinstance(url, str) and url
                ],
                "person": person,
                "company": company,
                "outreach_notes": notes,
            }
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "research_lead")
        except Exception as e:
            raise_tool_error(e, "research_lead")

    @mcp.tool(
        timeout=tool_timeout,
        title="Draft Outreach Message",
        annotations={"readOnlyHint": True},
        tags={"outreach", "sales"},
        output_schema=OutreachDraft.model_json_schema(),
    )
    async def draft_outreach_message(
        lead_brief: str,
        product_value_proposition: str,
        call_to_action: str,
        tone: str = "warm",
        max_chars: int = 600,
    ) -> dict[str, Any]:
        """Draft a LinkedIn outreach message without sending it."""
        name = _lead_name(lead_brief)
        tone_prefix = {
            "direct": "I noticed",
            "warm": "I saw",
            "casual": "I came across",
        }.get(tone.lower(), "I saw")
        signal = _compact_text(lead_brief, 220)
        value = _compact_text(product_value_proposition, 180)
        cta = _compact_text(call_to_action, 120)
        draft = (
            f"Hi {name}, {tone_prefix} {signal}. "
            f"It made me think this could be relevant: {value}. "
            f"{cta}"
        )
        connection_note = (
            f"Hi {name}, {tone_prefix.lower()} your LinkedIn profile and thought "
            f"there may be a relevant reason to connect. {cta}"
        )
        return {
            "draft": _trim_to_chars(draft, max_chars),
            "connection_note": _trim_to_chars(connection_note, min(max_chars, 300)),
            "max_chars": max_chars,
            "requires_human_approval": True,
            "send_status": "draft_only",
        }

    @mcp.tool(
        timeout=tool_timeout,
        title="Plan Follow Up",
        annotations={"readOnlyHint": True},
        tags={"outreach", "sales"},
        output_schema=FollowUpPlan.model_json_schema(),
    )
    async def plan_follow_up(
        lead_brief: str,
        previous_message: str,
        call_to_action: str,
        cadence_days: list[int] | None = None,
    ) -> dict[str, Any]:
        """Plan draft-only LinkedIn follow-ups."""
        days = cadence_days or [3, 7, 14]
        name = _lead_name(lead_brief)
        cta = _compact_text(call_to_action, 140)
        prior = _compact_text(previous_message, 180)
        drafts = [
            f"Hi {name}, quick follow-up on my note about {prior}. {cta}",
            f"Hi {name}, closing the loop in case the timing was off. {cta}",
            f"Hi {name}, last note from me here. If this is not relevant, no worries. {cta}",
        ]
        return {
            "schedule": [f"Day {day}" for day in days],
            "drafts": drafts[: len(days)],
            "requires_human_approval": True,
            "send_status": "draft_only",
        }

    @mcp.tool(
        timeout=tool_timeout,
        title="Review Outreach Target",
        annotations={"readOnlyHint": True},
        tags={"outreach", "sales"},
        output_schema=OutreachReview.model_json_schema(),
    )
    async def review_outreach_target(
        lead_brief: str,
        message: str,
    ) -> dict[str, Any]:
        """Review a lead/message pair for fit, specificity, and outreach risk."""
        flags: list[str] = []
        suggestions: list[str] = []
        brief_lower = lead_brief.lower()
        message_lower = message.lower()

        if len(message.strip()) < 40:
            flags.append("message_too_short")
            suggestions.append("Add one sourced reason for reaching out.")
        if not any(word in brief_lower for word in ("experience", "skills", "company")):
            flags.append("thin_lead_context")
            suggestions.append("Research the profile or company before drafting.")
        if not any(word in message_lower for word in ("noticed", "saw", "came across")):
            flags.append("missing_personalization_signal")
            suggestions.append("Reference a profile, company, role, or skill signal.")
        if any(word in message_lower for word in ("guaranteed", "urgent", "act now")):
            flags.append("spam_or_pressure_language")
            suggestions.append("Remove exaggerated claims or pressure language.")

        readiness = "ready_for_human_review" if not flags else "needs_revision"
        if not suggestions:
            suggestions.append("Have a human approve before sending.")
        return {
            "readiness": readiness,
            "flags": flags,
            "suggestions": suggestions,
            "requires_human_approval": True,
        }
