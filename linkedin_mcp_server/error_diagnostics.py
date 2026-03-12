"""Issue-ready diagnostics for scraper failures."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from linkedin_mcp_server.common_utils import slugify_fragment, utcnow_iso
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
ISSUE_TITLE_PREFIX = "[BUG]"
ISSUE_SEARCH_API = "https://api.github.com/search/issues"


def build_issue_diagnostics(
    exception: Exception,
    *,
    context: str,
    target_url: str | None = None,
    section_name: str | None = None,
) -> dict[str, Any]:
    """Write an issue-ready report and return structured diagnostics."""
    timestamp = utcnow_iso()
    source_profile_dir = _safe_source_profile_dir()
    current_runtime_id = get_runtime_id()
    source_state = load_source_state(source_profile_dir)
    runtime_state = load_runtime_state(current_runtime_id, source_profile_dir)
    trace_dir = mark_trace_for_retention() or get_trace_dir()
    log_path = trace_dir / "server.log" if trace_dir else None
    issue_dir = trace_dir or (auth_root_dir(source_profile_dir) / "issue-reports")
    issue_dir.mkdir(parents=True, exist_ok=True)
    issue_path = (
        issue_dir
        / f"{timestamp.replace(':', '').replace('-', '')}-{slugify_fragment(context) or 'issue'}.md"
    )
    gist_command = _build_gist_command(issue_dir, issue_path, log_path)

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
        "suggested_gist_command": gist_command,
    }
    payload = {
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
    payload["existing_issues"] = _find_existing_issues(payload)
    issue_template = _render_issue_template(payload)
    issue_path.write_text(issue_template)
    payload["issue_template_path"] = str(issue_path)
    payload["issue_template"] = issue_template
    return payload


def format_tool_error_with_diagnostics(
    message: str, diagnostics: dict[str, Any]
) -> str:
    """Append issue-report locations to a tool-facing error message."""
    lines = [message, "", "Diagnostics:"]
    if diagnostics.get("issue_template_path"):
        lines.append(f"- Issue template: {diagnostics['issue_template_path']}")
    runtime = diagnostics.get("runtime") or {}
    if runtime.get("trace_dir"):
        lines.append(f"- Trace artifacts: {runtime['trace_dir']}")
    if runtime.get("log_path"):
        lines.append(f"- Server log: {runtime['log_path']}")
    if runtime.get("suggested_gist_command"):
        lines.append(f"- Suggested gist command: {runtime['suggested_gist_command']}")
    lines.append(
        f"- Runtime: {runtime.get('current_runtime_id', 'unknown')} on {runtime.get('hostname', 'unknown')}"
    )
    existing_issues = diagnostics.get("existing_issues") or []
    if existing_issues:
        lines.append("- Matching open issues were found. Review them first:")
        for issue in existing_issues:
            lines.append(f"  - #{issue['number']}: {issue['title']} ({issue['url']})")
        lines.append(
            "- If one matches this failure, upload the gist and post it as a comment on that issue instead of opening a new issue."
        )
    else:
        lines.append(f"- File the issue here: {ISSUE_URL}")
    lines.append(
        "- Read the generated issue template and attach the listed files before posting."
    )
    return "\n".join(lines)


def _render_issue_template(payload: dict[str, Any]) -> str:
    runtime = payload["runtime"]
    existing_issues = payload.get("existing_issues") or []
    has_existing_issues = bool(existing_issues)
    installation_lines = _installation_method_lines(runtime)
    tool_lines = _tool_lines(payload)
    return (
        "\n".join(
            [
                "# LinkedIn MCP scrape failure",
                "",
                "## File This Issue",
                f"- Suggested title: {payload['suggested_issue_title']}",
                "- Read this generated file before posting.",
                "- Copy the sections below into the GitHub bug report template.",
                "- Attach this generated markdown file, the server log, and the trace artifacts directory.",
                (
                    "- Review the existing open issues below first. If one matches, post the gist as a comment there instead of opening a new issue."
                    if has_existing_issues
                    else f"- GitHub issue link: {ISSUE_URL}"
                ),
                "",
                "## Existing Open Issues",
                *(
                    [
                        f"- #{issue['number']}: {issue['title']} ({issue['url']})"
                        for issue in existing_issues
                    ]
                    if has_existing_issues
                    else ["- No matching open issues found during diagnostics."]
                ),
                "",
                "## Installation Method",
                *installation_lines,
                "",
                "## When does the error occur?",
                "- [ ] At startup",
                "- [x] During tool call (specify which tool):",
                *tool_lines,
                "",
                "## MCP Client Configuration",
                "",
                "**Client used for reproduction**:",
                "```text",
                "Local curl-based MCP HTTP client against the server's streamable-http transport",
                "```",
                "",
                "## MCP Client Logs",
                "```text",
                "See attached server log and trace artifacts.",
                "```",
                "",
                "## Error Description",
                f"Context: {payload['context']}",
                f"Section: {payload.get('section_name') or 'n/a'}",
                f"Target URL: {payload.get('target_url') or 'n/a'}",
                f"Error: {payload['error_type']}: {payload['error_message']}",
                "",
                "## Runtime Diagnostics",
                f"- Hostname: {runtime['hostname']}",
                f"- Current runtime: {runtime['current_runtime_id']}",
                f"- Source profile: {runtime['source_profile_dir']}",
                f"- Portable cookies: {runtime['portable_cookie_path']}",
                f"- Derived runtime profile: {runtime['runtime_profile_dir']}",
                f"- Derived storage-state: {runtime['runtime_storage_state_path']}",
                f"- Trace artifacts: {runtime['trace_dir'] or 'not enabled'}",
                f"- Server log: {runtime['log_path'] or 'not enabled'}",
                f"- Suggested gist command: {runtime['suggested_gist_command'] or 'not available'}",
                "",
                "## Session State",
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
                "",
                "## Attachment Checklist",
                "- Read this generated markdown file and use it as the issue body/context.",
                "- Attach this generated markdown file itself.",
                "- Attach the server log if available.",
                "- Attach the trace screenshots/trace.jsonl if available.",
                "- Optional: run the suggested gist command below to upload the text artifacts as a single shareable bundle.",
                "",
                "## Suggested Gist Command",
                "```bash",
                runtime["suggested_gist_command"] or "# gist command unavailable",
                "```",
                "",
                "## Reproduction",
                "1. Run a fresh local `uv run -m linkedin_mcp_server --login`.",
                "2. Start the server again using the same installation method and debug env vars used for this run.",
                "3. Re-run the failing MCP tool call.",
                (
                    "4. If one of the listed open issues matches, post the gist as a comment there as additional information."
                    if has_existing_issues
                    else "4. If no existing issue matches, open a new GitHub bug report with the information above."
                ),
            ]
        )
        + "\n"
    )


def _safe_source_profile_dir():
    try:
        return get_source_profile_dir()
    except BaseException:
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


def _build_gist_command(
    issue_dir: Path,
    issue_path: Path,
    log_path: Path | None,
) -> str:
    trace_path = issue_dir / "trace.jsonl"
    files = [str(issue_path)]
    if log_path is not None:
        files.append(str(log_path))
    if trace_path.exists():
        files.append(str(trace_path))
    quoted = " ".join(f'"{path}"' for path in files)
    return f'gh gist create {quoted} -d "LinkedIn MCP debug artifacts"'


def _find_existing_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if _inside_running_event_loop():
        return []

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


def _installation_method_lines(runtime: dict[str, Any]) -> list[str]:
    current_runtime_id = str(runtime.get("current_runtime_id") or "")
    docker_checked = "x" if "container" in current_runtime_id else " "
    return [
        f"- [{docker_checked}] Docker (specify docker image version/tag): `stickerdaniel/linkedin-mcp-server:latest` with `~/.linkedin-mcp` mounted into `/home/pwuser/.linkedin-mcp`",
        "- [ ] Claude Desktop DXT extension (specify docker image version/tag): _._._",
        "- [ ] Local Python setup",
    ]


def _tool_lines(payload: dict[str, Any]) -> list[str]:
    selected_tool = _tool_name_for_context(payload)
    tool_names = [
        "get_person_profile",
        "get_company_profile",
        "get_company_posts",
        "get_job_details",
        "search_jobs",
        "search_people",
        "close_session",
    ]
    return [
        f"  - [{'x' if tool_name == selected_tool else ' '}] {tool_name}"
        for tool_name in tool_names
    ]


def _tool_name_for_context(payload: dict[str, Any]) -> str | None:
    context = str(payload.get("context") or "")
    if context in {
        "get_person_profile",
        "get_company_profile",
        "get_company_posts",
        "get_job_details",
        "search_jobs",
        "search_people",
        "close_session",
    }:
        return context

    if context in {"extract_page", "extract_overlay", "scrape_person"}:
        return "get_person_profile"
    if context == "scrape_company":
        return "get_company_profile"
    if context == "extract_search_page":
        target_url = str(payload.get("target_url") or "")
        if "/search/results/people" in target_url:
            return "search_people"
        if "/jobs/search" in target_url:
            return "search_jobs"

    return None


def _issue_search_query(payload: dict[str, Any]) -> str:
    route = payload.get("target_url") or payload.get("context") or ""
    if "/recent-activity/" in route:
        summary = '"recent-activity redirect loop"'
    else:
        section = payload.get("section_name") or "scrape"
        summary = f'"{section}"'
    return f"repo:stickerdaniel/linkedin-mcp-server is:issue is:open {summary}"
