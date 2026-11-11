#!/usr/bin/env python3
"""Refuse selectors that depend on LinkedIn's class names or English labels.

Scans every string constant under ``linkedin_mcp_server/`` except docstrings
and reports five kinds of match:

1. ``bem-class``: a BEM class token such as ``.artdeco-modal__dismiss``,
   LinkedIn's class naming, anywhere in a string.
2. ``class-attribute``: a class attribute selector such as
   ``[class*="listitem__link"]``, with any operator.
3. ``label-value``: an ``aria-label`` or ``title`` value selector such as
   ``[aria-label="Dismiss"]`` or ``[title^=Save]``, with any operator, quoted
   or unquoted. Presence alone (``[aria-label]``) is allowed.
4. ``class-selector``: any ``.class`` token in a literal selector passed
   directly to ``locator``, ``wait_for_selector``, ``query_selector`` or
   ``query_selector_all``, positionally or by keyword, or in a JS
   ``querySelector``, ``querySelectorAll``, ``closest`` or ``matches`` call
   whose one argument is a literal inside a string. Only CSS counts: a
   Playwright selector is split at ``>>``, its ``id=``, ``text=``, ``xpath=``
   and other engine parts are skipped, and so are CSS comments, escapes
   (``#foo\\.bar``) and quoted values. A JS literal is read after its own
   escapes, so ``a[href$=\\".pdf\\"]`` there is not a class.
5. ``text-selector``: a Playwright text selector with a written value: the
   ``:has-text(...)``, ``:text(...)`` and ``:text-is(...)`` pseudo-classes
   anywhere in a string, a ``text=`` or quoted part of a literal selector
   passed to the methods in rule 4, and ``get_by_text`` or
   ``get_by_role(..., name=...)`` with a literal. ``has_text`` filters, as in
   ``.filter(has_text=...)`` or ``locator(..., has_text=...)``, are outside
   this rule.

Rules 1 to 3 and the pseudo-classes of rule 5 judge a value where it is
written, so a selector defined in one module and used in another, or handed
from Python to page JS, is checked at its definition. Rules 2, 3 and 5 report
the complete clause, value included, so an exception names the exact value
it allows. A value quoted from inside a JS string, such as
``[title=\\"a]b\\"]``, keeps its escaped quotes and every ``]`` between them.

A dependency that is unavoidable is registered in
``scripts/selector_policy_exceptions.toml`` by file, owning symbol, exact text
and count. Any unregistered match, and any registered count that differs
from the tree in either direction, fails the check.

Limits: a non-BEM class token or a text engine part that reaches a selector
through a variable, concatenation or template is not caught; rules 4 and 5
see literal arguments only. Parsing page text (for example "Page 2 of 5") is
outside this policy.
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

Rule = Literal[
    "bem-class", "class-attribute", "label-value", "class-selector", "text-selector"
]

_BEM = re.compile(r"\.[A-Za-z][\w-]*__[\w-]+")
_CLASS_ATTRIBUTE = re.compile(r"\[\s*class\s*[*^$|~]?=", re.IGNORECASE)
_LABEL_VALUE = re.compile(r"\[\s*(?:aria-label|title)\s*[*^$|~]?=", re.IGNORECASE)
_TEXT_PSEUDO = re.compile(r":(?:has-text|text-is|text)\(")
# A quoted CSS string, plain or as written inside a JS string literal, where
# \"a]b\" is the CSS string "a]b" and a doubled backslash is a CSS escape.
_QUOTED = (
    r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
    r"""|\\"(?:\\\\(?:\\.|[^\\])|\\[^"\\]|[^"\\])*\\\""""
    r"""|\\'(?:\\\\(?:\\.|[^\\])|\\[^'\\]|[^'\\])*\\')"""
)
# The rest of an attribute clause after its operator: a quoted or bare value,
# an optional case flag, and the closing bracket.
_CLAUSE_TAIL = re.compile(rf"""\s*(?:{_QUOTED}|[^\s\]"']+)\s*(?:[iIsS]\s*)?\]""")
# The rest of a text pseudo-class after its parenthesis.
_PSEUDO_TAIL = re.compile(rf"""\s*(?:{_QUOTED}|[^)"']*)\s*\)""")

