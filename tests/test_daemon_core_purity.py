"""The daemon's decision cores stay free of I/O, and ruff is what says so.

Each case hands ruff a source on stdin under a file name inside or outside
``linkedin_mcp_server/daemon/core``, so the directory's own ``ruff.toml`` is
found the way ``ruff check .`` finds it, and nothing is written anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / "linkedin_mcp_server" / "daemon"
IN_THE_CORE = DAEMON / "core" / "_probe.py"
BESIDE_IT = DAEMON / "_probe.py"


def _findings(source: str, filename: Path) -> dict[int, set[str]]:
    """The rule codes ruff reports for *source*, by line."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "json",
            "--stdin-filename",
            str(filename),
            "-",
        ],
        input=source,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert completed.returncode in (0, 1), completed.stderr
    found: dict[int, set[str]] = {}
    for finding in json.loads(completed.stdout):
        found.setdefault(finding["location"]["row"], set()).add(finding["code"])
    return found


_FORBIDDEN = [
    "import asyncio",
    "import os",
    "import subprocess",
    "import socket",
    "import time",
    "from time import monotonic",
    "import uuid",
    "from pathlib import Path",
    "import httpx",
    "import httpx2",
    "from builtins import open",
    "from io import open",
]


def _adapters() -> list[str]:
    """Every daemon module outside the core, read off the package itself."""
    return sorted(path.stem for path in DAEMON.glob("*.py") if path.stem != "__init__")


@pytest.mark.parametrize("line", _FORBIDDEN)
def test_the_core_refuses_io_time_and_identifiers(line: str):
    source = f"{line}  # noqa: F401\n"
    assert "TID251" in _findings(source, IN_THE_CORE).get(1, set())


def test_the_core_refuses_the_open_builtin():
    assert "PTH123" in _findings('open("x")\n', IN_THE_CORE).get(1, set())


@pytest.mark.parametrize("module", _adapters())
@pytest.mark.parametrize(
    "spelling",
    [
        "from linkedin_mcp_server.daemon import {module}",
        "import linkedin_mcp_server.daemon.{module}",
        "from .. import {module}",
    ],
)
def test_the_core_refuses_every_adapter_module(module: str, spelling: str):
    """A module added beside the core fails here until the ban names it too."""
    source = spelling.format(module=module) + "  # noqa: F401\n"
    assert "TID251" in _findings(source, IN_THE_CORE).get(1, set())


def test_the_cores_may_import_each_other():
    source = "from linkedin_mcp_server.daemon.core import call  # noqa: F401\n"
    assert _findings(source, IN_THE_CORE) == {}


def test_the_core_is_linted_like_the_rest_of_the_package():
    """A ruff.toml without ``extend`` would replace the root selection, silently.

    The probes are chosen so that ruff's own default selection disagrees with
    the root on both sides: E731 is selected by the root and not by default,
    and BLE001 the other way round.
    """
    source = "f = lambda: 0\ntry:\n    f()\nexcept Exception:\n    raise\n"
    in_the_core = _findings(source, IN_THE_CORE)
    assert in_the_core == _findings(source, BESIDE_IT)
    assert "E731" in in_the_core.get(1, set())


def test_the_ban_stops_at_the_core():
    source = "import asyncio  # noqa: F401\nopen('x')\n"
    assert _findings(source, BESIDE_IT) == {}
