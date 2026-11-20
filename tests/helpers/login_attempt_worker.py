"""Child process used by the cross-process login tests.

Runs the real ``interactive_login`` with the browser half replaced by a body
that writes a session into the attempt directory it was handed, so the kernel
arbitrates the attempt locks, the source lease and the guardians.

Commands:

``login <profile> <guard> <hold>``
    *guard* is ``unguarded``, ``none`` or a generation. *hold* is ``none``,
    ``close`` (held after the cookie, before the browser has closed), ``lease``
    (held after the peer check, before asking for the source lease) or
    ``unconfirmed`` (the browser close is not confirmed; the process stays up
    afterwards). Prints ``HELD`` where it is held and waits for a line on
    stdin, then prints ``RESULT <bool>``.

``crash <profile> <attempt|both|released>``
    Opens a pending login and starts a marked process group for it. ``both``
    also takes the source lease with a guardian of its own, as a serving
    browser does, and starts a second group; ``released`` then lets that
    guardian and the lease go again, as a confirmed close does. Prints
    ``READY <attempt-dir> <pid>...`` and waits to be killed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from linkedin_mcp_server import setup  # noqa: E402
from linkedin_mcp_server.login_attempt import (  # noqa: E402
    PendingLogin,
    open_pending_login,
)
from linkedin_mcp_server.process_tree import (  # noqa: E402
    new_browser_process_marker,
    remember_detached_process_groups,
    start_browser_guardian,
)
from linkedin_mcp_server.profile_lease import (  # noqa: E402
    ProfileLease,
    get_profile_lease,
)
from linkedin_mcp_server.session_state import UNGUARDED  # noqa: E402


def _held_here() -> None:
    print("HELD", flush=True)
    sys.stdin.readline()


def _guard(raw: str) -> object:
    if raw == "unguarded":
        return UNGUARDED
    if raw == "none":
        return None
    return raw


def _login(profile: Path, guard: str, hold: str) -> int:
    async def body(
        pending: PendingLogin,
        *,
        config: Any,
        state: setup._LoginState,
        stop_when: Callable[[], bool] | None = None,
        login_viewer: bool = False,
    ) -> bool:
        (pending.profile_dir / "Default").mkdir(parents=True)
        (pending.profile_dir / "Default" / "Cookies").write_text(f"pid {os.getpid()}")
        pending.cookie_path.write_text("[]")
        if hold == "close":
            await asyncio.to_thread(_held_here)
        state.close_confirmed = hold != "unconfirmed"
        return True

    setattr(setup, "_login_into_fresh_profile", body)
    if hold == "lease":
        real = setup.get_profile_lease

        def held_back(source_profile_dir: Path | None = None) -> ProfileLease:
            _held_here()
            return real(source_profile_dir)

        setattr(setup, "get_profile_lease", held_back)

    result = asyncio.run(setup.interactive_login(profile, superseded_by=_guard(guard)))
    print(f"RESULT {result}", flush=True)
    if hold == "unconfirmed":
        sys.stdin.readline()
    return 0


def _marked_group() -> int:
    marker, environment = new_browser_process_marker()
    browser = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, **environment},
    )
    remember_detached_process_groups(marker)
    return browser.pid


def _crash(profile: Path, which: str) -> int:
    pending = open_pending_login(profile)
    pids = [_marked_group()]
    if which in ("both", "released"):
        lease = get_profile_lease(profile)
        assert lease.try_acquire()
        serving = start_browser_guardian(lease.guardian_fd())
        if which == "both":
            pids.append(_marked_group())
        else:
            serving.release()
            lease.release()
    print("READY", pending.directory, *pids, flush=True)
    time.sleep(600)
    return 0


def main() -> int:
    command, *args = sys.argv[1:]
    if command == "login":
        return _login(Path(args[0]), args[1], args[2])
    if command == "crash":
        return _crash(Path(args[0]), args[1])
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