_PYTHON_SINKS = frozenset(
    {"locator", "wait_for_selector", "query_selector", "query_selector_all"}
)
# `Locator.locator` names its selector parameter differently from the others.
_SELECTOR_KEYWORDS = frozenset({"selector", "selector_or_locator"})
# Whitespace and comments around a JS call argument.
_JS_TRIVIA = r"(?:\s|/\*.*?\*/|//[^\n]*\n)*"
# A JS selector call whose one argument is a string literal, with an optional
# trailing comma. A template literal counts only without `${`, which would
# make it an expression.
_JS_SINK = re.compile(
    rf"""\b(?:querySelector(?:All)?|closest|matches)\s*\({_JS_TRIVIA}"""
    r"""(?P<q>['"`])(?P<body>(?:\\.|(?!(?P=q)).)*?)(?P=q)"""
    rf"""{_JS_TRIVIA}(?:,{_JS_TRIVIA})?\)""",
    re.DOTALL,
)
_JS_ESCAPE = re.compile(
    r"\\(?:u\{(?P<braced>[0-9A-Fa-f]{1,6})\}|u(?P<u>[0-9A-Fa-f]{4})"
    r"|x(?P<x>[0-9A-Fa-f]{2})|(?P<char>\r\n|[\s\S]))"
)
_JS_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    # A backslash before a line break continues the line.
    "\n": "",
    "\r": "",
    "\r\n": "",
    "\u2028": "",
    "\u2029": "",
}
_CSS_ESCAPE = r"\\(?:[0-9A-Fa-f]{1,6}\s?|[^\n0-9A-Fa-f])"
_CSS_IDENT = rf"-?(?:[A-Za-z_]|{_CSS_ESCAPE})(?:[\w-]|{_CSS_ESCAPE})*"
# Read left to right: comments, quoted strings, attribute clauses, IDs and
# escapes are consumed whole, so a dot inside one of them is never a class.
_CSS_TOKEN = re.compile(
    r"""/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'"""
    r"""|\[(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\]"'])*\]"""
    rf"""|#{_CSS_IDENT}|{_CSS_ESCAPE}|(?P<cls>\.{_CSS_IDENT})""",
    re.DOTALL,
)
# What Playwright reads as an engine name before the first `=` of a part.
_ENGINE_NAME = re.compile(r"[A-Za-z_0-9+:*-]+")
_XPATH = re.compile(r"\(*//|\.\.")

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


def _clause(value: str, start: int, prefix_end: int, tail: re.Pattern[str]) -> str:
    """Return the clause opened at ``start``, value included.

    ``tail`` matches the value and the closing bracket or parenthesis. A
    clause cut short by the end of the constant (an f-string or a
    concatenation) is returned up to the first closing character or that end,
    so it is still reported.
    """
    found = tail.match(value, prefix_end)
    if found:
        return value[start : found.end()]
    close = value.find("]" if value[start] == "[" else ")", prefix_end)
    return value[start : close + 1 if close != -1 else len(value)]


def _css_class_tokens(css: str) -> Iterator[tuple[int, str]]:
    for token in _CSS_TOKEN.finditer(css):
        if token.group("cls"):
            yield token.start(), token.group("cls")


def _selector_parts(selector: str) -> Iterator[tuple[int, str]]:
    """Yield each part of a Playwright selector chain with its offset.

    Splits at ``>>`` outside quotes, as Playwright's ``parseSelectorString``
    does.
    """
    start = index = 0
    quote = ""
    while index < len(selector):
        char = selector[index]
        if char == "\\":
            index += 1
        elif char == quote:
            quote = ""
        elif not quote and char in "\"'`":
            quote = char
        elif not quote and selector.startswith(">>", index):
            yield start, selector[start:index]
            start = index + 2
            index += 1
        index += 1
    yield start, selector[start:]


