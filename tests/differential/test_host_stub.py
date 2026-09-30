"""The host stub quits the way a host does: stdin EOF, then a wait.

Driven against stand-in stdio servers. One takes three seconds to close, which
is longer than the two seconds after which the MCP SDK's own stdio client
starts signalling, so a stub that killed its server would fail here and a
native row built on it would measure a kill while calling it a quit. Two more
answer the call and then end badly, one with a nonzero status on EOF and one
before the host ever quits; neither may read as a normal host quit. A fourth
leaves a grandchild holding its stderr past its exit, which is where the
post-exit hook has to run: after the exit, before that stderr closes.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import psutil

from differential.harness import (
    READ_TOOL,
    ToolCall,
    actor_environment,
    claim_account,
    host_failures,
    run_host_session,
)
from differential.synthetic_origin import POST_MARKER
from linkedin_mcp_server import daemon_descriptor

_STAND_IN_SERVER = """
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastmcp import FastMCP

marker, ending, die = sys.argv[1], sys.argv[2], sys.argv[3]


@asynccontextmanager
async def closing(app):
    print("stand-in server up", file=sys.stderr, flush=True)
    with open(die + ".pid", "w") as pid:
        pid.write(str(os.getpid()))
    try:
        yield {}
    finally:
        if ending == "slow":
            time.sleep(3)
            print("stand-in server closed", file=sys.stderr, flush=True)
        elif ending == "status":
            print("stand-in server failing its close", file=sys.stderr, flush=True)
            sys.stderr.flush()
            os._exit(7)
        elif ending == "linger":
            # A grandchild that inherits stderr and outlives the server by two
            # seconds, then says so on that stderr.
            import subprocess

            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; time.sleep(2); "
                    "print('grandchild done', file=sys.stderr, flush=True)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )


mcp = FastMCP("stand-in", lifespan=closing)


@mcp.tool
def %(tool)s(num_posts: int = 10) -> dict:
    if ending == "crash":
        # Dies by itself once the host has the answer, long before it quits.
        def crash():
            while not os.path.exists(die):
                time.sleep(0.02)
            os._exit(9)

        threading.Thread(target=crash, daemon=True).start()
    return {"url": "https://www.linkedin.com/feed/", "sections": {"feed": marker}}


