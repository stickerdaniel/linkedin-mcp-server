import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from linkedin_mcp_server.profile_lease import ProfileLeaseUnavailableError

from linkedin_mcp_server.debug_trace import (
    _safe_source_profile_dir,
    cleanup_trace_dir,
    get_trace_dir,
    mark_trace_for_retention,
    record_page_trace,
    reset_trace_state_for_testing,
)


def setup_function():
    reset_trace_state_for_testing()


def teardown_function():
    reset_trace_state_for_testing()


def test_get_trace_dir_creates_ephemeral_dir_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    trace_dir = get_trace_dir()

    assert trace_dir is not None
    assert trace_dir.exists()
    assert "trace-runs" in str(trace_dir)


def test_cleanup_trace_dir_removes_ephemeral_dir_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    trace_dir = get_trace_dir()
    assert trace_dir is not None

    cleanup_trace_dir()

    assert not trace_dir.exists()


def test_mark_trace_for_retention_keeps_trace_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    trace_dir = mark_trace_for_retention()
    assert trace_dir is not None

    cleanup_trace_dir()

    assert trace_dir.exists()


def test_explicit_trace_dir_is_preserved(monkeypatch, tmp_path):
    trace_dir = tmp_path / "explicit-trace"
    monkeypatch.setenv("LINKEDIN_DEBUG_TRACE_DIR", str(trace_dir))

    resolved = get_trace_dir()
    assert resolved == trace_dir
    trace_dir.mkdir(parents=True, exist_ok=True)

    cleanup_trace_dir()

    assert trace_dir.exists()


def test_trace_mode_off_disables_trace_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("LINKEDIN_TRACE_MODE", "off")

    assert get_trace_dir() is None


@pytest.mark.asyncio
async def test_reset_trace_state_resets_step_counter(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    page = MagicMock()
    page.url = "https://www.linkedin.com/feed/"
    page.title = AsyncMock(return_value="LinkedIn")
    page.evaluate = AsyncMock(return_value="Feed")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=locator)
    page.context.cookies = AsyncMock(return_value=[])
    page.screenshot = AsyncMock()

    await record_page_trace(page, "first")
    trace_dir = get_trace_dir()
    assert trace_dir is not None
    first_payload = json.loads((trace_dir / "trace.jsonl").read_text().splitlines()[0])
    assert first_payload["step_id"] == 1

    reset_trace_state_for_testing()
    monkeypatch.setenv("USER_DATA_DIR", str((tmp_path / "second") / "profile"))

    await record_page_trace(page, "first-again")
    second_trace_dir = get_trace_dir()
    assert second_trace_dir is not None
    second_payload = json.loads(
        (second_trace_dir / "trace.jsonl").read_text().splitlines()[0]
    )
    assert second_payload["step_id"] == 1


def test_safe_source_profile_dir_ignores_generic_env_fallback(monkeypatch):
    monkeypatch.setenv("USER_DATA_DIR", "/tmp/unrelated-user-data")
    monkeypatch.setattr(
        "linkedin_mcp_server.debug_trace.get_source_profile_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert _safe_source_profile_dir() == Path("~/.linkedin-mcp/profile").expanduser()


def _aged_run(root: Path, name: str, *, days: float) -> Path:
    run = root / name
    run.mkdir(parents=True)
    log = run / "server.log"
    log.write_text("x")
    stamp = time.time() - days * 24 * 60 * 60
    os.utime(log, (stamp, stamp))
    os.utime(run, (stamp, stamp))
    return run


def _use_profile(monkeypatch, profile: Path) -> None:
    monkeypatch.setattr(
        "linkedin_mcp_server.debug_trace.get_source_profile_dir", lambda: profile
    )


def test_a_new_run_removes_runs_left_behind_long_ago(monkeypatch, tmp_path):
    _use_profile(monkeypatch, tmp_path / "profile")
    root = tmp_path / "trace-runs"
    stale = _aged_run(root, "run-stale", days=30)
    recent = _aged_run(root, "run-recent", days=1)
    foreign = _aged_run(root, "notes", days=30)

    trace_dir = get_trace_dir()

    assert trace_dir is not None and trace_dir.parent == root.resolve()
    assert not stale.exists()
    assert recent.exists() and foreign.exists()


def test_a_quiet_run_of_a_live_server_is_kept(monkeypatch, tmp_path):
    """Another server that has logged nothing for weeks still owns its run."""
    _use_profile(monkeypatch, tmp_path / "profile")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "USER_DATA_DIR": str(tmp_path / "profile"),
    }
    env.pop("LINKEDIN_DEBUG_TRACE_DIR", None)
    env.pop("LINKEDIN_TRACE_MODE", None)
    other = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from linkedin_mcp_server.debug_trace import get_trace_dir;"
            "print(get_trace_dir(), flush=True); input()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert other.stdout is not None
        quiet = Path(other.stdout.readline().strip())
        assert quiet.parent == (tmp_path / "trace-runs").resolve()
        stamp = time.time() - 30 * 24 * 60 * 60
        for path in (*quiet.iterdir(), quiet):
            os.utime(path, (stamp, stamp))

        get_trace_dir()

        assert quiet.exists()
    finally:
        other.communicate("\n", timeout=30)


def test_a_run_that_could_not_be_locked_is_kept(monkeypatch, tmp_path):
    """Its server may still be alive once locking works again."""
    _use_profile(monkeypatch, tmp_path / "profile")

    def unavailable(fd: int, *, exclusive: bool) -> bool:
        raise ProfileLeaseUnavailableError("no locks here")

    with monkeypatch.context() as patched:
        patched.setattr("linkedin_mcp_server.debug_trace.try_lock", unavailable)
        unlocked = get_trace_dir()
    assert unlocked is not None
    stamp = time.time() - 30 * 24 * 60 * 60
    for path in (*unlocked.iterdir(), unlocked):
        os.utime(path, (stamp, stamp))
    reset_trace_state_for_testing()

    get_trace_dir()

    assert unlocked.exists()


def test_a_run_still_being_written_is_kept(monkeypatch, tmp_path):
    _use_profile(monkeypatch, tmp_path / "profile")
    live = _aged_run(tmp_path / "trace-runs", "run-live", days=30)
    (live / "server.log").write_text("still logging")

    get_trace_dir()

    assert live.exists()


def test_runs_under_a_root_this_server_does_not_own_are_left_alone(
    monkeypatch, tmp_path
):
    # Not tmp_path/profile, which the isolation fixture claims.
    profile = tmp_path / "elsewhere" / "profile"
    profile.mkdir(parents=True)
    (profile / "someone-elses-file").write_text("x")
    _use_profile(monkeypatch, profile)
    stale = _aged_run(tmp_path / "elsewhere" / "trace-runs", "run-stale", days=30)

    get_trace_dir()

    assert stale.exists()


def test_a_linked_trace_root_is_not_pruned(monkeypatch, tmp_path):
    _use_profile(monkeypatch, tmp_path / "profile")
    elsewhere = tmp_path / "elsewhere"
    stale = _aged_run(elsewhere, "run-stale", days=30)
    try:
        (tmp_path / "trace-runs").symlink_to(elsewhere, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"this runner cannot create symlinks: {exc}")

    get_trace_dir()

    assert stale.exists()
