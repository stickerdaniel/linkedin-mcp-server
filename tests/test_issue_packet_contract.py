"""Contract tests for GitHub issue forms and packet structure."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml

ISSUE_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE"

COMMON_REQUIRED_IDS = {
    "packet-summary",
    "related-issues",
    "evidence",
    "steps-to-reproduce",
}
RUNTIME_REQUIRED_IDS = {"setup", "linkedin-variant"}

ISSUE_FORM_FILES = [
    "bug_report.yml",
    "feature_request.yml",
    "documentation_issue.yml",
    "chore.yml",
]

FORBIDDEN_PUNCTUATION = {"—", "–", "“", "”", "‘", "’"}
EMOJI_PATTERN = re.compile(r"[\U00010000-\U0010ffff]|[☀-➿]|[⌀-⏿]|[⭐-⭕]")


def _load_yaml(filename: str) -> dict[str, Any]:
    path = ISSUE_TEMPLATE_DIR / filename
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _assert_plain_text(text: str, context: str) -> None:
    for char in FORBIDDEN_PUNCTUATION:
        assert char not in text, (
            f"Found forbidden character {char!r} in {context}: {text!r}"
        )
    assert not EMOJI_PATTERN.search(text), f"Found emoji in {context}: {text!r}"


def test_issue_forms_share_packet_required_ids() -> None:
    config_data = _load_yaml("config.yml")
    assert "body" not in config_data, "config.yml should not be an issue template"

    bug_report = _load_yaml("bug_report.yml")
    feature_request = _load_yaml("feature_request.yml")
    documentation_issue = _load_yaml("documentation_issue.yml")
    chore = _load_yaml("chore.yml")

    runtime_and_common = COMMON_REQUIRED_IDS | RUNTIME_REQUIRED_IDS

    for filename, data, expected_ids in [
        ("bug_report.yml", bug_report, runtime_and_common),
        ("feature_request.yml", feature_request, runtime_and_common),
        ("documentation_issue.yml", documentation_issue, COMMON_REQUIRED_IDS),
        ("chore.yml", chore, COMMON_REQUIRED_IDS),
    ]:
        body = data.get("body", [])
        field_map = {
            item["id"]: item for item in body if isinstance(item, dict) and "id" in item
        }
        for expected_id in expected_ids:
            assert expected_id in field_map, (
                f"{filename} missing required id {expected_id!r}"
            )
            item = field_map[expected_id]
            validations = item.get("validations") or {}
            assert validations.get("required") is True, (
                f"{filename} field {expected_id!r} must have required: true"
            )


def test_issue_forms_have_unique_ids_and_labels() -> None:
    for filename in ISSUE_FORM_FILES:
        data = _load_yaml(filename)
        body = data.get("body", [])
        seen_ids: set[str] = set()
        seen_labels: set[str] = set()
        for item in body:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id:
                assert item_id not in seen_ids, (
                    f"Duplicate id {item_id!r} in {filename}"
                )
                seen_ids.add(item_id)
            attrs = item.get("attributes") or {}
            label = attrs.get("label")
            if label:
                assert label not in seen_labels, (
                    f"Duplicate label {label!r} in {filename}"
                )
                seen_labels.add(label)


def test_issue_form_defaults_do_not_answer_required_fields() -> None:
    for filename in ISSUE_FORM_FILES:
        data = _load_yaml(filename)
        body = data.get("body", [])
        for item in body:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"input", "textarea"}:
                validations = item.get("validations") or {}
                if validations.get("required") is True:
                    attrs = item.get("attributes") or {}
                    val = attrs.get("value")
                    assert not val, (
                        f"Required field {item.get('id')!r} in {filename} "
                        f"specifies a non-empty value: {val!r}"
                    )


def test_issue_form_routes_preserve_existing_issue_types() -> None:
    expected_routes = {
        "bug_report.yml": {"prefix": "[BUG] ", "label": "bug"},
        "feature_request.yml": {"prefix": "[FEATURE] ", "label": "enhancement"},
        "documentation_issue.yml": {"prefix": "[DOCS] ", "label": "documentation"},
        "chore.yml": {"prefix": "[CHORE] ", "label": "chore"},
    }
    for filename, route in expected_routes.items():
        data = _load_yaml(filename)
        title = data.get("title", "")
        assert title.startswith(route["prefix"]), (
            f"{filename} title {title!r} does not start with {route['prefix']!r}"
        )
        labels = data.get("labels", [])
        assert route["label"] in labels, (
            f"{filename} labels {labels!r} missing {route['label']!r}"
        )


def test_agent_instructions_match_and_point_to_packet_skill() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    agents_path = repo_root / "AGENTS.md"
    claude_path = repo_root / "CLAUDE.md"
    assert agents_path.read_bytes() == claude_path.read_bytes()

    pointer = (
        "[.agents/skills/issue-packet/SKILL.md](.agents/skills/issue-packet/SKILL.md)"
    )
    agents_text = agents_path.read_text(encoding="utf-8")
    assert pointer in agents_text

    skill_path = repo_root / ".agents" / "skills" / "issue-packet" / "SKILL.md"
    assert skill_path.is_file()


def _packet_skill_text() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    skill_path = repo_root / ".agents" / "skills" / "issue-packet" / "SKILL.md"
    return skill_path.read_text(encoding="utf-8")


def test_packet_skill_frontmatter_and_trigger_branches() -> None:
    content = _packet_skill_text()
    parts = content.split("---", 2)
    assert len(parts) >= 3, "Frontmatter not found between --- markers"
    frontmatter = yaml.safe_load(parts[1])

    assert frontmatter.get("name") == "issue-packet"
    assert frontmatter.get("disable-model-invocation") is not True

    desc = frontmatter.get("description", "")
    assert "Packet intake" not in desc
    for phrase in [
        "Open or file a GitHub issue, bug report, feature request, docs issue, or chore.",
        "Add evidence to an existing issue.",
        "Run gh issue create or gh issue comment.",
    ]:
        assert phrase in desc, f"Missing trigger sentence: {phrase!r}"


def test_packet_skill_requires_search_first_and_consent() -> None:
    content = _packet_skill_text()
    required = [
        "Search this repository's open and closed issues.",
        "Prepare a new issue only after completed searches leave no matching report.",
        "Read the matching `.github/ISSUE_TEMPLATE/*.yml`, including every required id",
        "Build a separate public copy from local evidence.",
        "Ask the human in this session for an explicit yes to this create or comment.",
        "The initial request to report, a prior session's permission, or a CLI flag is not that approval.",
    ]
    for phrase in required:
        assert phrase in content, f"Missing workflow requirement: {phrase!r}"


def test_packet_skill_cli_create_preserves_form_routes() -> None:
    content = _packet_skill_text()
    assert "pass the form's title prefix in `--title`" in content
    assert "the form's label in `--label`" in content

    expected_routes = {
        "bug_report.yml": {"prefix": "[BUG] ", "label": "bug"},
        "feature_request.yml": {"prefix": "[FEATURE] ", "label": "enhancement"},
        "documentation_issue.yml": {"prefix": "[DOCS] ", "label": "documentation"},
        "chore.yml": {"prefix": "[CHORE] ", "label": "chore"},
    }
    for filename, route in expected_routes.items():
        data = _load_yaml(filename)
        title = data.get("title", "")
        assert title.startswith(route["prefix"])
        labels = data.get("labels", [])
        assert route["label"] in labels
        assert route["prefix"] in content, (
            f"Skill missing title prefix {route['prefix']!r} from {filename}"
        )
        assert f"--label {route['label']}" in content, (
            f"Skill missing --label {route['label']} from {filename}"
        )


def test_reporting_workflow_links_do_not_contain_stale_intake_copy() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    readme_path = repo_root / "README.md"
    contributing_path = repo_root / "CONTRIBUTING.md"

    readme_text = readme_path.read_text(encoding="utf-8")
    contributing_text = contributing_path.read_text(encoding="utf-8")

    for text, name in [
        (readme_text, "README.md"),
        (contributing_text, "CONTRIBUTING.md"),
    ]:
        assert "Please [open an issue]" not in text, (
            f"Stale 'Please [open an issue]' found in {name}"
        )
        assert "1. [Open an issue]" not in text, (
            f"Stale '1. [Open an issue]' found in {name}"
        )
        assert "issue-packet/SKILL.md" in text, (
            f"Missing issue-packet/SKILL.md in {name}"
        )


def _maintainer_skill_text(name: str) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / ".agents" / "skills" / name / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_repro_skill_does_not_trigger_on_raw_issue_url() -> None:
    content = _maintainer_skill_text("2-repro-issue")
    parts = content.split("---", 2)
    assert len(parts) >= 3, "Frontmatter not found in repro skill"
    frontmatter = yaml.safe_load(parts[1])
    desc = frontmatter.get("description", "")
    assert "pastes an issue URL" not in desc
    assert "authenticated LinkedIn session" not in desc


def test_maintainer_skills_use_trigger_descriptions() -> None:
    expected = {
        "1-triage-issues": ["Triage backlog", "scan open issues"],
        "2-repro-issue": ["Reproduce #N", "investigate #N"],
        "3-verify-pr-fix": ["Verify PR #N"],
    }
    for name, phrases in expected.items():
        content = _maintainer_skill_text(name)
        parts = content.split("---", 2)
        desc = yaml.safe_load(parts[1]).get("description", "")
        assert "#" in desc or name == "1-triage-issues"
        for phrase in phrases:
            assert phrase in desc, f"{name} description missing {phrase!r}: {desc!r}"


def test_maintainer_skills_evaluate_packet_before_live() -> None:
    triage = _maintainer_skill_text("1-triage-issues")
    assert "## 2. Packet admission" in triage
    assert "Incomplete packets never enter the reproduction shortlist." in triage
    assert "Recommend `needs more info`" in triage
    assert (
        "Do not check out, start the MCP server, run scrapers, apply labels, "
        "comment, close, or assign."
    ) in triage

    repro = _maintainer_skill_text("2-repro-issue")
    assert "A live LinkedIn call is optional." in repro
    assert "ask before login, session changes, or LinkedIn writes." in repro
    assert "/tmp/repro-$NUM-init.json" in repro
    assert "/tmp/repro-$NUM-initialized.json" in repro
    assert """grep -q '"error"' /tmp/repro-$NUM-init.json""" in repro
    assert "Capture and inspect both response bodies before `tools/call`." in repro
    assert "This is harmless." not in repro
    assert "Never mock." not in repro

    verify = _maintainer_skill_text("3-verify-pr-fix")
    assert (
        "Absence of a local baseline limits the live branch, not the whole PR verdict."
    ) in verify
    assert "Do not create fake success or failure files." in verify
    assert "Assumes /2-repro-issue has already captured" not in verify
    assert "Do not check out or call the server." in verify
    assert "Worktree is dirty. Ask before checkout." in verify
    assert "/tmp/verify-pr-$PR-init.json" in verify
    assert """grep -q '"error"' /tmp/verify-pr-$PR-init.json""" in verify


def test_packet_copy_uses_plain_public_text() -> None:
    for filename in ISSUE_FORM_FILES:
        data = _load_yaml(filename)
        _assert_plain_text(data.get("name", ""), f"{filename} name")
        _assert_plain_text(data.get("description", ""), f"{filename} description")
        _assert_plain_text(data.get("title", ""), f"{filename} title")

        for item in data.get("body", []):
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes") or {}
            if "label" in attrs:
                _assert_plain_text(attrs["label"], f"{filename} {item.get('id')} label")
            if "description" in attrs:
                _assert_plain_text(
                    attrs["description"], f"{filename} {item.get('id')} description"
                )
            if "value" in attrs:
                _assert_plain_text(attrs["value"], f"{filename} markdown value")
            if "placeholder" in attrs:
                _assert_plain_text(
                    attrs["placeholder"], f"{filename} {item.get('id')} placeholder"
                )
