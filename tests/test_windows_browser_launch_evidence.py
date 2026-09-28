"""Locked-stack native Windows browser-launch evidence for issue #808."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server import process_tree

_PROBE = Path(__file__).with_name("windows_guardian_probe.py")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_BROWSER = "153.0.8010.12"


def _without_node_override[T](probe: Callable[[], T]) -> T:
    override = os.environ.pop("PLAYWRIGHT_NODEJS_PATH", None)
    try:
        return probe()
    finally:
        if override is not None:
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = override


def _assign_and_release(process: Any, harness: Any, nonce: str) -> None:
    try:
        harness.assign_popen(process)
    except BaseException as assignment_error:
        try:
            process.kill()
            process.wait(timeout=5)
        except BaseException:
            pass
        raise assignment_error
    assert process.stdin is not None
    process_tree.release_windows_gate(process.stdin, nonce)


def _locked_stack() -> dict[str, str]:
    import patchright
    from patchright._impl._driver import compute_driver_executable

    package = Path(patchright.__file__).parent
    core = json.loads(
        (package / "driver" / "package" / "package.json").read_text(encoding="utf-8")
    )
    browsers = json.loads(
        (package / "driver" / "package" / "browsers.json").read_text(encoding="utf-8")
    )
    chromium = next(item for item in browsers["browsers"] if item["name"] == "chromium")
    expected_node = package / "driver" / "node.exe"

    def probe() -> tuple[Path, dict[str, str], Path]:
        node, _entrypoint = compute_driver_executable()
        node_path = Path(node)
        if node_path.resolve() != expected_node.resolve():
            raise AssertionError(
                "Patchright did not select its wheel-owned driver/node.exe"
            )
        node_runtime = json.loads(
            subprocess.run(
                [
                    node_path,
                    "-e",
                    "console.log(JSON.stringify({node:process.version,uv:process.versions.uv}))",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        from patchright.sync_api import sync_playwright

        with sync_playwright() as driver:
            resolved_browser = Path(driver.chromium.executable_path)
        return node_path, node_runtime, resolved_browser

    node_path, node_runtime, resolved_browser = _without_node_override(probe)
    return {
        "patchright": version("patchright"),
        "core": core["version"],
        "revision": chromium["revision"],
        "browser_manifest_version": chromium["browserVersion"],
        "node_path": str(node_path),
        "node": node_runtime["node"],
        "uv": node_runtime["uv"],
        "browser_path": str(resolved_browser),
        "browser_exists": str(resolved_browser.exists()).lower(),
    }


@pytest.mark.skipif(os.name != "nt", reason="native Windows browser Job evidence")
def test_locked_browser_tree_remains_fenced_until_guardian_proves_zero(
    tmp_path: Path,
) -> None:
    stack = _locked_stack()
    assert stack == {
        **stack,
        "patchright": "1.63.0",
        "core": "1.63.0",
        "revision": "1243",
        "browser_manifest_version": _EXPECTED_BROWSER,
        "node": "v24.21.0",
        "uv": "1.52.1",
        "browser_exists": "true",
    }

    root = tmp_path / "browser-launch-owner-loss"
    root.mkdir()
    harness = process_tree.WindowsJob.named("browser-launch-evidence")
    assert harness.name is not None
    (root / "outer-job.json").write_text(
        json.dumps({"name": harness.name}), encoding="utf-8"
    )
    nonce = process_tree.release_nonce()
    process = subprocess.Popen(
        process_tree.windows_gate_command(
            [
                sys.executable,
                str(_PROBE),
                "run",
                "browser-launch-owner-loss",
                str(root),
            ],
            nonce,
        ),
        cwd=_REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PLAYWRIGHT_NODEJS_PATH": str(root / "must-not-run-node.exe"),
        },
    )
    try:
        _assign_and_release(process, harness, nonce)
        stdout, stderr = process.communicate(timeout=180)
        assert process.returncode == 0, stderr.decode("utf-8", "replace")
        harness.wait_until_empty(timeout=60)
    finally:
        if not harness.closed:
            harness.terminate()
            if process.poll() is None:
                process.wait(timeout=30)
            harness.wait_until_empty(timeout=30)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)

    measurement = json.loads(stdout)
    browser = measurement["browser"]
    inventory = measurement["inventory"]
    assert measurement["scenario"] == "browser-launch-owner-loss"
    assert measurement["lease_rejections"] == [
        "inventory-retained",
        "job-zero",
        "both-zero",
    ]
    assert measurement["post_release_acquired"] is True
    assert measurement["outer_process_ids"] == measurement["coordinator_process_ids"]
    assert measurement["coordinator_process_ids"]
    assert browser["renderer_ready"] is True
    assert browser["worker_ready"] is True
    assert browser["patchright_browser_version"] == _EXPECTED_BROWSER
    assert browser["browser_get_version"]["product"].endswith(_EXPECTED_BROWSER)
    assert inventory["required_roles"] == ["browser", "renderer"]
    assert inventory["owner_outside_inner"] is True
    assert inventory["guardian_outside_inner"] is True
    assert inventory["driver_in_inner_and_outer"] is True
    assert inventory["cdp_sample_count"] >= 2
    assert (
        Path(inventory["browser_image_path"]).resolve()
        == Path(stack["browser_path"]).resolve()
    )
    metadata = measurement["metadata"]
    assert metadata["inner_job_kill_on_close"] is True
    assert metadata["ignored_playwright_nodejs_path"] is True
    assert (
        Path(metadata["expected_driver_path"]).resolve()
        == Path(stack["node_path"]).resolve()
    )
    assert (
        Path(
            inventory["process_identities"][str(metadata["driver_pid"])]["image_path"]
        ).resolve()
        == Path(stack["node_path"]).resolve()
    )
    assert metadata["prelaunch_active_processes"] == 1
    assert metadata["prelaunch_inner_pids"] == [metadata["driver_pid"]]

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"stack": stack, "measurement": measurement}))
            stream.write("\n")


def test_stack_gate_hides_invalid_node_override_and_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "invalid-node")
    observed: list[str | None] = []

    assert (
        _without_node_override(
            lambda: observed.append(os.environ.get("PLAYWRIGHT_NODEJS_PATH")) or "ok"
        )
        == "ok"
    )
    assert observed == [None]
    assert os.environ["PLAYWRIGHT_NODEJS_PATH"] == "invalid-node"


def test_assignment_failure_kills_gated_process_and_preserves_primary() -> None:
    events: list[str] = []
    assignment_error = OSError("assignment failed")

    class Process:
        stdin = object()

        def kill(self) -> None:
            events.append("kill")

        def wait(self, *, timeout: float) -> int:
            events.append(f"wait {timeout}")
            raise OSError("cleanup failed")

    class Harness:
        def assign_popen(self, _process: object) -> None:
            events.append("assign")
            raise assignment_error

    with pytest.raises(OSError) as raised:
        _assign_and_release(Process(), Harness(), "nonce")

    assert raised.value is assignment_error
    assert events == ["assign", "kill", "wait 5"]