def _selector_matches(selector: str) -> Iterator[tuple[int, Rule, str]]:
    """Rule 4 and the text engine of rule 5 in a Playwright selector.

    Each part is classified as Playwright does: ``name=body`` when the text
    before the first ``=`` is an engine name, a quoted part as ``text``, a
    leading ``//`` or ``..`` as ``xpath``, and anything else as CSS.
    """
    for start, raw in _selector_parts(selector):
        part = raw.strip()
        offset = start + len(raw) - len(raw.lstrip())
        name, equals, body = part.partition("=")
        if equals and _ENGINE_NAME.fullmatch(name.strip()):
            engine = name.strip().lstrip("*")
            body_offset = offset + len(name) + 1
        elif len(part) > 1 and part[0] in "\"'" and part[-1] == part[0]:
            engine, body, body_offset = "text", part, offset
        elif _XPATH.match(part):
            engine, body, body_offset = "xpath", part, offset
        else:
            engine, body, body_offset = "css", part, offset
        if engine == "text":
            yield offset, "text-selector", part
        elif engine == "css":
            for token_offset, token in _css_class_tokens(body):
                yield body_offset + token_offset, "class-selector", token


def _content_matches(value: str) -> Iterator[tuple[int, Rule, str]]:
    for match in _BEM.finditer(value):
        yield match.start(), "bem-class", match.group(0)
    for rule, pattern, tail in (
        ("class-attribute", _CLASS_ATTRIBUTE, _CLAUSE_TAIL),
        ("label-value", _LABEL_VALUE, _CLAUSE_TAIL),
        ("text-selector", _TEXT_PSEUDO, _PSEUDO_TAIL),
    ):
        for match in pattern.finditer(value):
            clause = _clause(value, match.start(), match.end(), tail)
            yield match.start(), rule, clause


def _js_string(body: str) -> tuple[str, list[int]]:
    """Decode a JS string literal's body, with each character's offset in it."""
    text: list[str] = []
    offsets: list[int] = []
    position = 0
    for escape in _JS_ESCAPE.finditer(body):
        text += body[position : escape.start()]
        offsets += range(position, escape.start())
        code = escape["braced"] or escape["u"] or escape["x"]
        if code:
            decoded = chr(min(int(code, 16), sys.maxunicode))
        else:
            decoded = _JS_SIMPLE_ESCAPES.get(escape["char"], escape["char"])
        text += decoded
        offsets += [escape.start()] * len(decoded)
        position = escape.end()
    text += body[position:]
    offsets += range(position, len(body))
    return "".join(text), offsets


def _js_sink_matches(value: str) -> Iterator[tuple[int, str]]:
    for call in _JS_SINK.finditer(value):
        body = call.group("body")
        if call.group("q") == "`" and "${" in body:
            continue
        css, offsets = _js_string(body)
        for offset, token in _css_class_tokens(css):
            yield call.start("body") + offsets[offset], token


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


def _literal(node: ast.expr | None) -> ast.Constant | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node
    return None


def _literal_argument(call: ast.Call, keywords: frozenset[str]) -> ast.Constant | None:
    """Return the literal first argument, passed positionally or by keyword."""
    if call.args:
        return _literal(call.args[0])
    return _literal(next((k.value for k in call.keywords if k.arg in keywords), None))


def _literal_selector(node: ast.AST) -> ast.Constant | None:
    """Return the literal selector of a Playwright selector call."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PYTHON_SINKS
    ):
        return _literal_argument(node, _SELECTOR_KEYWORDS)
    return None


def _literal_text_locator(node: ast.AST) -> tuple[ast.Constant, str] | None:
    """Return the literal of ``get_by_text`` or ``get_by_role(name=)``.

    The text reported is the call reduced to that literal, such as
    ``get_by_role(name='Home')``.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    if node.func.attr == "get_by_text":
        text = _literal_argument(node, frozenset({"text"}))
        return (text, f"get_by_text({text.value!r})") if text else None
    if node.func.attr == "get_by_role":
        name = _literal(next((k.value for k in node.keywords if k.arg == "name"), None))
        return (name, f"get_by_role(name={name.value!r})") if name else None
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
        if argument is not None and isinstance(selector := argument.value, str):
            covered = {offset for offset, *_ in _content_matches(selector)}
            for offset, rule, text in _selector_matches(selector):
                if offset not in covered:
                    line = _line(argument, selector, offset)
                    matches.append(Match(path, line, owner, rule, text))
        if located := _literal_text_locator(node):
            literal, text = located
            matches.append(Match(path, literal.lineno, owner, "text-selector", text))
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
