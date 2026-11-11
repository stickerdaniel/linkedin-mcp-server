"""The selector policy check refuses class, label-value and text selectors.

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
        ("page.locator(selector='.toast')", ("class-selector", ".toast")),
        ("page.wait_for_selector(selector='.toast')", ("class-selector", ".toast")),
        ("page.query_selector(selector='.toast')", ("class-selector", ".toast")),
        ("page.query_selector_all(selector='.toast')", ("class-selector", ".toast")),
        (
            "row.locator(selector_or_locator='.toast')",
            ("class-selector", ".toast"),
        ),
        (
            "X = \"document.querySelector('.toast',)\"",
            ("class-selector", ".toast"),
        ),
        (
            "X = \"document.querySelector(/* selector */ '.toast')\"",
            ("class-selector", ".toast"),
        ),
        (
            "X = \"document.querySelector('.toast' /* selector */)\"",
            ("class-selector", ".toast"),
        ),
        (
            "X = \"document.querySelector(// selector\\n  '.toast',\\n)\"",
            ("class-selector", ".toast"),
        ),
        (
            'JS = r\'document.querySelector("[title=\\"a]b\\"]")\'',
            ("label-value", '[title="a]b"]'),
        ),
        (
            "JS = r'document.querySelector(\".foo__b\\x61r\")'",
            ("bem-class", ".foo__bar"),
        ),
        (
            'JS = r\'const title = "[title=\\"a]b\\"]";\'',
            ("label-value", '[title=\\"a]b\\"]'),
        ),
        ("page.locator('id=main >> div.toast')", ("class-selector", ".toast")),
        ("page.locator('xpath=//main >> css=.toast')", ("class-selector", ".toast")),
        ("page.locator('css:light=.toast')", ("class-selector", ".toast")),
        ("page.locator('id=main >> css:light=.toast')", ("class-selector", ".toast")),
    ],
)
def test_a_class_or_label_value_selector_is_refused(
    source: str, expected: tuple[str, str]
) -> None:
    assert _found(source) == [expected]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("X = 'nav button:has-text(\"Home\")'", ':has-text("Home")'),
        ("X = \"li:text('Save')\"", ":text('Save')"),
        ("X = 'a:text-is(\"Jobs\")'", ':text-is("Jobs")'),
        ("page.locator('text=Sign in')", "text=Sign in"),
        (
            "page.locator('text=Continue on LinkedIn.com')",
            "text=Continue on LinkedIn.com",
        ),
        ("page.locator('main >> \"Sign in\"')", '"Sign in"'),
        ("page.get_by_text('Sign in')", "get_by_text('Sign in')"),
        ("page.get_by_text(text='Sign in', exact=True)", "get_by_text('Sign in')"),
        (
            "page.get_by_role('button', name='Sign in')",
            "get_by_role(name='Sign in')",
        ),
    ],
)
def test_a_text_selector_with_a_written_value_is_refused(
    source: str, expected: str
) -> None:
    assert _found(source) == [("text-selector", expected)]


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
        'JS = r\'document.querySelector("a[href$=\\".pdf\\"]")\'',
        "page.locator('id=foo.bar')",
        "page.locator(r'#foo\\.bar')",
        "page.locator('button /* .toast */')",
    ],
)
def test_presence_href_and_docstrings_are_allowed(source: str) -> None:
    assert _found(source) == []


@pytest.mark.parametrize(
    "source",
    [
        'URL = "https://example.com/search?text=Sign+in"',
        "subprocess.run(['grep', 'text=Sign in'])",
        "page.get_by_text(label)",
        "page.get_by_role('button')",
        "page.get_by_role('button', name=label)",
        "page.locator('button').filter(has_text='Save')",
        "page.locator('button', has_text='Save')",
    ],
)
def test_text_outside_a_selector_or_from_a_variable_is_allowed(source: str) -> None:
    assert _found(source) == []


def test_a_bem_prefix_does_not_hide_the_complete_escaped_class() -> None:
    assert ("class-selector", ".foo__b\\61r") in _found("page.locator(r'.foo__b\\61r')")


@pytest.mark.parametrize(
    ("template", "before", "after"),
    [
        ('document.querySelector("[title=\\"{}\\"]")', "a]b", "a]c"),
        ('document.querySelector("[title=\\x22{}\\x22]")', "a]b", "a]c"),
        ('document.querySelector("[title=\\u0022{}\\u0022]")', "a]b", "a]c"),
        ('document.querySelector(".foo__b{}")', "\\x61r", "\\x75z"),
    ],
)
def test_an_exception_pins_the_decoded_js_selector(
    template: str, before: str, after: str
) -> None:
    def scan(value: str) -> list:
        return _CHECK.scan_source(f"JS = r'{template.format(value)}'", "snippet.py")

    registered = [
        _CHECK.Registered(m.path, m.symbol, m.text, 1, "test") for m in scan(before)
    ]
    assert registered
    assert _CHECK.problems(scan(before), registered) == []
    assert _CHECK.problems(scan(after), registered) != []


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


def test_a_substituted_text_selector_value_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _plant(
        tree,
        "linkedin_mcp_server/core/auth.py",
        'nav button:has-text("Home")',
        'nav button:has-text("Start")',
    )
    report = _report(tree, capsys)
    assert 'text-selector: :has-text("Start")' in report
    assert ':has-text("Home") in is_logged_in: registered 1, found 0' in report


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
