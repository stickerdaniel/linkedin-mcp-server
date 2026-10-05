"""The consumer contract; rule and grammar tests live in post-no-bills."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "check-attribution.yml"
_TEMPLATE = _REPO_ROOT / ".github" / "pull_request_template.md"
_ACTION = "stickerdaniel/post-no-bills"


def _workflow() -> dict[str, Any]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    if True in workflow:
        workflow["on"] = workflow.pop(True)
    return workflow


def test_workflow_checks_attribution_in_required_job() -> None:
    workflow = _workflow()
    assert workflow["on"] == {
        "pull_request_target": {
            "types": ["opened", "synchronize", "reopened", "edited"]
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert set(workflow["jobs"]) == {"check-bot-coauthors"}
    job = workflow["jobs"]["check-bot-coauthors"]
    assert "if" not in job
    assert "continue-on-error" not in job
    assert "permissions" not in job
    assert "name" not in job
    assert job["runs-on"] == "ubuntu-latest"
    (step,) = job["steps"]
    assert set(step) == {"name", "uses", "with", "env"}
    assert re.fullmatch(rf"{re.escape(_ACTION)}@[0-9a-f]{{40}}", step["uses"])
    assert step["with"] == {"require-model-attribution": True}
    assert step["env"] == {
        "GUARDRAILS_WORKFLOW_REF": "${{ github.workflow_ref }}",
        "GUARDRAILS_WORKFLOW_SHA": "${{ github.workflow_sha }}",
        "GUARDRAILS_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
        "GUARDRAILS_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    }
    # Renovate updates both the digest and version without changing this test.
    pin_line = next(
        line
        for line in _WORKFLOW.read_text().splitlines()
        if f"uses: {step['uses']}" in line
    )
    assert re.search(r" # v[0-9]+\.[0-9]+\.[0-9]+$", pin_line)


def test_template_ends_with_editable_attribution_placeholder() -> None:
    lines = [
        line.strip() for line in _TEMPLATE.read_text().splitlines() if line.strip()
    ]
    assert lines[-1] == "Generated with [model] for [job] in [harness]."
    assert '"Generated with <model>" with or without a final period' in lines[-2]
    assert "detailed form below, which requires its final period" in lines[-2]
    assert "coding-agent runtime" in lines[-2]
    assert "in Claude Code via T3 Code" in lines[-2]


def test_guardrails_updates_have_isolated_and_bounded_automerge_rules() -> None:
    config = json.loads((_REPO_ROOT / ".github" / "renovate.json").read_text())
    automatic, manual = config["packageRules"][-2:]
    for rule in (automatic, manual):
        assert rule["matchManagers"] == ["github-actions"]
        assert rule["matchPackageNames"] == [_ACTION]
    assert automatic["matchUpdateTypes"] == ["minor", "patch"]
    assert automatic["groupName"] == "post-no-bills"
    assert automatic["groupSlug"] == "post-no-bills"
    assert automatic["automerge"] is True
    assert manual["matchUpdateTypes"] == ["major", "digest", "pin", "pinDigest"]
    assert manual["groupName"] == "post-no-bills manual"
    assert manual["groupSlug"] == "post-no-bills-manual"
    assert manual["automerge"] is False
