"""Local evidence notes for scraper failures."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from linkedin_mcp_server.common_utils import (
    secure_mkdir,
    secure_write_text,
    slugify_fragment,
    utcnow_iso,
)
from linkedin_mcp_server.debug_trace import get_trace_dir, mark_trace_for_retention
from linkedin_mcp_server.session_state import (
    auth_root_dir,
    get_runtime_id,
    get_source_profile_dir,
    load_runtime_state,
    load_source_state,
    portable_cookie_path,
    runtime_profile_dir,
    runtime_storage_state_path,
)

ISSUE_URL = "https://github.com/stickerdaniel/linkedin-mcp-server/issues/new/choose"
PACKET_SKILL_URL = (
    "https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/"
    ".agents/skills/issue-packet/SKILL.md"
)
ISSUE_TITLE_PREFIX = "[BUG]"
ISSUE_SEARCH_API = "https://api.github.com/search/issues"
PACKET_GUIDANCE = (
    "Packet: use these local notes as evidence. Follow the packet skill "
    f"({PACKET_SKILL_URL}) or the current issue form ({ISSUE_URL}) to prepare "
    "a redacted report. Search open and closed issues, then show the final "
    "issue or comment to the human and ask for an explicit yes before posting."
)


def build_issue_diagnostics(
    exception: Exception,
    *,
    context: str,
    target_url: str | None = None,
    section_name: str | None = None,
) -> dict[str, Any]:
    """Write local evidence notes and return MCP-safe diagnostic locations."""
    timestamp = utcnow_iso()
    source_profile_dir = _safe_source_profile_dir()
    current_runtime_id = get_runtime_id()
    source_state = load_source_state(source_profile_dir)
    runtime_state = load_runtime_state(current_runtime_id, source_profile_dir)
    trace_dir = mark_trace_for_retention() or get_trace_dir()
    log_path = trace_dir / "server.log" if trace_dir else None
    issue_dir = trace_dir or (auth_root_dir(source_profile_dir) / "issue-reports")
    secure_mkdir(issue_dir)
    issue_path = (
        issue_dir
        / f"{timestamp.replace(':', '').replace('-', '')}-{slugify_fragment(context) or 'issue'}.md"
    )

    runtime_details = {
        "hostname": socket.gethostname(),
        "current_runtime_id": current_runtime_id,
        "source_profile_dir": str(source_profile_dir),
        "portable_cookie_path": str(portable_cookie_path(source_profile_dir)),
        "source_state": asdict(source_state) if source_state else None,
        "runtime_profile_dir": str(
            runtime_profile_dir(current_runtime_id, source_profile_dir)
        ),
        "runtime_storage_state_path": str(
            runtime_storage_state_path(current_runtime_id, source_profile_dir)
        ),
        "runtime_state": asdict(runtime_state) if runtime_state else None,
        "trace_dir": str(trace_dir) if trace_dir else None,
        "log_path": str(log_path) if log_path and log_path.exists() else None,
        "suggested_gist_command": None,
    }
    payload: dict[str, Any] = {
        "created_at": timestamp,
        "context": context,
        "section_name": section_name,
        "target_url": target_url,
        "error_type": type(exception).__name__,
        "error_message": str(exception),
        "runtime": runtime_details,
        "suggested_issue_title": _suggest_issue_title(
            context=context,
            section_name=section_name,
            target_url=target_url,
            current_runtime_id=current_runtime_id,
        ),
    }
    payload["issue_search_skipped"] = _inside_running_event_loop()
    if payload["issue_search_skipped"]:
        payload["existing_issues"] = []
    else:
        payload["existing_issues"] = _find_existing_issues(payload)
    issue_template = _render_issue_template(payload)
    secure_write_text(issue_path, issue_template)
    return _public_issue_diagnostics(payload, issue_path=issue_path)


def format_tool_error_with_diagnostics(
    message: str, diagnostics: dict[str, Any]
) -> str:
    """Append local note locations and packet guidance to a tool-facing error."""
    lines = [message, "", "Diagnostics:"]
    if diagnostics.get("issue_template_path"):
        lines.append(f"- Local diagnostic notes: {diagnostics['issue_template_path']}")
    runtime = diagnostics.get("runtime") or {}
    if runtime.get("trace_dir"):
        lines.append(f"- Local trace artifacts: {runtime['trace_dir']}")
    if runtime.get("log_path"):
        lines.append(f"- Local server log: {runtime['log_path']}")
    lines.append(f"- Runtime: {runtime.get('current_runtime_id', 'unknown')}")
    lines.append(PACKET_GUIDANCE)
    existing_issues = diagnostics.get("existing_issues") or []
    if existing_issues:
        lines.append("- Candidate open issues to review:")
        for issue in existing_issues:
            lines.append(f"  - #{issue['number']}: {issue['title']} ({issue['url']})")
        lines.append(
            "- If one matches this failure, prepare a redacted comment with new evidence and ask for an explicit yes before posting."
        )
    elif diagnostics.get("issue_search_skipped"):
        lines.append(
            "- Advisory open-issue search was skipped in async server context. Packet search of open and closed issues is still required."
        )
    else:
        lines.append(
            "- Diagnostics did not establish a canonical match. Packet search of open and closed issues is still required."
        )
    return "\n".join(lines)


def _render_issue_template(payload: dict[str, Any]) -> str:
    # Local notes cannot be a completed packet: the reporter supplies the
    # actual call and account context. Path keys here are local troubleshooting
    # aids. They are not permission to copy their values into an issue or gist.
    runtime = payload["runtime"]
    existing_issues = payload.get("existing_issues") or []
    has_existing_issues = bool(existing_issues)
    issue_search_skipped = bool(payload.get("issue_search_skipped"))
    if has_existing_issues:
        advisory_lines = [
            "- Candidate open issues to review:",
            *[
                f"  - #{issue['number']}: {issue['title']} ({issue['url']})"
                for issue in existing_issues
            ],
        ]
    elif issue_search_skipped:
        advisory_lines = [
            "- Advisory open-issue search was skipped in async server context. Packet search of open and closed issues is still required."
        ]
    else:
        advisory_lines = [
            "- Diagnostics did not establish a canonical match. Packet search of open and closed issues is still required."
        ]
    return (
        "\n".join(
            [
                "# Local diagnostic notes",
                "",
                "These notes are local troubleshooting aids. They are not a completed packet.",
                PACKET_GUIDANCE,
                "",
                "## Captured failure",
                f"- Context: {payload['context']}",
                f"- Section: {payload.get('section_name') or 'n/a'}",
                f"- Target URL: {payload.get('target_url') or 'n/a'}",
                f"- Error: {payload['error_type']}: {payload['error_message']}",
                f"- Suggested title (local only): {payload['suggested_issue_title']}",
                "",
                "## Advisory open issues",
                *advisory_lines,
                "",
                "## Local runtime",
                f"- Hostname (local): {runtime['hostname']}",
                f"- Current runtime: {runtime['current_runtime_id']}",
                f"- Source profile (local): {runtime['source_profile_dir']}",
                f"- Portable cookies (local): {runtime['portable_cookie_path']}",
                f"- Derived runtime profile (local): {runtime['runtime_profile_dir']}",
                f"- Derived storage-state (local): {runtime['runtime_storage_state_path']}",
                f"- Trace artifacts (local): {runtime['trace_dir'] or 'not enabled'}",
                f"- Server log (local): {runtime['log_path'] or 'not enabled'}",
                "",
                "## Session state (local)",
                "```json",
                json.dumps(
                    {
                        "source_state": runtime["source_state"],
                        "runtime_state": runtime["runtime_state"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                "```",
            ]
        )
        + "\n"
    )


def _public_issue_diagnostics(
    payload: dict[str, Any], *, issue_path: Path
) -> dict[str, Any]:
    runtime = payload["runtime"]
    return {
        "created_at": payload["created_at"],
        "context": payload["context"],
        "section_name": payload["section_name"],
        "target_url": payload["target_url"],
        "error_type": payload["error_type"],
        "error_message": payload["error_message"],
        "suggested_issue_title": payload["suggested_issue_title"],
        "existing_issues": payload["existing_issues"],
        "issue_search_skipped": payload["issue_search_skipped"],
        "issue_template_path": str(issue_path),
        "runtime": {
            "current_runtime_id": runtime["current_runtime_id"],
            "trace_dir": runtime["trace_dir"],
            "log_path": runtime["log_path"],
            "suggested_gist_command": None,
        },
    }


def _safe_source_profile_dir():
    try:
        return get_source_profile_dir()
    except Exception:
        return (Path.home() / ".linkedin-mcp" / "profile").expanduser()


def _suggest_issue_title(
    *,
    context: str,
    section_name: str | None,
    target_url: str | None,
    current_runtime_id: str,
) -> str:
    section = section_name or "unknown-section"
    route = target_url or context
    if "/recent-activity/" in route:
        summary = f"recent-activity redirect loop in {section} on {current_runtime_id}"
    else:
        summary = f"{section} scrape failure in {context} on {current_runtime_id}"
    return f"{ISSUE_TITLE_PREFIX} {summary}"


def _find_existing_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    query = _issue_search_query(payload)
    if not query:
        return []

    request = Request(
        f"{ISSUE_SEARCH_API}?q={quote_plus(query)}&per_page=3",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "linkedin-mcp-server-diagnostics",
        },
    )
    try:
        with urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []

    issues: list[dict[str, Any]] = []
    for item in data.get("items", []):
        issues.append(
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "url": item.get("html_url"),
            }
        )
    return issues


def _inside_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _issue_search_query(payload: dict[str, Any]) -> str:
    route = payload.get("target_url") or payload.get("context") or ""
    if "/recent-activity/" in route:
        summary = '"recent-activity redirect loop"'
    else:
        section = payload.get("section_name") or "scrape"
        summary = f'"{section}"'
    return f"repo:stickerdaniel/linkedin-mcp-server is:issue is:open {summary}"
