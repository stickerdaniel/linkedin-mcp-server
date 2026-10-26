"""Contracts for the short-lived Docker login viewer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import re
import signal
import stat
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import unquote

import pytest

import linkedin_mcp_server.login_viewer as viewer_module
from linkedin_mcp_server.login_viewer import (
    LoginViewer,
    LoginViewerError,
    mount_points,
    require_persistent_profile_mount,
    viewer_url,
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (
            "abc_123-XYZ",
            "http://127.0.0.1:6080/vnc_lite.html#?"
            "path=websockify%3Ftoken%3Dabc_123-XYZ&scale=true",
        )
    ],
)
def test_viewer_url_is_an_exact_token_private_fragment(
    token: str, expected: str
) -> None:
    assert viewer_url(token) == expected
    assert "?token=" not in expected.split("#", 1)[0]
    assert "scale=true" in expected
    assert "resize=" not in expected

    # Debian Bookworm's vnc_lite.html uses this exact `[?&]` parser. The `?`
    # after `#` is load-bearing: a fragment beginning with bare `#path=` does
    # not match even though the upstream source comment suggests it does.
    match = re.match(r".*[?&]path=([^&#]*)", expected)
    assert match is not None
    assert unquote(match.group(1)) == f"websockify?token={token}"


def test_mountinfo_decodes_escaped_mount_paths() -> None:
    text = (
        "36 25 0:31 / / rw,relatime - overlay overlay rw\n"
        "51 36 8:1 /profile /home/pwuser/My\\040Auth\\134Root rw - ext4 /dev/sda rw\n"
    )

    assert mount_points(text) == (
        Path("/"),
        Path("/home/pwuser/My Auth\\Root"),
    )


def test_mount_preflight_accepts_a_distinct_ancestor_mount(tmp_path: Path) -> None:
    profile = tmp_path / "mounted root" / "nested" / "profile"
    mountinfo = tmp_path / "mountinfo"
    escaped = str(profile.parents[1]).replace(" ", r"\040")
    mountinfo.write_text(
        f"36 25 0:31 / / rw - overlay overlay rw\n"
        f"51 36 8:1 / {escaped} rw - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )

    require_persistent_profile_mount(profile, mountinfo_path=mountinfo)


def test_mount_preflight_accepts_the_authentication_root_mount(tmp_path: Path) -> None:
    profile = tmp_path / "auth-root" / "profile"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:31 / / rw - overlay overlay rw\n"
        f"51 36 8:1 / {profile.parent} rw - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )

    require_persistent_profile_mount(profile, mountinfo_path=mountinfo)


@pytest.mark.parametrize("nested", [False, True])
def test_mount_preflight_rejects_mounts_at_or_inside_the_profile(
    tmp_path: Path, nested: bool
) -> None:
    profile = tmp_path / "auth-root" / "profile"
    mount_point = profile / "Default" if nested else profile
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:31 / / rw - overlay overlay rw\n"
        f"50 36 8:1 / {profile.parent} rw - ext4 /dev/sda rw\n"
        f"51 50 8:2 / {mount_point} rw - ext4 /dev/sdb rw\n",
        encoding="utf-8",
    )

    with pytest.raises(LoginViewerError, match="cannot rotate the profile"):
        require_persistent_profile_mount(profile, mountinfo_path=mountinfo)


@pytest.mark.parametrize("filesystem", ["tmpfs", "ramfs"])
def test_mount_preflight_rejects_a_memory_backed_authentication_root(
    tmp_path: Path, filesystem: str
) -> None:
    profile = tmp_path / "persistent" / "auth-root" / "profile"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:31 / / rw - overlay overlay rw\n"
        f"50 36 8:1 / {profile.parents[1]} rw - ext4 /dev/sda rw\n"
        f"51 50 0:51 / {profile.parent} rw - {filesystem} {filesystem} rw\n",
        encoding="utf-8",
    )

    with pytest.raises(LoginViewerError, match=filesystem):
        require_persistent_profile_mount(profile, mountinfo_path=mountinfo)


def test_mount_preflight_uses_the_most_specific_covering_mount(tmp_path: Path) -> None:
    profile = tmp_path / "persistent" / "auth-root" / "nested" / "profile"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:31 / / rw - overlay overlay rw\n"
        f"50 36 0:51 / {profile.parents[2]} rw - tmpfs tmpfs rw\n"
        f"51 50 8:1 / {profile.parents[1]} rw - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )

    require_persistent_profile_mount(profile, mountinfo_path=mountinfo)


def test_mount_preflight_rejects_an_unwritable_authentication_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "auth-root" / "profile"
    profile.mkdir(parents=True)
    marker = profile / "session-marker"
    marker.write_text("unchanged", encoding="utf-8")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:31 / / rw - overlay overlay rw\n"
        f"51 36 8:1 / {profile.parent} rw - ext4 /dev/sda rw\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(viewer_module.os, "access", lambda *_args: False)

    with pytest.raises(LoginViewerError, match="is not writable") as error:
        require_persistent_profile_mount(profile, mountinfo_path=mountinfo)

    assert "mkdir -p ~/.linkedin-mcp" not in str(error.value)
    assert marker.read_text(encoding="utf-8") == "unchanged"


def test_mount_preflight_rejects_only_the_container_root(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("36 25 0:31 / / rw - overlay overlay rw\n", encoding="utf-8")

    with pytest.raises(LoginViewerError, match="distinct persistent mount"):
        require_persistent_profile_mount(
            tmp_path / "unmounted" / "profile", mountinfo_path=mountinfo
        )


def test_default_mount_remedy_is_exact(tmp_path: Path) -> None:
    from linkedin_mcp_server.config.schema import DEFAULT_USER_DATA_DIR

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("36 25 0:31 / / rw - overlay overlay rw\n", encoding="utf-8")

    with pytest.raises(
        LoginViewerError,
        match=r"\-v ~[/]\.linkedin-mcp:/home/pwuser/\.linkedin-mcp",
    ):
        require_persistent_profile_mount(
            Path(DEFAULT_USER_DATA_DIR), mountinfo_path=mountinfo
        )


class _FakeProcess:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.status: int | None = None

    def poll(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")
        self.status = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.status or 0

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")
        self.status = -9


class _Connection:
    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_exact_commands_token_mode_secrecy_and_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    events: list[str] = []
    directory = tmp_path / "viewer"
    token = "fixed-secret-token"

    def fake_popen(command: list[str], **_kwargs: Any) -> _FakeProcess:
        commands.append(command)
        return _FakeProcess(Path(command[0]).name, events)

    def fake_mkdtemp(**_kwargs: Any) -> str:
        directory.mkdir()
        return str(directory)

    monkeypatch.setattr(viewer_module.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(viewer_module.secrets, "token_urlsafe", lambda _bytes: token)
    monkeypatch.setattr(viewer_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        viewer_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="_NET_SUPPORTING_WM_CHECK: window # 0x1"
        ),
    )
    monkeypatch.setattr(
        viewer_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: _Connection(),
    )
    monkeypatch.setattr(viewer_module.time, "sleep", lambda _seconds: None)

    viewer = LoginViewer(display=":99")
    viewer.start_window_manager()
    url = viewer.start_remote_control()
    token_file = directory / "tokens"

    assert commands == [
        ["openbox", "--sm-disable"],
        [
            "x11vnc",
            "-display",
            ":99",
            "-rfbport",
            "5900",
            "-listen",
            "127.0.0.1",
            "-allow",
            "127.0.0.1",
            "-no6",
            "-shared",
            "-forever",
            "-nopw",
            "-noremote",
        ],
        [
            "websockify",
            "--web=/usr/share/novnc",
            "--token-plugin=ReadOnlyTokenFile",
            f"--token-source={token_file}",
            "0.0.0.0:6080",
        ],
    ]
    assert token_file.read_text(encoding="utf-8") == (f"{token}: 127.0.0.1:5900\n")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert token not in " ".join(part for command in commands for part in command)
    assert url == viewer_url(token)

    viewer.stop_remote_control()
    assert events == ["terminate:websockify", "terminate:x11vnc"]
    assert token_file.exists()
    viewer.stop_window_manager()
    assert events[-1] == "terminate:openbox"
    assert not directory.exists()


def test_dockerfile_installs_viewer_packages_without_x11_utils() -> None:
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
        encoding="utf-8"
    )
    for package in ("openbox", "x11vnc", "novnc", "websockify"):
        assert package in dockerfile
    assert "--no-install-recommends" in dockerfile
    assert "chown -R pwuser:pwuser /opt/patchright" in dockerfile
    assert "x11-utils" not in dockerfile


def test_config_requires_login_and_rejects_other_one_shot_modes() -> None:
    from linkedin_mcp_server.config.schema import AppConfig, ConfigurationError

    config = AppConfig()
    config.server.login_viewer = True
    with pytest.raises(ConfigurationError, match="requires --login"):
        config.validate()

    config.server.login = True
    config.server.status = True
    with pytest.raises(ConfigurationError, match="--status"):
        config.validate()


def test_cli_preflight_rejects_a_host_before_mount_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from linkedin_mcp_server import cli_main
    from linkedin_mcp_server.config.schema import AppConfig, ConfigurationError

    config = AppConfig()
    config.server.login = True
    config.server.login_viewer = True
    monkeypatch.setattr(cli_main, "is_container_runtime", lambda: False)
    mount_check = pytest.fail
    monkeypatch.setattr(cli_main, "require_persistent_profile_mount", mount_check)

    with pytest.raises(ConfigurationError, match="only inside a container"):
        cli_main._preflight_login_viewer(config)


def test_viewer_signal_notifies_the_async_login_and_restores_handlers() -> None:
    import linkedin_mcp_server.setup as setup

    received: list[int] = []
    previous = signal.getsignal(signal.SIGTERM)
    with setup._viewer_signal_handlers(received.append):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        cast(Callable[[int, object], None], handler)(signal.SIGTERM, None)

    assert received == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.asyncio
async def test_viewer_signal_cancels_and_awaits_login_cleanup() -> None:
    import linkedin_mcp_server.setup as setup

    cleaned = False

    async def login() -> bool:
        nonlocal cleaned
        try:
            await asyncio.sleep(0)
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            cast(Callable[[int, object], None], handler)(signal.SIGTERM, None)
            await asyncio.Future()
            return True
        finally:
            cleaned = True

    with pytest.raises(setup._ViewerInterrupted) as interrupted:
        await setup._run_bounded_viewer_login(login())

    assert interrupted.value.signum == signal.SIGTERM
    assert cleaned is True


@pytest.mark.parametrize(
    ("login_timeout", "login_viewer", "expected_budget"),
    [
        (0, False, "no time limit"),
        (3600, False, "60 minutes"),
        (0, True, "30 minutes"),
        (600, True, "10 minutes"),
        (3600, True, "30 minutes"),
    ],
)
@pytest.mark.asyncio
async def test_login_prompt_reports_the_effective_viewer_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    login_timeout: float,
    login_viewer: bool,
    expected_budget: str,
) -> None:
    import linkedin_mcp_server.setup as setup

    class Manager:
        close_confirmed = True

    class Viewer:
        def start_window_manager(self) -> None:
            pass

        def stop_window_manager(self) -> None:
            pass

    async def login_succeeds(*_args: object, **_kwargs: object) -> bool:
        return True

    config = SimpleNamespace(
        browser=SimpleNamespace(login_timeout_seconds=login_timeout, slow_mo=0)
    )
    monkeypatch.setattr(setup, "build_launch_options", lambda _config: ({}, None))
    monkeypatch.setattr(setup, "describe_launch", lambda _options: None)
    monkeypatch.setattr(setup, "BrowserManager", lambda **_kwargs: Manager())
    monkeypatch.setattr(setup, "LoginViewer", Viewer)
    monkeypatch.setattr(setup, "_run_login", login_succeeds)

    assert await setup._login_into_fresh_profile(
        tmp_path / "profile",
        config=config,
        state=setup._LoginState(),
        login_viewer=login_viewer,
    )

    assert f"You have {expected_budget} to complete authentication" in (
        capsys.readouterr().out
    )


@pytest.mark.asyncio
async def test_a_viewer_that_never_starts_leaves_the_profile_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No browser was opened, so nothing may be held on its account.

    The window manager comes up before the browser does, and a login that ends
    there never reaches the manager at all. Its ``close_confirmed`` is then the
    pessimistic default every manager starts with, which reads exactly like a
    Chromium that would not shut down: the profile stays marked busy for the
    life of the process, over a browser that never ran.
    """
    import linkedin_mcp_server.setup as setup

    class Manager:
        # As constructed, and never opened.
        close_confirmed = False

    class Viewer:
        def start_window_manager(self) -> None:
            raise RuntimeError("openbox did not become ready")

        def stop_window_manager(self) -> None:
            pass

    async def never_reached(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("the login ran without a window manager")

    config = SimpleNamespace(
        browser=SimpleNamespace(login_timeout_seconds=60, slow_mo=0)
    )
    monkeypatch.setattr(setup, "build_launch_options", lambda _config: ({}, None))
    monkeypatch.setattr(setup, "describe_launch", lambda _options: None)
    monkeypatch.setattr(setup, "BrowserManager", lambda **_kwargs: Manager())
    monkeypatch.setattr(setup, "LoginViewer", Viewer)
    monkeypatch.setattr(setup, "_run_login", never_reached)

    state = setup._LoginState()
    with pytest.raises(RuntimeError, match="openbox"):
        await setup._login_into_fresh_profile(
            tmp_path / "profile",
            config=config,
            state=state,
            login_viewer=True,
        )

    assert state.close_confirmed is True


def test_viewer_hard_cap_applies_when_login_timeout_is_unlimited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import linkedin_mcp_server.setup as setup

    started = False

    async def never_finishes(*_args: object, **_kwargs: object) -> bool:
        nonlocal started
        started = True
        await asyncio.Future()
        return True

    monkeypatch.setattr(setup, "interactive_login", never_finishes)
    monkeypatch.setattr(setup, "VIEWER_WALL_SECONDS", 0.01)

    assert (
        setup.run_profile_creation(str(tmp_path / "profile"), login_viewer=True)
        is False
    )
    assert started is True
    assert "hard 0-second limit" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_deadline_closes_exposure_before_waiting_for_profile_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import linkedin_mcp_server.setup as setup

    exposure_closed = asyncio.Event()
    release_profile_cleanup = asyncio.Event()

    async def login() -> bool:
        try:
            await asyncio.Future()
        finally:
            # Models _run_login closing websockify and x11vnc before the outer
            # login flow restores the retired profile.
            exposure_closed.set()
            await release_profile_cleanup.wait()
        return False

    monkeypatch.setattr(setup, "VIEWER_WALL_SECONDS", 0.01)
    bounded_login = asyncio.create_task(setup._run_bounded_viewer_login(login()))

    await asyncio.wait_for(exposure_closed.wait(), timeout=0.2)
    assert bounded_login.done() is False

    release_profile_cleanup.set()
    with pytest.raises(TimeoutError):
        await bounded_login


@pytest.mark.asyncio
async def test_remote_teardown_precedes_chromium_and_preserves_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import linkedin_mcp_server.setup as setup

    events: list[str] = []

    class Manager:
        async def __aenter__(self) -> Any:
            events.append("chromium:start")
            return SimpleNamespace(page=object())

        async def __aexit__(self, *_args: object) -> None:
            events.append("chromium:stop")

    class Viewer:
        def start_remote_control(self) -> str:
            events.extend(("x11vnc:start", "websockify:start"))
            return viewer_url("token")

        def stop_remote_control(self) -> None:
            events.extend(("websockify:stop", "x11vnc:stop"))
            raise RuntimeError("teardown failed")

    async def launch_failed(*_args: object, **_kwargs: object) -> None:
        raise ValueError("navigation failed")

    monkeypatch.setattr(setup, "goto_reporting_proxy_errors", launch_failed)

    with pytest.raises(ValueError, match="navigation failed"):
        await setup._run_login(
            cast(setup.BrowserManager, Manager()),
            tmp_path / "profile",
            SimpleNamespace(),
            0,
            viewer=cast(LoginViewer, Viewer()),
        )

    assert capsys.readouterr().out.count(viewer_url("token")) == 1
    assert events == [
        "chromium:start",
        "x11vnc:start",
        "websockify:start",
        "websockify:stop",
        "x11vnc:stop",
        "chromium:stop",
    ]


@pytest.mark.asyncio
async def test_window_manager_teardown_does_not_mask_cookie_export_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import linkedin_mcp_server.setup as setup

    events: list[str] = []

    class Manager:
        close_confirmed = True

    class Viewer:
        def start_window_manager(self) -> None:
            events.append("openbox:start")

        def stop_window_manager(self) -> None:
            events.append("openbox:stop")
            raise RuntimeError("openbox teardown failed")

    async def cookie_export_failed(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(setup, "build_launch_options", lambda _config: ({}, None))
    monkeypatch.setattr(setup, "describe_launch", lambda _options: None)
    monkeypatch.setattr(setup, "BrowserManager", lambda **_kwargs: Manager())
    monkeypatch.setattr(setup, "LoginViewer", Viewer)
    monkeypatch.setattr(setup, "_run_login", cookie_export_failed)
    config = SimpleNamespace(
        browser=SimpleNamespace(login_timeout_seconds=1800, slow_mo=0)
    )
    state = setup._LoginState()

    result = await setup._login_into_fresh_profile(
        tmp_path / "profile", config=config, state=state, login_viewer=True
    )

    assert result is False
    assert state.close_confirmed is True
    assert events == ["openbox:start", "openbox:stop"]
    assert "Warning: viewer teardown failed: openbox teardown failed" in (
        capsys.readouterr().out
    )
