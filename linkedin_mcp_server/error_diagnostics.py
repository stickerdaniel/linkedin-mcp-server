"""Issue-ready diagnostics for scraper failures."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import socket
from pathlib import Path
import re
from typing import Any

from linkedin_mcp_server.debug_trace import get_trace_dir
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


def build_issue_diagnostics(
    exception: Exception,
    *,
    context: str,
    target_url: str | None = None,
    section_name: str | None = None,
) -> dict[str, Any]:
    """Write an issue-ready report and return structured diagnostics."""
    timestamp = _utcnow()
    source_profile_dir = _safe_source_profile_dir()
    current_runtime_id = get_runtime_id()
    source_state = load_source_state(source_profile_dir)
    runtime_state = load_runtime_state(current_runtime_id, source_profile_dir)
    trace_dir = get_trace_dir()
    log_path = trace_dir / "server.log" if trace_dir else None
    issue_dir = trace_dir or (auth_root_dir(source_profile_dir) / "issue-reports")
    issue_dir.mkdir(parents=True, exist_ok=True)
    issue_path = (
        issue_dir
        / f"{timestamp.replace(':', '').replace('-', '')}-{_slugify(context)}.md"
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
    }
    payload = {
        "created_at": timestamp,
        "context": context,
        "section_name": section_name,
        "target_url": target_url,
        "error_type": type(exception).__name__,
        "error_message": str(exception),
        "runtime": runtime_details,
    }
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
    lines.append(
        f"- Runtime: {runtime.get('current_runtime_id', 'unknown')} on {runtime.get('hostname', 'unknown')}"
    )
    lines.append(f"- File the issue here: {ISSUE_URL}")
    lines.append(
        "- Read the generated issue template and attach the listed files to the GitHub issue."
    )
    return "\n".join(lines)


def _render_issue_template(payload: dict[str, Any]) -> str:
    runtime = payload["runtime"]
    return (
        "\n".join(
            [
                "# LinkedIn MCP scrape failure",
                "",
                "## File This Issue",
                f"- GitHub issue link: {ISSUE_URL}",
                "- Read this generated file before posting.",
                "- Copy the Summary and Runtime sections into the GitHub issue.",
                "- Attach this generated markdown file, the server log, and the trace artifacts directory.",
                "",
                "## Summary",
                f"- Context: {payload['context']}",
                f"- Section: {payload.get('section_name') or 'n/a'}",
                f"- Target URL: {payload.get('target_url') or 'n/a'}",
                f"- Error: {payload['error_type']}: {payload['error_message']}",
                "",
                "## Runtime",
                f"- Hostname: {runtime['hostname']}",
                f"- Current runtime: {runtime['current_runtime_id']}",
                f"- Source profile: {runtime['source_profile_dir']}",
                f"- Portable cookies: {runtime['portable_cookie_path']}",
                f"- Derived runtime profile: {runtime['runtime_profile_dir']}",
                f"- Derived storage-state: {runtime['runtime_storage_state_path']}",
                f"- Trace artifacts: {runtime['trace_dir'] or 'not enabled'}",
                f"- Server log: {runtime['log_path'] or 'not enabled'}",
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
                "",
                "## Reproduction",
                "1. Run a fresh local `uv run -m linkedin_mcp_server --login`.",
                "2. Start the local Docker server with the same debug env vars used for this run.",
                "3. Re-run the failing MCP tool call.",
            ]
        )
        + "\n"
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "issue"


def _safe_source_profile_dir():
    try:
        return get_source_profile_dir()
    except BaseException:
        return (Path.home() / ".linkedin-mcp" / "profile").expanduser()


def _utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
