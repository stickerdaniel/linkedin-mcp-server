from pathlib import Path

import pytest

from linkedin_mcp_server.error_diagnostics import (
    ISSUE_URL,
    PACKET_GUIDANCE,
    PACKET_SKILL_URL,
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
    assert diagnostics["runtime"]["suggested_gist_command"] is None
    assert "issue_template" not in diagnostics
    assert "hostname" not in diagnostics["runtime"]
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "## Advisory open issues" in issue_body
    assert "#220" in issue_body
    assert "Candidate open issues to review" in issue_body
    assert "gist" not in issue_body.lower()
    assert PACKET_GUIDANCE in issue_body
    assert PACKET_SKILL_URL in issue_body
    assert ISSUE_URL in issue_body


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

    assert "- Local diagnostic notes: /tmp/issue.md" in message
    assert "- Local trace artifacts: /tmp/trace" in message
    assert "- Local server log: /tmp/trace/server.log" in message
    assert "Candidate open issues to review" in message
    assert "#220" in message
    assert PACKET_GUIDANCE in message
    assert "gist" not in message.lower()
    assert "File the issue here" not in message
    assert "- Runtime: linux-arm64-container" in message
    assert "test-host" not in message


def test_format_tool_error_empty_search_still_requires_packet_search():
    message = format_tool_error_with_diagnostics(
        "Scrape failed",
        {
            "issue_template_path": "/tmp/issue.md",
            "existing_issues": [],
            "issue_search_skipped": False,
            "runtime": {"current_runtime_id": "macos-arm64-host"},
        },
    )

    assert "did not establish a canonical match" in message
    assert "Packet search of open and closed issues is still required" in message
    assert PACKET_GUIDANCE in message


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
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "did not establish a canonical match" in issue_body
    assert "No matching open issues found" not in issue_body


def test_build_issue_diagnostics_sets_gist_command_none(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues",
        lambda payload: [],
    )
    # The gist command lists server.log only if the process-wide trace dir has
    # one. That dir is global state another test on the same xdist worker can
    # set, so pin it off here to keep this "log is absent" case deterministic.
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics.mark_trace_for_retention",
        lambda: None,
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics.get_trace_dir", lambda: None
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/test/",
        section_name="main_profile",
    )

    assert diagnostics["runtime"]["suggested_gist_command"] is None
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "gist" not in issue_body.lower()
    assert "gh gist create" not in issue_body


@pytest.mark.asyncio
async def test_build_issue_diagnostics_skips_network_search_in_event_loop(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    called = {"urlopen": False, "find": False}

    def fail_urlopen(*args, **kwargs):
        called["urlopen"] = True
        raise AssertionError("urlopen should not be called inside the event loop")

    def fail_find(*args, **kwargs):
        called["find"] = True
        raise AssertionError(
            "_find_existing_issues should not be called inside the event loop"
        )

    monkeypatch.setattr("linkedin_mcp_server.error_diagnostics.urlopen", fail_urlopen)
    monkeypatch.setattr(
        "linkedin_mcp_server.error_diagnostics._find_existing_issues", fail_find
    )

    diagnostics = build_issue_diagnostics(
        RuntimeError("boom"),
        context="extract-page",
        target_url="https://www.linkedin.com/in/test/",
        section_name="main_profile",
    )

    assert diagnostics["existing_issues"] == []
    assert diagnostics["issue_search_skipped"] is True
    assert called["urlopen"] is False
    assert called["find"] is False
    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "search was skipped in async server context" in issue_body
    assert "Packet search of open and closed issues is still required" in issue_body


def test_build_issue_diagnostics_preserves_captured_failure(monkeypatch, tmp_path):
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

    assert diagnostics["context"] == "search_jobs"
    assert diagnostics["error_type"] == "RuntimeError"
    assert diagnostics["error_message"] == "boom"
    assert diagnostics["target_url"].endswith("keywords=python")
    assert diagnostics["section_name"] == "search_results"
    assert Path(diagnostics["issue_template_path"]).is_file()
    assert "# Local diagnostic notes" in issue_body
    assert "- Context: search_jobs" in issue_body
    assert "- Section: search_results" in issue_body
    assert "- Error: RuntimeError: boom" in issue_body
    assert "MCP client" not in issue_body
    assert "Installation method" not in issue_body
    assert "uv run -m linkedin_mcp_server --login" not in issue_body
    assert "Expected behavior" not in issue_body
    assert "curl-based" not in issue_body
    assert "Call `search_jobs` again" not in issue_body


def test_build_issue_diagnostics_does_not_invent_install_or_tool_call(
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
        context="extract_saved_jobs_page",
        target_url="https://www.linkedin.com/my-items/saved-jobs/",
        section_name="saved_jobs",
    )

    issue_body = Path(diagnostics["issue_template_path"]).read_text()
    assert "- Current runtime: linux-amd64-container" in issue_body
    assert "- Context: extract_saved_jobs_page" in issue_body
    assert "Tool: get_saved_jobs" not in issue_body
    assert "Docker using" not in issue_body
    assert "`~/.linkedin-mcp` mounted" not in issue_body


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
    assert "## Local runtime" in issue_body
    assert "Source profile (local):" in issue_body
    assert "upload" not in issue_body.lower()
