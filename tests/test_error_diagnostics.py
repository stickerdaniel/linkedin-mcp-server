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
    assert diagnostics["section_name"] == "posts"
    assert diagnostics["runtime"]["trace_dir"] is not None
    issue_body = diagnostics["issue_template"]
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
