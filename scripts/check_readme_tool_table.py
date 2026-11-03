"""Check that every tool description in the README table fits one line on GitHub.

GitHub renders a repository README at a fixed 838px wide from a 1280px
viewport up, in its Mona Sans web font, so a row's width is the same on every
platform. The first column is as wide as the longest tool name, and each
description has to fit in what is left.

The widths are a sum of per-character advances measured on the live page
(``scripts/github-readme-font.json``). Against the real layout that sum was 0
to 4px wider than the browser's own measurement, never narrower, so a row this
check passes does not wrap.

To remeasure after GitHub changes its font or layout, open the repository page
in Chromium at 1440x900, take the description cell of the tools table, and
record the ``getBoundingClientRect().width`` of each character repeated 20
times in a ``white-space: pre`` span inside it, divided by 20; the code advance
and padding come from a ``<code>`` of 20 and 40 characters, and the table width
from the table itself.

Usage: ``python3 scripts/check_readme_tool_table.py [README.md]``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT = json.loads(
    (ROOT / "scripts/github-readme-font.json").read_text(encoding="utf-8")
)
HEADER = "| Tool | Description |"
ROW = re.compile(r"^\| `(?P<tool>[a-z_]+)` \| (?P<description>.+) \|$")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
CODE = re.compile(r"`([^`]*)`")


def tool_rows(readme: str) -> list[tuple[str, str]]:
    """Return ``(tool, description)`` for each row of the tools table."""
    lines = readme.splitlines()
    rows = []
    for line in lines[lines.index(HEADER) + 2 :]:
        if not line.startswith("|"):
            break
        match = ROW.match(line)
        if not match:
            raise ValueError(f"Unexpected tools table row: {line}")
        rows.append((match["tool"], match["description"]))
    return rows


def code_width(text: str) -> float:
    return len(text) * FONT["code_advance"] + FONT["code_padding"]


def text_width(markdown: str) -> float:
    """Width of a description cell's rendered text, inline code included."""
    text = LINK.sub(r"\1", markdown)
    plain = CODE.sub("", text)
    unknown = sorted({char for char in plain if char not in FONT["advances"]})
    if unknown:
        raise ValueError(f"No measured width for {unknown}; remeasure the font")
    return sum(code_width(code) for code in CODE.findall(text)) + sum(
        FONT["advances"][char] for char in plain
    )


def wrapping_rows(readme: str) -> list[str]:
    """One message per description that would wrap on GitHub."""
    rows = tool_rows(readme)
    name_column = max(code_width(name) for name, _ in rows)
    available = (
        FONT["table_width"]
        - FONT["table_border"]
        - 2 * FONT["cell_edges"]
        - name_column
    )
    return [
        f"{tool}: the description is {width:.0f}px wide and wraps on GitHub; "
        f"shorten it to {available:.0f}px or less"
        for tool, description in rows
        if (width := text_width(description)) > available
    ]


def main(argv: list[str]) -> int:
    readme = Path(argv[0]) if argv else ROOT / "README.md"
    problems = wrapping_rows(readme.read_text(encoding="utf-8"))
    for problem in problems:
        print(f"::error file=README.md::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
