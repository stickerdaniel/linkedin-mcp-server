"""Release workflow invariants outside the versioned registry files."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="requires Bash")


def _job(name: str) -> str:
    marker = f"  {name}:\n"
    start = _WORKFLOW.index(marker)
    rest = _WORKFLOW[start + len(marker) :]
    following = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n", rest)
    end = (
        len(_WORKFLOW) if following is None else start + len(marker) + following.start()
    )
    return _WORKFLOW[start:end]


def _script(job: str, name: str) -> str:
    rest = job.split(f"      - name: {name}\n", 1)[1]
    block = rest.split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.startswith("          "):
            lines.append(line[10:])
        elif line:
            break
        else:
            lines.append("")
    return "\n".join(lines) + "\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    remote, source = tmp_path / "remote.git", tmp_path / "source"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "-b", "main", str(source))
    for key, value in {
        "user.name": "Release Test",
        "user.email": "release@example.invalid",
        "commit.gpgsign": "false",
    }.items():
        _git(source, "config", key, value)
    _git(source, "remote", "add", "origin", str(remote))

    def commit(name: str) -> str:
        (source / "version.txt").write_text(name, encoding="utf-8")
        _git(source, "add", "version.txt")
        _git(source, "commit", "-m", name)
        return _git(source, "rev-parse", "HEAD")

    one = commit("released-one")
    _git(source, "tag", "v1.0.0")
    two = commit("released-two")
    _git(source, "tag", "v2.0.0")
    unreleased = commit("unreleased")
    _git(source, "switch", "-c", "diverged", one)
    divergent = commit("divergent")
    _git(source, "switch", "main")
    _git(source, "push", "origin", "--all")
    _git(source, "push", "origin", "--tags")
    return remote, {
        "one": one,
        "two": two,
        "unreleased": unreleased,
        "divergent": divergent,
    }


def _run(
    tmp_path: Path,
    remote: Path,
    head: str,
    branch: str | None,
    script: str,
    **env: str,
):
    if branch is not None:
        _git(remote, "update-ref", "refs/heads/docker-release", branch)
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", str(remote), str(checkout))
    _git(checkout, "checkout", "--detach", head)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        text=True,
        capture_output=True,
        env={**os.environ, **env},
    )
    tip = _git(remote, "rev-parse", "refs/heads/docker-release")
    return result, tip, checkout


def test_release_order_permissions_and_checkout_are_pinned() -> None:
    job = _job("advance-docker-catalog")
    assert "needs: [prepare-release, create-github-release]" in job
    assert _WORKFLOW.index("  create-github-release:\n") < _WORKFLOW.index(
        "  advance-docker-catalog:\n"
    )
    assert "ref: ${{ needs.prepare-release.outputs.release-sha }}" in job
    assert "fetch-depth: 0" in job
    assert "\n    if:" not in job
    assert "permissions:\n      contents: write" in job
    assert "id-token:" not in job


def test_existing_tag_must_name_the_release_commit(tmp_path: Path) -> None:
    remote, revisions = _repository(tmp_path)
    script = _script(_job("prepare-release"), "Create release tag")
    result, _, _ = _run(
        tmp_path, remote, revisions["two"], revisions["one"], script, VERSION="1.0.0"
    )
    assert result.returncode != 0
    assert "expected released commit" in result.stdout


def test_non_commit_tag_is_refused(tmp_path: Path) -> None:
    remote, revisions = _repository(tmp_path)
    tree = _git(remote, "rev-parse", f"{revisions['one']}^{{tree}}")
    _git(remote, "update-ref", "refs/tags/v1.0.0", tree)
    script = _script(_job("prepare-release"), "Create release tag")
    result, _, _ = _run(
        tmp_path, remote, revisions["two"], revisions["one"], script, VERSION="1.0.0"
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("head", "branch", "succeeds", "expected"),
    [
        ("one", None, True, "one"),
        ("two", "one", True, "two"),
        ("one", "two", False, "two"),
        ("two", "unreleased", False, "unreleased"),
        ("two", "divergent", False, "divergent"),
    ],
)
def test_release_branch_moves_only_forward(
    tmp_path: Path, head: str, branch: str | None, succeeds: bool, expected: str
) -> None:
    remote, revisions = _repository(tmp_path)
    script = _script(
        _job("advance-docker-catalog"), "Advance Docker catalog release branch"
    )
    remote_branch = None if branch is None else revisions[branch]
    result, tip, _ = _run(tmp_path, remote, revisions[head], remote_branch, script)
    assert (result.returncode == 0) is succeeds
    assert tip == revisions[expected]


def test_rejected_push_fails_the_job(tmp_path: Path) -> None:
    remote, revisions = _repository(tmp_path)
    script = _script(
        _job("advance-docker-catalog"), "Advance Docker catalog release branch"
    )
    _, _, checkout = _run(tmp_path, remote, revisions["two"], revisions["one"], "true")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result = subprocess.run(["bash", "-c", script], cwd=checkout)
    assert result.returncode != 0
    assert _git(remote, "rev-parse", "refs/heads/docker-release") == revisions["one"]
