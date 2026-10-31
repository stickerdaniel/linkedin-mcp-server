"""A fixed host name lets a new container clear a killed one's profile lock.

Chromium clears a stale ``SingletonLock`` only when it names Chromium's own
host, and Docker gives each new container a new one. Without a fixed name, a
container killed with Chromium running locks the mounted profile against every
later container. See ``docs/decisions/2026-10-22-docker-fixed-hostname.md``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOSTNAME = "linkedin-mcp"


def test_compose_fixes_the_host_name():
    compose = yaml.safe_load((_REPO_ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["linkedin-mcp"]["hostname"] == _HOSTNAME


@pytest.mark.browser_contract
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.skipif(os.name == "nt", reason="the lock is a symlink only on POSIX")
async def test_chromium_clears_a_stale_lock_only_from_its_own_host(tmp_path):
    """The behaviour the fixed name relies on, measured on the shipped browser."""
    from patchright.async_api import async_playwright

    profile = tmp_path / "profile"
    lock = profile / "SingletonLock"
    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()

    async with async_playwright() as playwright:

        async def launch() -> None:
            context = await playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True
            )
            await context.close()

        try:
            await launch()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        assert not lock.is_symlink(), "a clean exit left its lock behind"

        foreign = f"old-container-{gone.pid}"
        os.symlink(foreign, lock)
        with pytest.raises(Exception):
            await launch()
        assert os.readlink(lock) == foreign, "Chromium cleared another host's lock"

        lock.unlink()
        os.symlink(f"{socket.gethostname()}-{gone.pid}", lock)
        await launch()


@pytest.mark.browser_contract
@pytest.mark.xdist_group("browser_runtime")
@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="process names come from /proc"
)
async def test_a_same_host_lock_holds_only_while_a_chromium_has_its_pid(tmp_path):
    """Containers sharing the host name number their processes separately, so
    a killed one's lock can name a pid something else holds. Chromium then
    clears the lock; ``profile_in_use_by`` has to agree, and still hold a lock
    a running Chromium wrote."""
    from patchright.async_api import async_playwright

    from linkedin_mcp_server.session_state import profile_in_use_by

    profile = tmp_path / "profile"
    lock = profile / "SingletonLock"

    async with async_playwright() as playwright:
        try:
            context = await playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True
            )
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            assert profile_in_use_by(profile) == lock
        finally:
            await context.close()

        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            os.symlink(f"{socket.gethostname()}-{other.pid}", lock)
            assert profile_in_use_by(profile) is None
            context = await playwright.chromium.launch_persistent_context(
                profile, channel="chromium", headless=True
            )
            await context.close()
        finally:
            other.kill()
            other.wait()
