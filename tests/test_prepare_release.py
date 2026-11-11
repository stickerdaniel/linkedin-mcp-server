"""Behaviour of ``scripts/prepare_release.py`` on a copy of the repository's files."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DERIVED = (
    "manifest.json",
    "docker-compose.yml",
    ".github/mcp/server.json",
    "plugins/linkedin-mcp-server/.codex-plugin/plugin.json",
    "plugins/linkedin-mcp-server/.claude-plugin/plugin.json",
    "plugins/linkedin-mcp-server/.cursor-plugin/plugin.json",
)
_NEW = "9.8.7"
_CURRENT = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """The script and every file it reads, at their repository paths."""
    for path in ("scripts/prepare_release.py", "pyproject.toml", "uv.lock", *_DERIVED):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO_ROOT / path, target)
    return tmp_path


def _run(tree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "prepare_release.py"), *args],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _bump(tree: Path) -> None:
    """What `uv version --bump` changes: pyproject.toml and the lock's own entry."""
    _replace(tree / "pyproject.toml", f'version = "{_CURRENT}"', f'version = "{_NEW}"')
    _replace(
        tree / "uv.lock",
        f'name = "mcp-server-linkedin"\nversion = "{_CURRENT}"',
        f'name = "mcp-server-linkedin"\nversion = "{_NEW}"',
    )


def test_sync_writes_the_version_into_every_derived_file(tree: Path) -> None:
    before = {path: (tree / path).read_text(encoding="utf-8") for path in _DERIVED}
    _bump(tree)

    result = _run(tree)

    assert result.returncode == 0, result.stderr
    # Every occurrence of the old version is a version site, so a sync that
    # reached them all and reformatted nothing leaves exactly this.
    for path in _DERIVED:
        assert (tree / path).read_text(encoding="utf-8") == before[path].replace(
            _CURRENT, _NEW
        ), path
    server = json.loads((tree / ".github/mcp/server.json").read_text(encoding="utf-8"))
    assert [p.get("version", p["identifier"]) for p in server["packages"]] == [
        _NEW,
        f"docker.io/stickerdaniel/linkedin-mcp-server:{_NEW}",
    ]
    assert f"image: stickerdaniel/linkedin-mcp-server:{_NEW}\n" in (
        tree / "docker-compose.yml"
    ).read_text(encoding="utf-8")

    check = _run(tree, "--check")
    assert check.returncode == 0, check.stderr
    assert check.stdout == f"Every versioned file reads {_NEW}\n"


@pytest.mark.parametrize("stale", [*_DERIVED, "uv.lock"])
def test_check_names_the_one_stale_file_and_writes_nothing(
    tree: Path, stale: str
) -> None:
    original = (tree / stale).read_text(encoding="utf-8")
    _bump(tree)
    assert _run(tree).returncode == 0
    (tree / stale).write_text(original, encoding="utf-8")
    files = (*_DERIVED, "uv.lock")
    before = {path: (tree / path).read_bytes() for path in files}

    result = _run(tree, "--check")

    assert result.returncode == 1
    named = [line.partition(":")[0] for line in result.stderr.splitlines()[:-1]]
    assert named == [stale]
    assert f"{_CURRENT!r}, expected {_NEW!r}" in result.stderr
    assert {path: (tree / path).read_bytes() for path in files} == before


@pytest.mark.parametrize(
    ("compose", "message"),
    [
        (
            "services:\n"
            "  linkedin-mcp:\n"
            f"    # image: stickerdaniel/linkedin-mcp-server:{_CURRENT}\n"
            "    image: stickerdaniel/linkedin-mcp-server:1.0.0\n",
            f"'1.0.0', expected {_CURRENT!r}",
        ),
        (
            "services:\n"
            "  linkedin-mcp:\n"
            f"#   image: stickerdaniel/linkedin-mcp-server:{_CURRENT}\n"
            "    image: example/other:latest\n"
            "    command: |\n"
            f"      image: stickerdaniel/linkedin-mcp-server:{_CURRENT}\n",
            "no version found",
        ),
    ],
    ids=["stale-live-image", "no-live-image"],
)
def test_check_reads_only_the_live_service_image(
    tree: Path, compose: str, message: str
) -> None:
    (tree / "docker-compose.yml").write_text(compose, encoding="utf-8")

    result = _run(tree, "--check")

    assert result.returncode == 1
    assert f"docker-compose.yml: {message}\n" in result.stderr


@pytest.mark.parametrize(
    "compose",
    [
        f'services:\n  linkedin-mcp:\n    image: "stickerdaniel/linkedin-mcp-server:{_CURRENT}"\n',
        "services:\n"
        "  linkedin-mcp:\n"
        "    volumes:\n"
        "      - ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp\n"
        f"    image: docker.io/stickerdaniel/linkedin-mcp-server:{_CURRENT}  # pinned\n",
    ],
    ids=["quoted", "registry-prefix-and-comment"],
)
def test_check_accepts_other_spellings_of_the_image(tree: Path, compose: str) -> None:
    (tree / "docker-compose.yml").write_text(compose, encoding="utf-8")

    result = _run(tree, "--check")

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", [(), ("--check",)], ids=["sync", "check"])
def test_unknown_registry_type_fails_and_writes_nothing(
    tree: Path, mode: tuple[str, ...]
) -> None:
    server_path = tree / ".github/mcp/server.json"
    server = json.loads(server_path.read_text(encoding="utf-8"))
    server["packages"].append({"registryType": "npm", "identifier": "x"})
    server_path.write_text(json.dumps(server, indent=2) + "\n", encoding="utf-8")
    _bump(tree)
    before = {path: (tree / path).read_bytes() for path in _DERIVED}

    result = _run(tree, *mode)

    assert result.returncode == 1
    assert ".github/mcp/server.json: unhandled registryType 'npm'" in result.stderr
    assert {path: (tree / path).read_bytes() for path in _DERIVED} == before
