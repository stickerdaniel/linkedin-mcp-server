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


def test_packet_skill_frontmatter_and_trigger_branches() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    skill_path = repo_root / ".agents" / "skills" / "issue-packet" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    assert len(parts) >= 3, "Frontmatter not found between --- markers"
    frontmatter = yaml.safe_load(parts[1])

    assert frontmatter.get("name") == "issue-packet"
    assert frontmatter.get("disable-model-invocation") is not True

    desc = frontmatter.get("description", "")
    for keyword in [
        "file",
        "open",
        "create",
        "bug report",
        "feature request",
        "gh issue create",
    ]:
        assert keyword in desc, (
            f"Keyword {keyword!r} not in frontmatter description: {desc!r}"
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


def test_repro_skill_does_not_trigger_on_raw_issue_url() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    repro_path = repo_root / ".agents" / "skills" / "2-repro-issue" / "SKILL.md"
    content = repro_path.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    assert len(parts) >= 3, "Frontmatter not found in repro skill"
    frontmatter = yaml.safe_load(parts[1])
    desc = frontmatter.get("description", "")
    assert "pastes an issue URL" not in desc


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
