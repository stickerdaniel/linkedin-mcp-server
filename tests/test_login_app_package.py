"""The sign-in view's page ships in every distribution the server is installed from."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from linkedin_mcp_server import login_app

_REPO = Path(__file__).resolve().parents[1]
_SHIPPED = (
    "linkedin_mcp_server/login_app/viewer.html",
    f"linkedin_mcp_server/login_app/{login_app.SDK_FILE}",
    "linkedin_mcp_server/login_app/EXT_APPS_LICENSE",
)


def test_the_shipped_sdk_is_the_vendored_release():
    shipped = _REPO / "linkedin_mcp_server" / "login_app" / login_app.SDK_FILE
    assert hashlib.sha256(shipped.read_bytes()).hexdigest() == login_app.SDK_SHA256


def test_the_wheel_carries_the_page_the_sdk_and_its_license(tmp_path: Path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    # Built from a copy: setuptools writes `build/` and egg-info next to the
    # sources it builds.
    source = tmp_path / "source"
    shutil.copytree(
        _REPO / "linkedin_mcp_server",
        source / "linkedin_mcp_server",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
        shutil.copy2(_REPO / name, source / name)
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(source)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    (wheel,) = (tmp_path / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert set(_SHIPPED) <= names
        sdk = archive.read(f"linkedin_mcp_server/login_app/{login_app.SDK_FILE}")
    assert hashlib.sha256(sdk).hexdigest() == login_app.SDK_SHA256


def test_the_extension_bundle_does_not_ignore_them():
    # `mcpb pack` skips what `.mcpbignore` matches, in .gitignore syntax, which
    # is what git evaluates here. Exit status 1 means no path is ignored.
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    checked = subprocess.run(
        [
            git,
            "-c",
            f"core.excludesFile={_REPO / '.mcpbignore'}",
            "check-ignore",
            "--no-index",
            "-v",
            *_SHIPPED,
        ],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if checked.returncode == 128:
        pytest.skip(f"not a git checkout: {checked.stderr.strip()}")
    assert checked.returncode == 1, checked.stdout
