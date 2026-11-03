"""Every tool description in the README table fits one line on GitHub.

GitHub renders a repository README at a fixed 838px wide from a 1280px
viewport up, in its Mona Sans web font, so a row's width is the same on every
platform. The first column is as wide as the longest tool name, and each
description has to fit in what is left.

The widths are a sum of per-character advances measured on the live page
(``tests/fixtures/github-readme-font.json``). Against the real layout that sum
was 0 to 4px wider than the browser's own measurement, never narrower, so a
row this test passes does not wrap.

To remeasure after GitHub changes its font or layout, open the repository page
in Chromium at 1440x900, take the description cell of the tools table, and
record the ``getBoundingClientRect().width`` of each character repeated 20
times in a ``white-space: pre`` span inside it, divided by 20; the code advance
and padding come from a ``<code>`` of 20 and 40 characters, and the table width
from the table itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FONT = json.loads((ROOT / "tests/fixtures/github-readme-font.json").read_text())
HEADER = "| Tool | Description |"
ROW = re.compile(r"^\| `(?P<tool>[a-z_]+)` \| (?P<description>.+) \|$")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
CODE = re.compile(r"`([^`]*)`")


def tool_rows(readme: str) -> list[tuple[str, str]]:
    lines = readme.splitlines()
    start = lines.index(HEADER) + 2
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        match = ROW.match(line)
        assert match, f"Unexpected tools table row: {line}"
        rows.append((match["tool"], match["description"]))
    return rows


def code_width(text: str) -> float:
    return len(text) * FONT["code_advance"] + FONT["code_padding"]


def text_width(markdown: str) -> float:
    """Width of a description cell's rendered text, inline code included."""
    text = LINK.sub(r"\1", markdown)
    width = sum(code_width(code) for code in CODE.findall(text))
    plain = CODE.sub("", text)
    unknown = sorted({char for char in plain if char not in FONT["advances"]})
    assert not unknown, f"No measured width for {unknown}; remeasure the font"
    return width + sum(FONT["advances"][char] for char in plain)


ROWS = tool_rows((ROOT / "README.md").read_text())


def test_the_table_lists_tools():
    assert len(ROWS) > 10


@pytest.mark.parametrize(("tool", "description"), ROWS, ids=[tool for tool, _ in ROWS])
def test_description_fits_one_line(tool: str, description: str):
    name_column = max(code_width(name) for name, _ in ROWS)
    available = (
        FONT["table_width"]
        - FONT["table_border"]
        - 2 * FONT["cell_edges"]
        - name_column
    )
    width = text_width(description)
    assert width <= available, (
        f"{tool}: the description is {width:.0f}px wide and wraps on GitHub; "
        f"shorten it to {available:.0f}px or less"
    )
