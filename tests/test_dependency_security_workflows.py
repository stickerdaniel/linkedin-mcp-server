"""Contracts for dependency review and scheduled dependency audits."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "run_uv_audit", _REPO_ROOT / "scripts" / "run_uv_audit.py"
)
assert _SPEC is not None and _SPEC.loader is not None
run_uv_audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_uv_audit)
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_AUDIT_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "dependency-audit.yml"
_DEPENDENCY_REVIEW = (
    "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294 # v5.0.0"
)
_ACTION_PIN = re.compile(r"uses:\s+[^@\s]+@([0-9a-f]{40})\s+#\s+v\S+")


def _workflow(path: Path) -> dict[str, Any]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def test_dependency_review_blocks_high_advisories_through_test() -> None:
    workflow = _workflow(_CI_WORKFLOW)
    review = workflow["jobs"]["dependency-review"]
    aggregator = workflow["jobs"]["test"]

    assert "pull_request" in workflow["on"]
    assert "push" in workflow["on"]
    assert review["if"] == "github.event_name == 'pull_request'"
    assert review["permissions"] == {"contents": "read"}
    assert review["steps"][0]["uses"] == _DEPENDENCY_REVIEW.split(" #", 1)[0]
    assert review["steps"][0]["with"]["fail-on-severity"] == "high"
    assert review["steps"][0]["with"]["fail-on-scopes"] == (
        "runtime, development, unknown"
    )
    assert review["steps"][0]["with"]["retry-on-snapshot-warnings"] is True
    assert "dependency-review" in aggregator["needs"]
    assert aggregator["if"] == "always()"
    assert aggregator["steps"][0]["if"] == "github.event_name == 'pull_request'"
    assert "needs.dependency-review.result" in aggregator["steps"][0]["run"]


def test_dependency_review_aggregator_decision() -> None:
    workflow = _workflow(_CI_WORKFLOW)
    step = workflow["jobs"]["test"]["steps"][0]
    match = re.fullmatch(
        r"test '\$\{\{ needs\.dependency-review\.result \}\}' (=|!=) (\w+)",
        step["run"],
    )

    assert step["if"] == "github.event_name == 'pull_request'"
    assert match is not None
    operator, expected = match.groups()

    def allows(event: str, result: str) -> bool:
        if event != "pull_request":
            return True
        return (result == expected) if operator == "=" else (result != expected)

    assert allows("push", "skipped")
    assert allows("pull_request", "success")
    assert not allows("pull_request", "failure")
    assert not allows("pull_request", "skipped")


def test_dependency_review_rejects_snapshot_warnings(tmp_path: Path) -> None:
    workflow = _workflow(_CI_WORKFLOW)
    step = workflow["jobs"]["dependency-review"]["steps"][1]

    assert step["name"] == "Require complete dependency snapshots"
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert "x-github-dependency-graph-snapshot-warnings:" in step["run"]
    assert "raise SystemExit" in step["run"]
    assert "|| true" not in step["run"]

    script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    response = tmp_path / "response"
    for header, expected_exit in (
        ("", 0),
        ("x-github-dependency-graph-snapshot-warnings:\n", 0),
        ("X-GitHub-Dependency-Graph-Snapshot-Warnings: c25hcHNob3Q=\n", 1),
    ):
        response.write_text(f"HTTP/2 200 OK\n{header}\n[]\n", encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-", str(response)],
            input=script,
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == expected_exit


def test_dependency_audit_is_scheduled_manual_and_informational() -> None:
    workflow = _workflow(_AUDIT_WORKFLOW)
    triggers = workflow["on"]
    audit = workflow["jobs"]["audit"]

    assert triggers == {
        "schedule": [{"cron": "17 6 * * 1"}],
        "workflow_dispatch": None,
    }
    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert audit["strategy"]["fail-fast"] is False
    assert audit["strategy"]["matrix"]["scope"] == ["runtime", "full"]
    assert audit["steps"][1]["with"]["version"] == "0.12.13"
    assert audit["steps"][1]["with"]["enable-cache"] is False
    assert "scripts/run_uv_audit.py" in audit["steps"][2]["run"]
    assert audit["steps"][3]["if"] == "always()"
    assert audit["steps"][3]["with"]["if-no-files-found"] == "error"


def _vulnerability() -> dict[str, Any]:
    return {
        "dependency": {"name": "aiohttp", "version": "3.14.1"},
        "id": "GHSA-mfx4-hv73-q22v",
        "display_id": "GHSA-mfx4-hv73-q22v",
        "aliases": ["CVE-2026-69243"],
        "summary": "AIOHTTP request smuggling vulnerability",
        "description": None,
        "link": "https://github.com/aio-libs/aiohttp/security/advisories/GHSA-mfx4-hv73-q22v",
        "fix_versions": ["3.14.2"],
        "published": "2026-08-03T20:46:10Z",
        "modified": "2026-09-10T03:51:14Z",
    }


def _adverse_status() -> dict[str, Any]:
    return {
        "name": "example-package",
        "status": "archived",
        "reason": "The project is no longer maintained",
    }


def _audit_payload(
    vulnerabilities: int = 0, adverse_statuses: int = 0
) -> dict[str, Any]:
    return {
        "schema": {"version": "preview"},
        "summary": {
            "audited_packages": 12,
            "vulnerabilities": vulnerabilities,
            "adverse_statuses": adverse_statuses,
        },
        "vulnerabilities": [_vulnerability() for _ in range(vulnerabilities)],
        "adverse_statuses": [_adverse_status() for _ in range(adverse_statuses)],
    }


@pytest.mark.parametrize(
    ("returncode", "payload", "expected_state", "expected_exit"),
    [
        (0, _audit_payload(), "no_vulnerabilities", 0),
        (0, _audit_payload(adverse_statuses=1), "no_vulnerabilities", 0),
        (1, _audit_payload(vulnerabilities=1), "vulnerabilities", 0),
        (2, None, "scanner_error", 2),
    ],
)
def test_audit_states_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    returncode: int,
    payload: dict[str, Any] | None,
    expected_state: str,
    expected_exit: int,
) -> None:
    completed = subprocess.CompletedProcess(
        [],
        returncode,
        stdout=json.dumps(payload) if payload else "",
        stderr="scanner failed\n",
    )
    monkeypatch.setattr(
        run_uv_audit.subprocess, "run", lambda *args, **kwargs: completed
    )
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    output_dir = tmp_path / "report"

    assert run_uv_audit.run("runtime", output_dir) == expected_exit

    report = json.loads((output_dir / "audit-result.json").read_text(encoding="utf-8"))
    assert report["state"] == expected_state
    assert ("--no-dev" in report["command"]) is True
    summary = summary_path.read_text(encoding="utf-8")
    assert f"**State:** `{expected_state}`" in summary
    assert "best effort; completeness is not guaranteed" in summary


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema": {"version": "preview"},
            "summary": {
                "audited_packages": 12,
                "vulnerabilities": 0,
                "adverse_statuses": 0,
            },
        },
        {**_audit_payload(), "schema": {"version": "stable"}},
        {
            **_audit_payload(),
            "summary": {
                "audited_packages": 12,
                "vulnerabilities": 1,
                "adverse_statuses": 0,
            },
        },
        {
            **_audit_payload(),
            "summary": {
                "audited_packages": -1,
                "vulnerabilities": 0,
                "adverse_statuses": 0,
            },
        },
    ],
)
def test_incomplete_or_inconsistent_audit_json_is_a_scanner_error(
    payload: dict[str, Any],
) -> None:
    completed = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps(payload), stderr=""
    )

    state, _, error = run_uv_audit._classify(completed)

    assert state == "scanner_error"
    assert error is not None


@pytest.mark.parametrize(
    ("returncode", "payload"),
    [
        (1, {**_audit_payload(vulnerabilities=1), "vulnerabilities": [{}]}),
        (
            1,
            {
                **_audit_payload(vulnerabilities=1),
                "vulnerabilities": [{**_vulnerability(), "id": None}],
            },
        ),
        (
            1,
            {
                **_audit_payload(vulnerabilities=1),
                "vulnerabilities": [
                    {**_vulnerability(), "dependency": {"name": "aiohttp"}}
                ],
            },
        ),
        (0, {**_audit_payload(adverse_statuses=1), "adverse_statuses": [{}]}),
        (
            0,
            {
                **_audit_payload(adverse_statuses=1),
                "adverse_statuses": [{**_adverse_status(), "reason": 1}],
            },
        ),
    ],
)
def test_malformed_audit_result_entries_are_scanner_errors(
    returncode: int, payload: dict[str, Any]
) -> None:
    completed = subprocess.CompletedProcess(
        [], returncode, stdout=json.dumps(payload), stderr=""
    )

    state, _, error = run_uv_audit._classify(completed)

    assert state == "scanner_error"
    assert error is not None


def test_full_audit_includes_the_development_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    completed = subprocess.CompletedProcess(
        [],
        0,
        stdout=json.dumps(_audit_payload()),
        stderr="",
    )
    monkeypatch.setattr(
        run_uv_audit.subprocess, "run", lambda *args, **kwargs: completed
    )

    assert run_uv_audit.run("full", tmp_path) == 0

    report = json.loads((tmp_path / "audit-result.json").read_text(encoding="utf-8"))
    assert "--no-dev" not in report["command"]
    assert "--frozen" in report["command"]


def test_new_workflow_actions_are_sha_pinned() -> None:
    for path in (_CI_WORKFLOW, _AUDIT_WORKFLOW):
        workflow = path.read_text(encoding="utf-8")
        uses = [line for line in workflow.splitlines() if "uses:" in line]
        assert uses
        assert all(_ACTION_PIN.search(line) for line in uses)


def test_dependency_review_source_is_registered() -> None:
    registry = (_REPO_ROOT / "btca.config.jsonc").read_text(encoding="utf-8")

    assert '"name": "githubDependencyReviewAction"' in registry
    assert '"url": "https://github.com/actions/dependency-review-action"' in registry
    assert '"branch": "main"' in registry
