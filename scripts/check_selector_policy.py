#!/usr/bin/env python3
"""Refuse selectors that depend on LinkedIn's class names or English labels.

Scans every string constant under ``linkedin_mcp_server/`` except docstrings
and reports four kinds of match:

1. ``bem-class``: a BEM class token such as ``.artdeco-modal__dismiss``,
   LinkedIn's class naming, anywhere in a string.
2. ``class-attribute``: a class attribute selector such as
   ``[class*="listitem__link"]``, with any operator.
3. ``label-value``: an ``aria-label`` or ``title`` value selector such as
   ``[aria-label="Dismiss"]`` or ``[title^=Save]``, with any operator, quoted
   or unquoted. Presence alone (``[aria-label]``) is allowed.
4. ``class-selector``: any ``.class`` token in a literal selector passed
   directly to ``locator``, ``wait_for_selector``, ``query_selector`` or
   ``query_selector_all``, or in a JS ``querySelector``, ``querySelectorAll``,
   ``closest`` or ``matches`` call with a literal argument inside a string.

Rules 1 to 3 judge a value where it is written, so a selector defined in one
module and used in another, or handed from Python to page JS, is checked at
its definition. Rules 2 and 3 report the complete attribute clause, value
included, so an exception names the exact value it allows.

A dependency that is unavoidable is registered in
``scripts/selector_policy_exceptions.toml`` by file, owning symbol, exact text
and count. Any unregistered match, and any registered count that differs
from the tree in either direction, fails the check.

Limits: a non-BEM class token that reaches a selector through a variable,
concatenation or template is not caught; rule 4 sees literal arguments only.
Parsing page text (for example "Page 2 of 5") is outside this policy.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "linkedin_mcp_server"
EXCEPTIONS = Path("scripts/selector_policy_exceptions.toml")

Rule = Literal["bem-class", "class-attribute", "label-value", "class-selector"]

_BEM = re.compile(r"\.[A-Za-z][\w-]*__[\w-]+")
_CLASS_ATTRIBUTE = re.compile(r"\[\s*class\s*[*^$|~]?=", re.IGNORECASE)
_LABEL_VALUE = re.compile(r"\[\s*(?:aria-label|title)\s*[*^$|~]?=", re.IGNORECASE)
# The rest of an attribute clause after its operator: a quoted or bare value,
# an optional case flag, and the closing bracket.
_CLAUSE_TAIL = re.compile(
    r"""\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s\]"']+)\s*(?:[iIsS]\s*)?\]"""
)

_PYTHON_SINKS = frozenset(
    {"locator", "wait_for_selector", "query_selector", "query_selector_all"}
)
# A JS selector call whose single argument is a string literal. A template
# literal counts only without `${`, which would make it an expression.
_JS_SINK = re.compile(
    r"""\b(?:querySelector(?:All)?|closest|matches)\s*\(\s*"""
    r"""(?P<q>['"`])(?P<body>(?:\\.|(?!(?P=q)).)*?)(?P=q)\s*\)""",
    re.DOTALL,
)
_CLASS_TOKEN = re.compile(r"\.-?[A-Za-z_][\w-]*")
# Attribute clauses and quoted strings inside a selector, blanked before
# looking for class tokens so `a[href$=".pdf"]` is not read as a class.
_SELECTOR_NOISE = re.compile(
    r"""\[(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\]"'])*\]"""
    r"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
)

_FIX_HINT = (
    "Prefer URL navigation, innerText, or attribute presence such as "
    '[aria-label] or a[href*="/jobs/view/"] (AGENTS.md, LinkedIn Page Rules). '
    f"If a class or label value is the only signal, register the exact text "
    f"in {EXCEPTIONS} with the reason."
)


@dataclass(frozen=True)
class Match:
    path: str
    line: int
    symbol: str
    rule: Rule
    text: str


@dataclass(frozen=True)
class Registered:
    path: str
    symbol: str
    text: str
    count: int
    reason: str


def _attribute_clause(value: str, start: int, prefix_end: int) -> str:
    """Return the attribute clause opened at ``start``, value included.

    A clause cut short by the end of the constant (an f-string or a
    concatenation) is returned up to that end, so it is still reported.
    """
    tail = _CLAUSE_TAIL.match(value, prefix_end)
    if tail:
        return value[start : tail.end()]
    close = value.find("]", prefix_end)
    return value[start : close + 1 if close != -1 else len(value)]


def _class_tokens(selector: str) -> Iterator[tuple[int, str]]:
    blanked = _SELECTOR_NOISE.sub(lambda m: " " * len(m.group(0)), selector)
    for match in _CLASS_TOKEN.finditer(blanked):
        yield match.start(), selector[match.start() : match.end()]


def _content_matches(value: str) -> Iterator[tuple[int, Rule, str]]:
    for match in _BEM.finditer(value):
        yield match.start(), "bem-class", match.group(0)
    for rule, pattern in (
        ("class-attribute", _CLASS_ATTRIBUTE),
        ("label-value", _LABEL_VALUE),
    ):
        for match in pattern.finditer(value):
            clause = _attribute_clause(value, match.start(), match.end())
            yield match.start(), rule, clause


