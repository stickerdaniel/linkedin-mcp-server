#!/usr/bin/env python3
"""Write the version from pyproject.toml into every file that repeats it.

Run after ``uv version --bump``, so the bump pull request carries the synced
files and the merged commit is the one the release publishes and tags.
``--check`` writes nothing and exits 1 naming every file that disagrees with
``pyproject.toml``, ``uv.lock`` included. CI runs it on every pull request and
the release workflow runs it again before anything is built or published.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_IMAGE = "stickerdaniel/linkedin-mcp-server"
# Stop at a quote as well as a space. `[^ ]*` swallowed the closing quote of a
# quoted image reference, which YAML then rejects. Applied once per line, as
# sed applies `s` without `g`.
_IMAGE_REFERENCE = re.compile(rf"{re.escape(_IMAGE)}:[^ \"'\n]*")
_IMAGE_KEY = re.compile(r"""image:\s*(["']?)([^\s"'#]*)\1\s*(?:#.*)?""")
# What a damaged file raises on the way in: unreadable, not JSON or TOML, a
# missing key, or a value of the wrong shape.
_MALFORMED = (OSError, ValueError, KeyError, TypeError)


def _json(text: str) -> dict[str, Any]:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise TypeError("not a JSON object")
    return document


# The form every JSON file here is checked in with, so a sync changes only the
# version and never reformats the file around it.
def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _tag(reference: str) -> str:
    return reference.rpartition(":")[2]


def _read_version(text: str) -> list[str]:
    return [_json(text)["version"]]


def _write_version(text: str, version: str) -> str:
    document = _json(text)
    document["version"] = version
    return _dump(document)


def _server_packages(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Each package, refusing a registry type this script cannot version."""
    for package in document["packages"]:
        if package["registryType"] not in {"pypi", "oci"}:
            raise ValueError(
                f"unhandled registryType {package['registryType']!r}: "
                "teach scripts/prepare_release.py how to version it"
            )
        yield package


# server.json carries the version in three places, one of them inside an image
# reference.
def _read_server(text: str) -> list[str]:
    document = _json(text)
    return [document["version"]] + [
        package["version"]
        if package["registryType"] == "pypi"
        else _tag(package["identifier"])
        for package in _server_packages(document)
    ]


def _write_server(text: str, version: str) -> str:
    document = _json(text)
    document["version"] = version
    for package in _server_packages(document):
        if package["registryType"] == "pypi":
            package["version"] = version
        else:
            repository, _, _ = package["identifier"].rpartition(":")
            package["identifier"] = f"{repository}:{version}"
    return _dump(document)


def _read_compose(text: str) -> list[str]:
    """Tags of the image every live service names from this repository.

    Reads the structure rather than matching the text: a commented-out image
    reference matches any pattern over the source, and blessed a compose file
    whose live service named something else entirely. A key is an `image:` only
    at the indentation of the first key under its service, which also skips a
    block scalar that happens to contain one. `docker.io/` is the same
    repository written out, and compose accepts both spellings.
    """
    tags: list[str] = []
    in_services = False
    service_indent = key_indent = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services = stripped.partition("#")[0].rstrip() == "services:"
            service_indent = key_indent = None
        elif not in_services:
            continue
        elif service_indent is None or indent <= service_indent:
            service_indent, key_indent = indent, None
        elif key_indent is None or indent == key_indent:
            key_indent = indent
            image = _IMAGE_KEY.fullmatch(stripped)
            if image is None:
                continue
            repository, _, tag = image[2].removeprefix("docker.io/").rpartition(":")
            if repository == _IMAGE:
                tags.append(tag)
    return tags


def _write_compose(text: str, version: str) -> str:
    return "".join(
        _IMAGE_REFERENCE.sub(f"{_IMAGE}:{version}", line, count=1)
        for line in text.splitlines(keepends=True)
    )


@dataclass(frozen=True)
class _Versioned:
    """A file that repeats the version, and how to read and rewrite it.

    `write` is None for a file another tool owns: `uv version --bump` updates
    uv.lock, and this script only checks it.
    """

    path: str
    read: Callable[[str], list[str]]
    write: Callable[[str, str], str] | None = None


def _versioned(project_name: str) -> tuple[_Versioned, ...]:
    def read_lock(text: str) -> list[str]:
        return [
            package["version"]
            for package in tomllib.loads(text).get("package", [])
            if package.get("name") == project_name
        ]

    # Only the plugin manifests carry the release. The shared .mcp.json runs
    # mcp-server-linkedin@latest, so it has no version to rewrite.
    return (
        _Versioned("manifest.json", _read_version, _write_version),
        _Versioned("docker-compose.yml", _read_compose, _write_compose),
        _Versioned(".github/mcp/server.json", _read_server, _write_server),
        _Versioned(
            "plugins/linkedin-mcp-server/.codex-plugin/plugin.json",
            _read_version,
            _write_version,
        ),
        _Versioned(
            "plugins/linkedin-mcp-server/.claude-plugin/plugin.json",
            _read_version,
            _write_version,
        ),
        _Versioned(
            "plugins/linkedin-mcp-server/.cursor-plugin/plugin.json",
            _read_version,
            _write_version,
        ),
        _Versioned("uv.lock", read_lock),
    )


def _describe(error: Exception) -> str:
    return f"missing key {error}" if isinstance(error, KeyError) else str(error)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _sync(files: tuple[_Versioned, ...], version: str) -> list[str]:
    """Rewrite every writable file, or none of them if any is malformed."""
    rewritten: dict[str, str] = {}
    problems: list[str] = []
    for file in files:
        if file.write is None:
            continue
        try:
            rewritten[file.path] = file.write(_read(file.path), version)
        except _MALFORMED as error:
            problems.append(f"{file.path}: {_describe(error)}")
    if problems:
        return problems
    for path, text in rewritten.items():
        if text != _read(path):
            (ROOT / path).write_text(text, encoding="utf-8")
            print(f"Updated {path}")
    return []


def _disagreements(files: tuple[_Versioned, ...], version: str) -> list[str]:
    problems: list[str] = []
    for file in files:
        try:
            found = file.read(_read(file.path))
        except _MALFORMED as error:
            problems.append(f"{file.path}: {_describe(error)}")
            continue
        if not found:
            problems.append(f"{file.path}: no version found")
            continue
        stale = [repr(value) for value in dict.fromkeys(found) if value != version]
        if stale:
            problems.append(f"{file.path}: {', '.join(stale)}, expected {version!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 if any file disagrees with pyproject.toml",
    )
    args = parser.parse_args(argv)

    project = tomllib.loads(_read("pyproject.toml"))["project"]
    version = project["version"]
    files = _versioned(project["name"])

    # A sync ends with the same check, because a text rewrite reports success
    # whatever it matched.
    problems = [] if args.check else _sync(files, version)
    problems = problems or _disagreements(files, version)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(
            "Run `uv lock` and `uv run scripts/prepare_release.py`, then commit "
            "the result.",
            file=sys.stderr,
        )
        return 1
    print(f"Every versioned file reads {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
