"""Decisions for the optional CI matrices, the required check, and manual evidence."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).parents[1]
_CHANGES = _REPO_ROOT / "scripts" / "ci_changes.py"
_GATE = _REPO_ROOT / "scripts" / "ci_gate.py"
_EVIDENCE = _REPO_ROOT / "scripts" / "ci_evidence.py"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_EVIDENCE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "differential-evidence.yml"

_GIT_ENV = os.environ.copy()
_GIT_ENV.update(
    {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "CI",
        "GIT_AUTHOR_EMAIL": "ci@example.com",
        "GIT_COMMITTER_NAME": "CI",
        "GIT_COMMITTER_EMAIL": "ci@example.com",
    }
)


def _load(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve the class's module while the class statement runs.
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


ci_changes = _load(_CHANGES)
ci_gate = _load(_GATE)
ci_evidence = _load(_EVIDENCE)


def _workflow(path: Path) -> dict[str, Any]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=_GIT_ENV,
    )
    return completed.stdout.decode()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    return repo


def _write(repo: Path, relative: str, text: str = "x\n") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _run_script(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize(
    "path",
    [
        "linkedin_mcp_server/core/browser.py",
        "tests/test_daemon_lock.py",
        "tests/fixtures/tool-contract/tools.json",
        "tests/README.md",
        "scripts/ci_changes.py",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "docker-compose.yml",
        ".dockerignore",
        "requirements/build-constraints.txt",
        "build/Dockerfile",
        "manifest.json",
        ".pre-commit-config.yaml",
        "plugins/linkedin-mcp-server/server.py",
    ],
)
def test_relevant_and_unknown_paths_need_the_optional_matrices(path: str) -> None:
    assert ci_changes.needs_optional_matrices([path]) is True


@pytest.mark.parametrize(
    "path",
    [
        "docs/docker-hub.md",
        "docs/CHANGELOG.md",
        "changelog.d/1058.feat.md",
        "README.md",
        "AGENTS.md",
        "assets/icons/icon.png",
        "plugins/linkedin-mcp-server/assets/icon.svg",
        "LICENSE",
        "NOTICE",
        ".github/CONTRIBUTING.md",
    ],
)
def test_documentation_changelog_and_assets_skip_the_optional_matrices(
    path: str,
) -> None:
    assert ci_changes.needs_optional_matrices([path]) is False


def test_one_relevant_path_among_docs_needs_the_matrices() -> None:
    assert (
        ci_changes.needs_optional_matrices(
            ["README.md", "changelog.d/1.fix.md", "linkedin_mcp_server/cli.py"]
        )
        is True
    )


def test_an_empty_diff_skips_the_optional_matrices() -> None:
    assert ci_changes.needs_optional_matrices([]) is False


def test_a_stack_compares_against_its_own_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md")
    main = _commit(repo, "docs")
    _git(repo, "checkout", "-b", "feature")
    _write(repo, "linkedin_mcp_server/feature.py", "feature\n")
    feature = _commit(repo, "feature")
    _git(repo, "checkout", "-b", "stack")
    _write(repo, "docs/stack.md", "stack\n")
    stack = _commit(repo, "stack")

    stack_paths = ci_changes.changed_paths(repo, feature, stack)
    assert stack_paths == ["docs/stack.md"]
    assert ci_changes.matrices_decision(repo, feature, stack) == "skip"
    assert main not in {feature, stack}
    assert ci_changes.matrices_decision(repo, main, feature) == "required"


def test_a_moved_base_tip_is_not_part_of_the_pull_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md")
    _commit(repo, "base")
    _git(repo, "checkout", "-b", "feature")
    _write(repo, "docs/feature.md", "feature\n")
    feature = _commit(repo, "feature docs")
    _git(repo, "checkout", "main")
    _write(repo, "linkedin_mcp_server/from_main.py", "main\n")
    moved_base = _commit(repo, "main code")

    paths = ci_changes.changed_paths(repo, moved_base, feature)
    assert paths == ["docs/feature.md"]
    assert ci_changes.matrices_decision(repo, moved_base, feature) == "skip"


def test_a_rename_out_of_a_relevant_tree_keeps_the_deleted_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "tests/old_name.py", "same\n")
    base = _commit(repo, "test")
    (repo / "docs").mkdir()
    _git(repo, "mv", "tests/old_name.py", "docs/renamed.md")
    head = _commit(repo, "rename")

    paths = ci_changes.changed_paths(repo, base, head)
    assert set(paths) == {"tests/old_name.py", "docs/renamed.md"}
    assert ci_changes.matrices_decision(repo, base, head) == "required"


def test_deleting_a_test_needs_the_matrices_and_deleting_docs_does_not(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _write(repo, "tests/doomed.py")
    _write(repo, "docs/old.md")
    base = _commit(repo, "both")
    (repo / "tests" / "doomed.py").unlink()
    deleted_test = _commit(repo, "delete test")
    assert ci_changes.changed_paths(repo, base, deleted_test) == ["tests/doomed.py"]
    assert ci_changes.matrices_decision(repo, base, deleted_test) == "required"

    (repo / "docs" / "old.md").unlink()
    deleted_doc = _commit(repo, "delete doc")
    assert ci_changes.changed_paths(repo, deleted_test, deleted_doc) == ["docs/old.md"]
    assert ci_changes.matrices_decision(repo, deleted_test, deleted_doc) == "skip"


def test_paths_with_spaces_and_newlines_stay_one_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md")
    base = _commit(repo, "base")
    _write(repo, "tests/my test.py", "code\n")
    weird = "docs/ok\nlinkedin_mcp_server_x.py"
    _write(repo, weird, "doc\n")
    head = _commit(repo, "odd names")

    paths = ci_changes.changed_paths(repo, base, head)
    assert "tests/my test.py" in paths
    assert weird in paths
    assert ci_changes.matrices_decision(repo, base, head) == "required"

    docs_only = _repo(tmp_path / "docs-only")
    _write(docs_only, "README.md")
    docs_base = _commit(docs_only, "base")
    _write(docs_only, weird, "doc\n")
    docs_head = _commit(docs_only, "newline doc")
    assert ci_changes.changed_paths(docs_only, docs_base, docs_head) == [weird]
    assert ci_changes.matrices_decision(docs_only, docs_base, docs_head) == "skip"


def test_a_missing_or_malformed_revision_fails_without_a_decision(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md")
    head = _commit(repo, "one")
    output = tmp_path / "output"
    missing = "0" * 40
    for base, head_sha in (
        (missing, head),
        ("not-a-sha", head),
        (head, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; touch pwned"),
    ):
        output.unlink(missing_ok=True)
        env = _GIT_ENV.copy()
        env.update(
            {
                "CI_CHANGES_REPO": str(repo),
                "BASE_SHA": base,
                "HEAD_SHA": head_sha,
                "GITHUB_OUTPUT": str(output),
            }
        )
        completed = _run_script(_CHANGES, env)
        assert completed.returncode != 0
        written = output.read_text(encoding="utf-8") if output.exists() else ""
        assert "matrices=" not in written


def test_the_changes_cli_writes_the_decision(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md")
    base = _commit(repo, "docs")
    _write(repo, "tests/new.py")
    head = _commit(repo, "test")
    output = tmp_path / "output"
    env = _GIT_ENV.copy()
    env.update(
        {
            "CI_CHANGES_REPO": str(repo),
            "BASE_SHA": base,
            "HEAD_SHA": head,
            "GITHUB_OUTPUT": str(output),
        }
    )
    completed = _run_script(_CHANGES, env)
    assert completed.returncode == 0
    assert output.read_text(encoding="utf-8") == "matrices=required\n"


def _gate_env(**overrides: str) -> dict[str, str]:
    env = _GIT_ENV.copy()
    env.update(
        {
            "EVENT_NAME": "pull_request",
            "LINUX_RESULT": "success",
            "DEPENDENCY_REVIEW_RESULT": "success",
            "DETECTION_RESULT": "success",
            "MATRICES": "required",
            "WINDOWS_DAEMON_RESULT": "success",
            "PLATFORM_BEHAVIOUR_RESULT": "success",
        }
    )
    env.update(overrides)
    return env


@pytest.mark.parametrize(
    ("overrides", "allowed", "reason"),
    [
        ({}, True, "ok"),
        ({"WINDOWS_DAEMON_RESULT": "skipped"}, False, "windows-daemon"),
        ({"PLATFORM_BEHAVIOUR_RESULT": "skipped"}, False, "platform-behaviour"),
        ({"WINDOWS_DAEMON_RESULT": "failure"}, False, "windows-daemon"),
        ({"PLATFORM_BEHAVIOUR_RESULT": "cancelled"}, False, "platform-behaviour"),
        (
            {
                "MATRICES": "skip",
                "WINDOWS_DAEMON_RESULT": "skipped",
                "PLATFORM_BEHAVIOUR_RESULT": "skipped",
            },
            True,
            "ok",
        ),
        (
            {
                "MATRICES": "skip",
                "WINDOWS_DAEMON_RESULT": "success",
                "PLATFORM_BEHAVIOUR_RESULT": "skipped",
            },
            False,
            "windows-daemon",
        ),
        (
            {
                "MATRICES": "skip",
                "WINDOWS_DAEMON_RESULT": "failure",
                "PLATFORM_BEHAVIOUR_RESULT": "skipped",
            },
            False,
            "windows-daemon",
        ),
        ({"MATRICES": ""}, False, "detection"),
        ({"MATRICES": "maybe"}, False, "detection"),
        ({"DETECTION_RESULT": "failure", "MATRICES": "required"}, False, "detection"),
        ({"DETECTION_RESULT": "skipped"}, False, "detection"),
        ({"DETECTION_RESULT": "cancelled"}, False, "detection"),
        ({"DETECTION_RESULT": ""}, False, "detection"),
        ({"LINUX_RESULT": "failure"}, False, "linux"),
        ({"LINUX_RESULT": "skipped"}, False, "linux"),
        ({"LINUX_RESULT": "cancelled"}, False, "linux"),
        ({"LINUX_RESULT": ""}, False, "linux"),
        ({"DEPENDENCY_REVIEW_RESULT": "skipped"}, False, "dependency-review"),
        ({"DEPENDENCY_REVIEW_RESULT": "failure"}, False, "dependency-review"),
        ({"DEPENDENCY_REVIEW_RESULT": "cancelled"}, False, "dependency-review"),
        ({"DEPENDENCY_REVIEW_RESULT": ""}, False, "dependency-review"),
        ({"EVENT_NAME": "workflow_dispatch"}, False, "event"),
        ({"EVENT_NAME": ""}, False, "event"),
    ],
)
def test_pull_request_gate(
    overrides: dict[str, str], allowed: bool, reason: str
) -> None:
    env = _gate_env(**overrides)
    result = ci_gate.evaluate_gate(
        event=env["EVENT_NAME"],
        linux=env["LINUX_RESULT"],
        dependency_review=env["DEPENDENCY_REVIEW_RESULT"],
        detection=env["DETECTION_RESULT"],
        matrices=env["MATRICES"],
        windows=env["WINDOWS_DAEMON_RESULT"],
        platform=env["PLATFORM_BEHAVIOUR_RESULT"],
    )
    assert result.allowed is allowed
    assert result.reason == reason
    completed = _run_script(_GATE, env)
    assert (completed.returncode == 0) is allowed
    if not allowed:
        assert f"ci gate: {reason}" in completed.stderr


def test_main_requires_linux_and_skipped_optional_matrices() -> None:
    result = ci_gate.evaluate_gate(
        event="push",
        linux="success",
        dependency_review="skipped",
        detection="skipped",
        matrices="",
        windows="skipped",
        platform="skipped",
    )
    assert result.allowed is True

    ran_anyway = ci_gate.evaluate_gate(
        event="push",
        linux="success",
        dependency_review="skipped",
        detection="skipped",
        matrices="",
        windows="success",
        platform="skipped",
    )
    assert ran_anyway.allowed is False
    assert ran_anyway.reason == "windows-daemon"

    cancelled = ci_gate.evaluate_gate(
        event="push",
        linux="cancelled",
        dependency_review="skipped",
        detection="skipped",
        matrices="",
        windows="skipped",
        platform="skipped",
    )
    assert cancelled.allowed is False
    assert cancelled.reason == "linux"


def test_a_failed_detector_fails_main_even_when_the_matrices_were_skipped() -> None:
    result = ci_gate.evaluate_gate(
        event="push",
        linux="success",
        dependency_review="skipped",
        detection="failure",
        matrices="",
        windows="skipped",
        platform="skipped",
    )
    assert result.allowed is False
    assert result.reason == "detection"


def test_manual_selection_is_one_platform_and_one_group() -> None:
    jobs = ci_evidence.evidence_matrix("ubuntu-latest", "native")
    assert jobs == [
        {
            "os": "ubuntu-latest",
            "rows": "native",
            "browsers": "~/.cache/ms-playwright",
        }
    ]
    assert len(ci_evidence.evidence_matrix("all", "pipes")) == len(
        ci_evidence.PLATFORMS
    )
    assert len(ci_evidence.evidence_matrix("macos-latest", "all")) == len(
        ci_evidence.GROUPS
    )
    everything = ci_evidence.evidence_matrix("all", "all")
    assert len(everything) == len(ci_evidence.PLATFORMS) * len(ci_evidence.GROUPS)
    assert {(job["os"], job["rows"]) for job in everything} == {
        (os_name, group)
        for os_name in ci_evidence.PLATFORMS
        for group in ci_evidence.GROUPS
    }


@pytest.mark.parametrize(
    ("platform", "group"),
    [("nope", "native"), ("ubuntu-latest", "nope"), ("", ""), ("all", "")],
)
def test_an_unknown_manual_selection_fails_without_a_matrix(
    tmp_path: Path, platform: str, group: str
) -> None:
    with pytest.raises(ci_evidence.CiEvidenceError):
        ci_evidence.evidence_matrix(platform, group)
    output = tmp_path / "output"
    env = _GIT_ENV.copy()
    env.update(
        {
            "EVIDENCE_PLATFORM": platform,
            "EVIDENCE_GROUP": group,
            "GITHUB_OUTPUT": str(output),
        }
    )
    completed = _run_script(_EVIDENCE, env)
    assert completed.returncode != 0
    written = output.read_text(encoding="utf-8") if output.exists() else ""
    assert "include=" not in written


def test_the_evidence_cli_writes_json_for_the_selected_jobs(tmp_path: Path) -> None:
    output = tmp_path / "output"
    env = _GIT_ENV.copy()
    env.update(
        {
            "EVIDENCE_PLATFORM": "windows-latest",
            "EVIDENCE_GROUP": "auth",
            "GITHUB_OUTPUT": str(output),
        }
    )
    completed = _run_script(_EVIDENCE, env)
    assert completed.returncode == 0
    jobs = json.loads(output.read_text(encoding="utf-8").removeprefix("include="))
    assert jobs == [
        {
            "os": "windows-latest",
            "rows": "auth",
            "browsers": "~\\AppData\\Local\\ms-playwright",
        }
    ]


_BROWSER_CACHE_KEY = (
    "ms-playwright-${{ runner.os }}-${{ runner.arch }}"
    "-${{ steps.patchright.outputs.version }}"
)
_NATIVE_ROWS = (
    "tests/differential/test_synthetic_origin.py",
    "tests/differential/test_host_quit_row.py",
    "tests/differential/test_frozen_rows.py",
    "tests/differential/test_host_comparison_rows.py",
    "tests/differential/test_owner_killed_rows.py",
    "tests/differential/test_failed_job_query_row.py",
    "tests/differential/test_unconfirmed_close_row.py",
)
_CALL_ROWS = (
    "tests/differential/test_call_loss_rows.py",
    "tests/differential/test_idle_race_rows.py",
    "tests/differential/test_owner_loss_rows.py",
    "tests/differential/test_profile_command_rows.py",
    "tests/differential/test_auth_repair_rows.py",
    "tests/differential/test_eligibility_rows.py",
)


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def _cache_keys(job: dict[str, Any]) -> list[str]:
    return [
        step["with"]["key"]
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/cache@")
    ]


def test_automatic_ci_keeps_linux_independent_and_gates_the_optional_matrices() -> None:
    workflow = _workflow(_CI)
    jobs = workflow["jobs"]
    linux = jobs["linux"]
    changes = jobs["changes"]
    windows = jobs["windows-daemon"]
    platform = jobs["platform-behaviour"]
    aggregate = jobs["test"]
    pull_request = workflow["on"]["pull_request"]

    assert pull_request is None or "branches" not in pull_request
    assert "workflow_dispatch" not in workflow["on"]
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert linux.get("needs") in (None, [])
    assert "windows-daemon" not in (linux.get("needs") or [])
    assert linux["env"]["FASTMCP_MCP_CAMELCASE_COMPAT"] == "false"
    linux_run = _runs(linux)
    assert "--dist loadgroup" in linux_run
    assert "test_retained_leader_pidfd_signals_its_group_after_reaping" in linux_run
    assert "--with-deps" in linux_run
    assert "--no-shell" in linux_run
    for row in (*_NATIVE_ROWS, *_CALL_ROWS):
        assert row not in linux_run

    assert changes["permissions"] == {"contents": "read"}
    checkout = changes["steps"][0]["with"]
    assert checkout["persist-credentials"] is False
    assert checkout["fetch-depth"] == 0
    detect = changes["steps"][1]
    assert "scripts/ci_changes.py" in detect["run"]
    assert "${{" not in detect["run"]
    assert detect["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert detect["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"

    for job in (windows, platform):
        assert job["needs"] == "changes"
        assert "pull_request" in job["if"]
        assert "needs.changes.result == 'success'" in job["if"]
        assert "needs.changes.outputs.matrices == 'required'" in job["if"]
        assert job["env"]["FASTMCP_MCP_CAMELCASE_COMPAT"] == "false"
    assert windows["strategy"]["matrix"]["python-version"] == ["3.12.4", "3.13", "3.14"]
    platform_run = _runs(platform)
    for kept in (
        "tests/test_profile_lease.py",
        "tests/test_process_tree.py",
        "tests/test_greenlet_runtime.py",
        "tests/test_browser_containment.py",
        "tests/test_windows_browser_launch_evidence.py",
        "tests/test_browser_identity.py",
        "tests/differential/test_watcher.py",
    ):
        assert kept in platform_run
    for row in _NATIVE_ROWS:
        assert row not in platform_run

    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == {
        "linux",
        "dependency-review",
        "changes",
        "windows-daemon",
        "platform-behaviour",
    }
    gate = next(
        step for step in aggregate["steps"] if "ci_gate.py" in step.get("run", "")
    )
    assert gate["if"] == "always()"
    assert "${{" not in gate["run"]
    assert _cache_keys(linux) == [_BROWSER_CACHE_KEY]
    assert _cache_keys(platform) == [_BROWSER_CACHE_KEY]
    for job, artifact in (
        (linux, "differential-evidence-ubuntu-latest"),
        (platform, "differential-evidence-${{ matrix.os }}"),
    ):
        uploads = [
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
            and step["with"]["name"] == artifact
        ]
        assert len(uploads) == 1
        assert uploads[0]["if"] == "always()"
    install = next(
        step
        for step in jobs["lint-and-check"]["steps"]
        if step.get("name") == "Install dependencies"
    )
    assert install["run"].strip() == "uv sync --group dev"


def test_manual_evidence_is_dispatch_only_and_defaults_to_one_linux_job() -> None:
    workflow = _workflow(_EVIDENCE_WORKFLOW)
    triggers = workflow["on"]
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    jobs = ci_evidence.evidence_matrix(
        inputs["platform"]["default"], inputs["group"]["default"]
    )
    assert jobs == [
        {
            "os": "ubuntu-latest",
            "rows": "native",
            "browsers": "~/.cache/ms-playwright",
        }
    ]
    assert set(inputs["platform"]["options"]) == {*ci_evidence.PLATFORMS, "all"}
    assert set(inputs["group"]["options"]) == {*ci_evidence.GROUPS, "all"}

    plan = workflow["jobs"]["plan"]
    evidence = workflow["jobs"]["evidence"]
    assert plan["permissions"] == {"contents": "read"}
    assert plan["steps"][0]["with"]["persist-credentials"] is False
    plan_step = plan["steps"][1]
    assert "scripts/ci_evidence.py" in plan_step["run"]
    assert "${{" not in plan_step["run"]
    assert evidence["needs"] == "plan"
    assert evidence.get("continue-on-error") is not True
    assert evidence["strategy"]["fail-fast"] is False
    assert (
        evidence["strategy"]["matrix"]["include"]
        == "${{ fromJson(needs.plan.outputs.include) }}"
    )
    assert evidence["env"]["FASTMCP_MCP_CAMELCASE_COMPAT"] == "false"
    assert _cache_keys(evidence) == [_BROWSER_CACHE_KEY]
    evidence_run = _runs(evidence)
    for row in (*_NATIVE_ROWS, *_CALL_ROWS):
        assert row in evidence_run
    assert "baseline.py" in evidence_run
    uploads = [
        step
        for step in evidence["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert {step["with"]["name"] for step in uploads} == {
        "differential-evidence-${{ matrix.os }}",
        "differential-calls-evidence-${{ matrix.os }}-${{ matrix.rows }}",
    }
    assert all(str(step["if"]).startswith("always()") for step in uploads)
    assert workflow["concurrency"]["cancel-in-progress"] is False
