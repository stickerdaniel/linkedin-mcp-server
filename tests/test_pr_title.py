"""Regression tests for the Conventional Commit PR-title validator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_pr_title.py"


def _load():
    spec = importlib.util.spec_from_file_location("validate_pr_title", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load()
validate_pr_title = _mod.validate_pr_title
CONVENTIONAL_TYPES = _mod.CONVENTIONAL_TYPES
main = _mod.main


class TestValidatePrTitle:
    @pytest.mark.parametrize(
        "title",
        [
            "feat(server): Add optional minimum tool interval",
            "fix: Cap search pages by remaining budget",
            "docs(messaging): Clarify send_message is compose-oriented",
            "test(daemon): Harden Windows election timing flakes",
            "chore(deps): update dependency patchright to v1.2.3",
            "ci: Add pull request title check",
            "build(deps): bump actions/checkout",
            "feat(api)!: Rename the public tool surface",
            "fix!: Refuse a browser older than the profile",
            "refactor(scraping): Extract search URL grammar",
            "perf(daemon): Reduce election stampede retries",
            "style: Apply ruff-format to interval clamp",
        ],
    )
    def test_accepts_valid_titles(self, title: str):
        assert validate_pr_title(title) is None

    def test_accepts_surrounding_whitespace(self):
        assert validate_pr_title("  feat: Add foo  ") is None

    def test_rejects_empty(self):
        error = validate_pr_title("")
        assert error is not None
        assert "empty" in error.lower()

    def test_rejects_multiline(self):
        error = validate_pr_title("feat: Add foo\nmore")
        assert error is not None
        assert "single line" in error.lower()

    def test_rejects_missing_separator(self):
        error = validate_pr_title("feat Add foo")
        assert error is not None
        assert "': '" in error

    def test_rejects_unknown_type(self):
        error = validate_pr_title("wip: temporary")
        assert error is not None
        assert "Unknown type 'wip'" in error
        for allowed in CONVENTIONAL_TYPES:
            assert allowed in error

    def test_rejects_empty_subject(self):
        error = validate_pr_title("feat:   ")
        assert error is not None
        assert "subject is empty" in error.lower()
        error = validate_pr_title("fix(jobs):")
        assert error is not None
        assert "subject is empty" in error.lower()

    def test_rejects_empty_scope_parens(self):
        error = validate_pr_title("feat(): Add foo")
        assert error is not None
        assert "scope" in error.lower() or "Conventional Commit" in error

    def test_label_workflow_types_are_covered(self):
        """label-pr.yml maps these types; the validator must accept them all."""
        for commit_type in (
            "feat",
            "fix",
            "docs",
            "refactor",
            "chore",
            "ci",
            "style",
            "test",
            "build",
            "perf",
        ):
            assert commit_type in CONVENTIONAL_TYPES
            assert validate_pr_title(f"{commit_type}: Subject") is None


class TestMain:
    def test_main_reads_env(self, monkeypatch, capsys):
        monkeypatch.setenv("PR_TITLE", "feat: Add foo")
        assert main([]) == 0
        assert "PR title ok" in capsys.readouterr().out

    def test_main_fails_on_invalid(self, monkeypatch, capsys):
        monkeypatch.setenv("PR_TITLE", "not a title")
        assert main([]) == 1
        err = capsys.readouterr().err
        assert err