def _js_sink_matches(value: str) -> Iterator[tuple[int, str]]:
    for call in _JS_SINK.finditer(value):
        body = call.group("body")
        if call.group("q") == "`" and "${" in body:
            continue
        for offset, token in _class_tokens(body):
            yield call.start("body") + offset, token


def _docstrings(tree: ast.Module) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


def _line(node: ast.Constant, value: str, offset: int) -> int:
    # Only a constant whose source lines map one to one onto its newlines
    # (a triple-quoted or single-line literal) can place a match exactly.
    if node.end_lineno is not None and node.end_lineno - node.lineno == value.count(
        "\n"
    ):
        return node.lineno + value.count("\n", 0, offset)
    return node.lineno


def _assigned_name(node: ast.Assign | ast.AnnAssign) -> str:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = [target.id for target in targets if isinstance(target, ast.Name)]
    return names[0] if names else "<module>"


def _literal_selector(node: ast.AST) -> ast.Constant | None:
    """Return the literal first argument of a Playwright selector call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PYTHON_SINKS
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return node.args[0]
    return None


def _owned_nodes(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Yield every node with its owning symbol.

    The owner is the innermost enclosing function or class, else the name of
    the module-level assignment, else ``<module>``.
    """

    def visit(node: ast.AST, owner: str, top: bool) -> Iterator[tuple[str, ast.AST]]:
        yield owner, node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                yield from visit(child, child.name, False)
            elif top and isinstance(child, ast.Assign | ast.AnnAssign):
                yield from visit(child, _assigned_name(child), False)
            else:
                yield from visit(child, owner, top and isinstance(node, ast.Module))

    yield from visit(tree, "<module>", True)


def scan_source(source: str, path: str) -> list[Match]:
    """Return every policy match in one module's source."""
    tree = ast.parse(source, filename=path)
    docstrings = _docstrings(tree)
    matches: list[Match] = []
    for owner, node in _owned_nodes(tree):
        argument = _literal_selector(node)
        if argument is not None and isinstance(argument.value, str):
            selector = argument.value
            covered = {offset for offset, *_ in _content_matches(selector)}
            for offset, token in _class_tokens(selector):
                if offset not in covered:
                    line = _line(argument, selector, offset)
                    matches.append(Match(path, line, owner, "class-selector", token))
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            continue
        value = node.value
        seen: set[int] = set()
        for offset, rule, text in _content_matches(value):
            seen.add(offset)
            matches.append(Match(path, _line(node, value, offset), owner, rule, text))
        for offset, token in _js_sink_matches(value):
            if offset not in seen:
                line = _line(node, value, offset)
                matches.append(Match(path, line, owner, "class-selector", token))
    return sorted(matches, key=lambda m: (m.line, m.rule, m.text))


def scan(root: Path) -> list[Match]:
    """Return every policy match under ``root``'s package directory."""
    matches: list[Match] = []
    for file in sorted((root / PACKAGE).rglob("*.py")):
        path = file.relative_to(root).as_posix()
        matches.extend(scan_source(file.read_text(encoding="utf-8"), path))
    return matches


def load_exceptions(file: Path) -> list[Registered]:
    entries = tomllib.loads(file.read_text(encoding="utf-8")).get("exception", [])
    fields = {"file", "symbol", "text", "count", "reason"}
    loaded: list[Registered] = []
    for index, entry in enumerate(entries):
        if set(entry) != fields:
            raise ValueError(
                f"{file}: exception {index + 1} needs exactly {sorted(fields)}"
            )
        if not isinstance(entry["count"], int) or entry["count"] < 1:
            raise ValueError(f"{file}: exception {index + 1} needs a count >= 1")
        if not str(entry["reason"]).strip():
            raise ValueError(f"{file}: exception {index + 1} needs a reason")
        loaded.append(
            Registered(
                entry["file"],
                entry["symbol"],
                entry["text"],
                entry["count"],
                entry["reason"],
            )
        )
    return loaded


def problems(matches: list[Match], exceptions: list[Registered]) -> list[str]:
    """Return one line per unregistered match or mismatched exception count."""
    found = Counter((m.path, m.symbol, m.text) for m in matches)
    expected: Counter[tuple[str, str, str]] = Counter()
    for entry in exceptions:
        expected[(entry.path, entry.symbol, entry.text)] += entry.count
    reported: list[str] = []
    for match in matches:
        key = (match.path, match.symbol, match.text)
        if key not in expected:
            reported.append(f"{match.path}:{match.line}: {match.rule}: {match.text}")
    for key, count in sorted(expected.items()):
        if found[key] != count:
            path, symbol, text = key
            lines = [m.line for m in matches if (m.path, m.symbol, m.text) == key]
            where = f"{path}:{lines[0]}" if lines else path
            reported.append(
                f"{where}: exception count: {text} in {symbol}: "
                f"registered {count}, found {found[key]}"
            )
    return reported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root: Path = args.root
    try:
        exceptions = load_exceptions(root / EXCEPTIONS)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    reported = problems(scan(root), exceptions)
    if not reported:
        return 0
    sys.stderr.write("\n".join(reported) + "\n\n" + _FIX_HINT + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
