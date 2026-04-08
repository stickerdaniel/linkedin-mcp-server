from pathlib import Path

import pytest

from linkedin_mcp_server.error_diagnostics import (
    _installation_method_lines,
    _installation_method_summary,
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)


def _required_issue_form_labels() -> list[str]:
    labels: list[str] = []
    current_label: str | None = None
    in_body = False
    issue_form_path = (
        Path(__file__).resolve().parents[1] / ".github/ISSUE_TEMPLATE/bug_report.yml"
    )
    lines = issue_form_path.read_text().splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped == "body:":
            in_body = True
            continue
        if not in_body:
            continue
        if stripped.startswith("- type:"):
            current_label = None
            continue
        if stripped.startswith("label:"):
            current_label = stripped.removeprefix("label:").strip().strip('"')
            continue
        if stripped == "required: true" and current_label:
            labels.append(current_label)
    return labels


def test_build_issue_diagnostics_includes_existing_issues(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [
            {
                "number": 220,
                "title": "[BUG] recent-activity redirect loop in posts on linux-arm64-container",
                "url": "https://github.com/stickerdaniel/linkedin-mcp-server/issues/220",
            }
        ],
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/williamhgates/recent-activity/all/",
        section_name="posts",
    )

    assert diagnostics["existing_issues"][0]["number"] == 220
    assert diagnostics["issue_search_skipped"] is False
    assert diagnostics["section_name"] == "posts"
    assert diagnostics["runtime"]["trace_dir"] is not None
    assert "issue_template" not in diagnostics
    assert "hostname" not in diagnostics["runtime"]
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "## Existing Open Issues" in issue_body
    assert "#220" in issue_body
    assert "post the gist as a comment there" in issue_body
    assert "## Setup" in issue_body
    assert "## What Happened" in issue_body
    assert "## Steps to Reproduce" in issue_body
    assert "## Logs" in issue_body


def test_format_tool_error_with_diagnostics_prefers_existing_issue_comment_flow():
    diagnostics = {
        "issue_template_path": "/tmp/issue.md",
        "existing_issues": [
            {
                "number": 220,
                "title": "[BUG] recent-activity redirect loop in posts on linux-arm64-container",
                "url": "https://github.com/stickerdaniel/linkedin-mcp-server/issues/220",
            }
        ],
        "runtime": {
            "trace_dir": "/tmp/trace",
            "log_path": "/tmp/trace/server.log",
            "suggested_gist_command": 'gh gist create "/tmp/issue.md"',
            "current_runtime_id": "linux-arm64-container",
            "hostname": "test-host",
        },
    }

    message = format_tool_error_with_diagnostics("Scrape failed", diagnostics)

    assert "Matching open issues were found" in message
    assert "#220" in message
    assert "post it as a comment" in message
    assert "File the issue here" not in message
    assert "- Runtime: linux-arm64-container" in message
    assert "test-host" not in message


def test_find_existing_issues_query_failure_is_tolerated(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no network")),
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/test/",
        section_name="main_profile",
    )

    assert diagnostics["existing_issues"] == []
    assert diagnostics["issue_search_skipped"] is False


def test_build_issue_diagnostics_omits_missing_server_log_from_gist(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [],
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/test/",
        section_name="main_profile",
    )

    gist_command = diagnostics["runtime"]["suggested_gist_command"]
    assert "server.log" not in gist_command


@pytest.mark.asyncio
async def test_build_issue_diagnostics_skips_network_search_in_event_loop(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    called = {"value": False}

    def fail(*args, **kwargs):
        called["value"] = True
        raise AssertionError("urlopen should not be called inside the event loop")

    monkeypatch.setattr("linkedin_mcp_server.error_diagnostics.urlopen", fail)

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/test/",
        section_name="main_profile",
    )

    assert diagnostics["existing_issues"] == []
    assert diagnostics["issue_search_skipped"] is True
    assert called["value"] is False
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "search was skipped in async server context" in issue_body


def test_build_issue_diagnostics_covers_required_bug_report_fields(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [],
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="search_jobs",
        target_url="https://www.linkedin.com/jobs/search/?keywords=python",
        section_name="search_results",
    )

    issue_body = Path(diagnostics["issue_template_path"]).read_text()

    for label in _required_issue_form_labels():
        assert f"## {label}" in issue_body

    assert "- Installation method:" in issue_body
    assert "- MCP client:" in issue_body
    assert "- Error:" in issue_body
    assert "- Expected behavior:" in issue_body
    assert "1. Run a fresh local `uv run -m linkedin_mcp_server --login`." in issue_body
    assert "Call `search_jobs` again" in issue_body
    assert "## Additional Diagnostics" in issue_body
    assert "### Session State" in issue_body


def test_build_issue_diagnostics_marks_inferred_tool_and_container_runtime(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics.get_runtime_id",
        lambda: "linux-amd64-container",
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [],
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="search_jobs",
        target_url="https://www.linkedin.com/jobs/search/?keywords=python",
        section_name="search_results",
    )

    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "- Installation method: Docker using" in issue_body
    assert "`~/.linkedin-mcp` mounted into `/home/pwuser/.linkedin-mcp`" in issue_body
    assert "- [x] Docker" in issue_body
    assert "- Tool: search_jobs" in issue_body


def test_installation_method_lines_marks_managed_runtime() -> None:
    lines = _installation_method_lines(
        {
            "current_runtime_id": "macos-arm64-host",
        }
    )

    assert lines[0].startswith("- [ ] Docker")
    assert (
        lines[1]
        == "- [x] Managed runtime (Claude Desktop MCP Bundle, `uvx`, or local `uv run` setup)"
    )


def test_installation_method_summary_returns_managed_runtime_for_non_container() -> (
    None
):
    summary = _installation_method_summary(
        {
            "current_runtime_id": "macos-arm64-host",
        }
    )

    assert (
        summary
        == "Managed runtime (Claude Desktop MCP Bundle, `uvx`, or local `uv run` setup)"
    )


def test_build_issue_diagnostics_keeps_sensitive_runtime_details_out_of_mcp_payload(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [],
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/test/",
        section_name="main_profile",
    )

    assert diagnostics["issue_template_path"]
    assert "issue_template" not in diagnostics
    assert "hostname" not in diagnostics["runtime"]
    assert "source_profile_dir" not in diagnostics["runtime"]
    assert diagnostics["issue_search_skipped"] is False
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "### Runtime Diagnostics" in issue_body
    assert "Source profile:" in issue_body
