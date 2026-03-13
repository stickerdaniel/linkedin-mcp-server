from pathlib import Path

import pytest

from linkedin_mcp_server.error_diagnostics import (
    build_issue_diagnostics,
    format_tool_error_with_diagnostics,
)


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
    assert "`~/.linkedin-mcp` mounted into `/home/pwuser/.linkedin-mcp`" in issue_body
    assert "- [x] Docker" in issue_body
    assert "  - [x] search_jobs" in issue_body


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
    assert "## Runtime Diagnostics" in issue_body
    assert "Source profile:" in issue_body
