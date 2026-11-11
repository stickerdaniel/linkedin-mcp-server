"""The selector policy check refuses class and label-value selectors.

Snippets pin each rule; planted faults run the real check against a copy of
the package, so a change to a registered dependency fails it.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "check_selector_policy", _REPO_ROOT / "scripts" / "check_selector_policy.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_CHECK = importlib.util.module_from_spec(_SPEC)
# Dataclasses resolve their module through sys.modules.
sys.modules[_SPEC.name] = _CHECK
_SPEC.loader.exec_module(_CHECK)


def _found(source: str) -> list[tuple[str, str]]:
    return [(m.rule, m.text) for m in _CHECK.scan_source(source, "snippet.py")]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('X = "div.jobs-card__title"', ("bem-class", ".jobs-card__title")),
        (
            "X = 'div[class*=\"listitem__link\"]'",
            ("class-attribute", '[class*="listitem__link"]'),
        ),
        ('X = "li[class ~= active]"', ("class-attribute", "[class ~= active]")),
        (
            "X = 'button[aria-label=\"Close\"]'",
            ("label-value", '[aria-label="Close"]'),
        ),
        ('X = "button[aria-label=Close]"', ("label-value", "[aria-label=Close]")),
        ('X = "a[title^=Save]"', ("label-value", "[title^=Save]")),
        (
            "X = 'button[ aria-label = \"Close\" i ]'",
            ("label-value", '[ aria-label = "Close" i ]'),
        ),
        ('X = f"[title*={name}]"', ("label-value", "[title*=")),
        ('page.locator(".toast")', ("class-selector", ".toast")),
        ('page.wait_for_selector("ul li.active")', ("class-selector", ".active")),
        (
            "X = \"document.querySelector('.feed-row')\"",
            ("class-selector", ".feed-row"),
        ),
        ('X = "el.closest(`.row`)"', ("class-selector", ".row")),
    ],
)
def test_a_class_or_label_value_selector_is_refused(
    source: str, expected: tuple[str, str]
) -> None:
    assert _found(source) == [expected]


@pytest.mark.parametrize(
    "source",
    [
        'X = "button[aria-label]"',
        'X = "a[title]"',
        "X = 'a[href*=\"/jobs/view/\"]'",
        'X = \'[aria-labelledby="x"], [data-title="y"]\'',
        "page.locator('a[href$=\".pdf\"]')",
        "X = \"document.querySelectorAll('main li label[aria-label]')\"",
        'def f():\n    """Reads .artdeco-modal__content and [class*=x]."""\n',
    ],
)
def test_presence_href_and_docstrings_are_allowed(source: str) -> None:
    assert _found(source) == []


def test_the_owner_is_the_innermost_symbol() -> None:
    source = (
        'TOP = "div.a__b"\n'
        "class Page:\n"
        "    def read(self):\n"
        "        page.locator('.toast')\n"
    )
    owners = [(m.symbol, m.text) for m in _CHECK.scan_source(source, "snippet.py")]
    assert owners == [("TOP", ".a__b"), ("read", ".toast")]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the package and its exceptions that passes the check."""
    shutil.copytree(
        _REPO_ROOT / "linkedin_mcp_server",
        tmp_path / "linkedin_mcp_server",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tmp_path / "scripts").mkdir()
    shutil.copy(
        _REPO_ROOT / "scripts" / "selector_policy_exceptions.toml",
        tmp_path / "scripts",
    )
    assert _CHECK.main(["--root", str(tmp_path)]) == 0
    return tmp_path


def _plant(root: Path, path: str, old: str, new: str) -> None:
    file = root / path
    source = file.read_text(encoding="utf-8")
    assert source.count(old) == 1
    file.write_text(source.replace(old, new), encoding="utf-8")


def _report(root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert _CHECK.main(["--root", str(root)]) == 1
    return capsys.readouterr().err


def test_a_class_in_a_shared_selector_constant_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(
        tree,
        "linkedin_mcp_server/core/utils.py",
        """_JOB_CARD_SELECTOR = 'a[href*="/jobs/view/"], [componentkey^="job-card-component-ref-"]'""",
        '_JOB_CARD_SELECTOR = "div.job-card-container__link"',
    )
    assert "core/utils.py:26: bem-class: .job-card-container__link" in _report(
        tree, capsys
    )


@pytest.mark.parametrize(
    ("added", "expected"),
    [
        ("'.msg-form__footer'", "bem-class: .msg-form__footer"),
        (
            "'.msg-form__send-toggle'",
            ".msg-form__send-toggle in _MESSAGE_COMPOSER_INSPECT_JS: "
            "registered 1, found 2",
        ),
    ],
)
def test_a_second_selector_in_an_exempted_symbol_fails(
    tree: Path, capsys: pytest.CaptureFixture[str], added: str, expected: str
) -> None:
    _plant(
        tree,
        "linkedin_mcp_server/linkedin/message_sender.py",
        "Array.from(scope.querySelectorAll('.msg-form__send-toggle'))",
        f"Array.from(scope.querySelectorAll({added}))"
        ".concat(Array.from(scope.querySelectorAll('.msg-form__send-toggle')))",
    )
    assert expected in _report(tree, capsys)


def test_a_substituted_label_value_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(
        tree,
        "linkedin_mcp_server/core/utils.py",
        'button[aria-label="Dismiss"]',
        'button[aria-label="Unreviewed"]',
    )
    report = _report(tree, capsys)
    assert 'label-value: [aria-label="Unreviewed"]' in report
    assert '[aria-label="Dismiss"] in handle_modal_close: registered 1, found 0' in (
        report
    )


def test_a_removed_occurrence_fails_as_a_stale_count(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(
        tree,
        "linkedin_mcp_server/linkedin/capture.py",
        "(dialog[open] or .artdeco-modal__content)",
        "(dialog[open])",
    )
    assert (
        ".artdeco-modal__content in _extract_overlay_content: registered 3, found 2"
        in _report(tree, capsys)
    )
