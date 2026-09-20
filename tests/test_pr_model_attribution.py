from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check_pr_model_attribution.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "check-attribution.yml"
_TEMPLATE = _REPO_ROOT / ".github" / "pull_request_template.md"
_SPEC = importlib.util.spec_from_file_location("check_pr_model_attribution", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
attribution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(attribution)


def _workflow() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


@pytest.mark.parametrize(
    "line",
    [
        "Generated with Claude Sonnet 4.5 for implementation in Claude Code.",
        (
            "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 "
            "for review in Claude Code."
        ),
        "Generated with GPT-5.6 Sol for planning, implementation, review in T3 Code.",
        "Generated with GPT-5.6 Sol for implementation/testing in T3 Code.",
        "Generated with GPT-5.6 Sol for security review for CI in T3 Code.",
        (
            "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
            "review and Codex for testing in T3 Code."
        ),
    ],
)
def test_accepts_supported_attribution_forms(line: str) -> None:
    assert attribution.has_model_attribution(line)


def test_accepts_trailing_blank_lines() -> None:
    body = (
        "## Summary\n\nDone.\n\n"
        "Generated with GPT-5.6 for implementation in Claude Code.\n\n"
    )

    assert attribution.has_model_attribution(body)


@pytest.mark.parametrize(
    "body",
    [
        "## Summary\n\nNo attribution here.",
        (
            "Generated with GPT-5.6 for implementation in Claude Code.\n\n"
            "A later non-empty line."
        ),
        "Generated with GPT-5.6 in Claude Code.",
        "Generated with GPT-5.6 for implementation.",
        "Generated with GPT-5.6 for implementation and testing in T3 Code.",
        (
            "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
            "review and Codex in T3 Code."
        ),
        (
            "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
            "in Claude Code."
        ),
        (
            "Generated with Claude Sonnet 4.5 for implementation and  for review "
            "in Claude Code."
        ),
        (
            "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for "
            "review and Codex for in Claude Code."
        ),
        "Generated with <model> for <job> in <harness>.",
        "Generated with [model] for [job] in [harness].",
        "",
        None,
    ],
)
def test_rejects_invalid_or_missing_attribution(body: str | None) -> None:
    assert not attribution.has_model_attribution(body)


def test_reads_body_from_event_file(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    body = "Generated with GPT-5.6 for implementation in Claude Code."
    event_path.write_text(
        json.dumps({"pull_request": {"body": body}}), encoding="utf-8"
    )

    assert attribution.read_pr_body(event_path) == body
    assert attribution.main([str(event_path)]) == 0


def test_reads_event_path_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"body": None}}), encoding="utf-8"
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    assert attribution.main([]) == 1


def test_template_ends_with_editable_attribution_placeholder() -> None:
    lines = [
        line.strip() for line in _TEMPLATE.read_text().splitlines() if line.strip()
    ]

    assert lines[-1] == "Generated with [model] for [job] in [harness]."
    assert not attribution.is_valid_attribution(lines[-1])


def test_workflow_checks_attribution_in_required_job() -> None:
    workflow = _workflow()
    trigger_types = workflow["on"]["pull_request_target"]["types"]
    job = workflow["jobs"]["check-bot-coauthors"]
    ordered_steps = job["steps"]
    steps = {step.get("name"): step for step in ordered_steps}

    assert "pull_request" not in workflow["on"]
    assert trigger_types == ["opened", "synchronize", "reopened", "edited"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["group"] == (
        "${{ github.workflow }}-${{ github.event.pull_request.number }}"
    )

    checkout = steps["Check out trusted workflow revision"]
    assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
    assert checkout["with"]["ref"] != "${{ github.event.pull_request.base.sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 1

    validator = steps["Check PR model attribution"]
    fetch = steps["Fetch PR commits"]
    assert validator["run"] == "python scripts/check_pr_model_attribution.py"
    assert validator["if"] == (
        "github.event.pull_request.user.login != 'dependabot[bot]' && "
        "github.event.pull_request.user.login != 'renovate[bot]'"
    )
    assert ordered_steps.index(validator) < ordered_steps.index(fetch)
    assert fetch["env"]["BASE_REF"] == "${{ github.event.pull_request.base.ref }}"
    assert fetch["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert 'case "$PR_NUMBER"' in fetch["run"]
    assert 'git check-ref-format "refs/heads/${BASE_REF}"' in fetch["run"]
    assert '"refs/heads/${BASE_REF}"' in fetch["run"]
    assert '"refs/pull/${PR_NUMBER}/head"' in fetch["run"]
    assert "git checkout" not in fetch["run"]

    for name in ("Check for bot Co-Authored-By lines", "Check for bot commit authors"):
        assert steps[name]["env"]["BASE_SHA"] == (
            "${{ github.event.pull_request.base.sha }}"
        )
        assert steps[name]["env"]["HEAD_SHA"] == (
            "${{ github.event.pull_request.head.sha }}"
        )


def test_failure_emits_actionable_github_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"body": "No attribution"}}), encoding="utf-8"
    )

    assert attribution.main([str(event_path)]) == 1
    output = capsys.readouterr().out
    assert output.startswith("::error title=PR model attribution required::")
    assert "final non-empty PR body line" in output
    assert 'Use commas or "/" between jobs for one model' in output
    assert (
        "Generated with Claude Sonnet 4.5 for implementation, testing in Claude Code."
        in output
    )
    assert (
        "Generated with Claude Sonnet 4.5 for implementation and GPT-5.6 for review "
        "in Claude Code."
    ) in output