mcp.run(transport="stdio", show_banner=False)
""" % {"tool": READ_TOOL}


async def _session(
    tmp_path: Path,
    ending: str,
    seen: list[str] | None = None,
    *,
    after_call: Callable[[], Awaitable[None]] | None = None,
    after_exit: Callable[[], Awaitable[None]] | None = None,
    script: Callable[[ToolCall], Awaitable[None]] | None = None,
):
    server = tmp_path / "stand_in_server.py"
    server.write_text(_STAND_IN_SERVER)
    die = tmp_path / "die"

    async def linger() -> None:
        if ending != "crash":
            return
        # The answer is in: let the server die, and see it gone before the
        # host quits.
        pid = int(Path(f"{die}.pid").read_text())
        die.touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            await asyncio.sleep(0.02)
        # Time for the event loop to see the exit, as a host's would.
        await asyncio.sleep(1)

    return await run_host_session(
        [sys.executable, str(server), POST_MARKER, ending, str(die)],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=(seen.append if seen is not None else lambda _line: None),
        after_call=after_call or linger,
        after_exit=after_exit,
        script=script,
    )


async def test_a_host_quit_waits_for_a_slow_server_instead_of_killing_it(tmp_path):
    seen: list[str] = []
    session = await _session(tmp_path, "slow", seen)

    assert session.error is None, session.error
    assert session.tool is not None
    assert session.tool["read_the_post"] and not session.tool["is_error"]
    assert session.alive_before_quit is True and session.stdin_closed is True
    assert session.exited_on_quit is True
    assert session.killed_by_harness is False
    assert session.quit_seconds is not None and session.quit_seconds >= 3
    assert "stand-in server closed" in seen
    assert seen == session.stderr
    assert host_failures(session) == []


async def test_a_server_that_answers_then_exits_nonzero_is_not_a_normal_quit(
    tmp_path,
):
    session = await _session(tmp_path, "status")
    assert session.tool is not None and session.tool["read_the_post"]
    assert session.alive_before_quit is True
    assert session.exited_on_quit is True and session.exit_code == 7
    assert session.killed_by_harness is False
    assert any("status 7" in failure for failure in host_failures(session))


async def test_a_server_that_died_before_the_quit_is_not_a_normal_quit(tmp_path):
    session = await _session(tmp_path, "crash")
    assert session.tool is not None and session.tool["read_the_post"]
    assert session.alive_before_quit is False
    assert any("already gone" in failure for failure in host_failures(session))


def _server_pid(tmp_path: Path) -> int:
    return int((tmp_path / "die.pid").read_text())


async def test_the_post_exit_hook_runs_after_the_exit_and_before_stderr_closes(
    tmp_path,
):
    seen: list[str] = []
    at_hook: dict = {}

    async def after_exit() -> None:
        at_hook["lines"] = list(seen)
        at_hook["ns"] = time.monotonic_ns()
        at_hook["server_running"] = psutil.pid_exists(_server_pid(tmp_path)) and (
            psutil.Process(_server_pid(tmp_path)).status() != psutil.STATUS_ZOMBIE
        )

    session = await _session(tmp_path, "linger", seen, after_exit=after_exit)

    assert session.error is None and session.after_exit_error is None
    assert at_hook["server_running"] is False
    # The grandchild still held stderr when the hook ran, and the stub then
    # waited for it as before.
    assert "grandchild done" not in at_hook["lines"]
    assert "grandchild done" in session.stderr and session.stderr_closed is True
    assert session.eof_monotonic_ns is not None
    assert session.exit_seen_monotonic_ns is not None
    assert session.eof_monotonic_ns <= session.exit_seen_monotonic_ns <= at_hook["ns"]
    assert host_failures(session) == []


async def test_a_failed_post_exit_hook_is_recorded_and_the_quit_still_completes(
    tmp_path,
):
    async def after_exit() -> None:
        raise RuntimeError("planted")

    session = await _session(tmp_path, "linger", after_exit=after_exit)

    assert session.after_exit_error == "RuntimeError: planted"
    assert session.error is None and session.teardown_error is None
    assert session.exited_on_quit is True and session.killed_by_harness is False
    # The stderr wait after the hook still ran.
    assert "grandchild done" in session.stderr and session.stderr_closed is True


async def test_the_post_exit_hook_never_runs_on_the_forced_cleanup(tmp_path):
    ran: list[str] = []

    async def fail_after_the_call() -> None:
        raise RuntimeError("the row gave up before its quit")

    async def after_exit() -> None:
        ran.append("hook")

    session = await _session(
        tmp_path, "slow", after_call=fail_after_the_call, after_exit=after_exit
    )

    assert session.error == "RuntimeError: the row gave up before its quit"
    assert session.killed_by_harness is True
    assert ran == []
    assert session.after_exit_error is None and session.exit_seen_monotonic_ns is None


async def test_the_default_and_scripted_reads_carry_their_send_and_receipt(
    tmp_path,
):
    async def script(call) -> None:
        await call(READ_TOOL, {"num_posts": 1})

    session = await _session(tmp_path, "slow", script=script)

    assert session.error is None and session.script_error is None
    assert session.tool is not None and len(session.scripted) == 1
    default, scripted = session.tool, session.scripted[0]
    for summary in (default, scripted):
        assert summary["tool"] == READ_TOOL and summary["read_the_post"]
        assert summary["began"] <= summary["ended"]
        assert summary["began_monotonic_ns"] <= summary["ended_monotonic_ns"]
    assert default["ended_monotonic_ns"] <= scripted["began_monotonic_ns"]
    assert session.eof_monotonic_ns is not None
    assert scripted["ended_monotonic_ns"] <= session.eof_monotonic_ns


def test_the_actor_environment_carries_the_row_and_drops_inherited_settings(
    tmp_path, monkeypatch
):
    # A CHROME_PATH left in the environment would quietly keep the server off
    # the daemon (a custom browser stays Direct), so K3 would measure K1.
    monkeypatch.setenv("CHROME_PATH", "/somewhere/chrome")
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_COOKIE_SET", "bridge_core")
    # A home of its own, so the account guard never looks at the real one.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: home)
    (tmp_path / "auth").mkdir()
    account = claim_account(tmp_path / "auth" / "profile")

    env = actor_environment(
        account, "http://127.0.0.1:9", daemon=True, browsers=Path("/cache")
    )

    assert "CHROME_PATH" not in env
    assert "LINKEDIN_DEBUG_BRIDGE_COOKIE_SET" not in env
    assert env["USER_DATA_DIR"] == str(account.profile)
    assert env["PROXY_SERVER"] == "http://127.0.0.1:9"
    assert env["DAEMON_ENABLED"] == "true"
    assert env["HEADLESS"] == "true"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(Path("/cache"))
    assert (
        actor_environment(
            account, "http://127.0.0.1:9", daemon=False, browsers=Path("/cache")
        )["DAEMON_ENABLED"]
        == "false"
    )
