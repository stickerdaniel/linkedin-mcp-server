"""The image's browser and process topology are part of its behaviour.

A Docker build is too expensive for the ordinary unit suite, and string checks
are enough for the pieces that can disappear silently: the browser product, the
launch mode, and who receives SIGTERM. The supervisor itself is exercised with
fake Xvfb and server processes, because child liveness is behaviour a string
check cannot prove.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
_ENTRYPOINT_PATH = _REPO_ROOT / "docker-entrypoint.sh"
_ENTRYPOINT = _ENTRYPOINT_PATH.read_text(encoding="utf-8")


def test_the_image_installs_only_the_full_browser() -> None:
    """The shell is a different product and nothing in the image launches it."""
    assert "patchright install chromium --no-shell" in _DOCKERFILE


def test_the_image_defaults_to_headed_on_a_virtual_display() -> None:
    assert "ENV HEADLESS=false" in _DOCKERFILE
    assert "ENV DISPLAY=:99" in _DOCKERFILE
    assert 'Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp' in _ENTRYPOINT


def test_tini_signals_the_whole_display_group() -> None:
    """Python must receive TERM before the PID namespace tears it down."""
    assert 'ENTRYPOINT ["tini", "-g"' in _DOCKERFILE
    assert "wait -n -p first_child" in _ENTRYPOINT
    assert 'kill -TERM "$server_pid" "$xvfb_pid"' in _ENTRYPOINT
    assert "xvfb-run" not in _DOCKERFILE


def test_the_entrypoint_waits_for_the_display_socket() -> None:
    """An Xvfb process existing does not mean its socket is ready yet."""
    assert "display_number=${BASH_REMATCH[1]}" in _ENTRYPOINT
    assert 'socket_path="/tmp/.X11-unix/X${display_number}"' in _ENTRYPOINT
    assert 'rm -f -- "$socket_path" "$lock_path"' in _ENTRYPOINT
    assert "$attempt -lt 100" in _ENTRYPOINT


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
def test_a_host_prefixed_display_is_refused_before_xvfb_starts() -> None:
    """The image owns a Unix-socket display and never starts a TCP X server."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")

    process = subprocess.run(
        [bash, str(_ENTRYPOINT_PATH), "/usr/bin/true"],
        env={**os.environ, "DISPLAY": "localhost:99.0"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert process.returncode == 2
    assert "DISPLAY must be a local X display such as :99 or :99.0" in process.stderr
    assert "Xvfb" not in process.stderr


@pytest.mark.skipif(os.name == "nt", reason="the image entrypoint is Bash on Linux")
def test_stale_x11_state_is_removed_and_xvfb_dying_stops_the_server(
    tmp_path: Path,
) -> None:
    """A hard-stopped container must restart and keep display liveness coupled.

    The stale socket and lock are what a SIGKILL leaves in the writable layer.
    Fake Xvfb refuses to start while either exists, then creates the real socket
    spelling for ``:N.0`` (``XN``) and dies cleanly. The supervisor must remove
    the stale state, notice the later death despite the child becoming a zombie,
    terminate the server, and return non-zero. ``kill -0`` keeps succeeding for
    a zombie, which is why waiting for either child is part of the contract.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to exercise the Linux image entrypoint")
    major = int(
        subprocess.check_output(
            [bash, "-c", 'printf "%s" "${BASH_VERSINFO[0]}"'], text=True
        )
    )
    if major < 5:
        pytest.skip("wait -n -p requires the Bash 5 shipped by the image")

    display_number = 10_000 + (os.getpid() % 10_000)
    display = f":{display_number}.0"
    socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
    lock_path = Path(f"/tmp/.X{display_number}-lock")
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    stale_socket = socket.socket(socket.AF_UNIX)
    stale_socket.bind(str(socket_path))
    stale_socket.close()
    lock_path.write_text("stale", encoding="utf-8")

    fake_xvfb = tmp_path / "Xvfb"
    fake_xvfb.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import pathlib
            import socket
            import sys
            import time

            number = sys.argv[1].removeprefix(":").split(".", 1)[0]
            path = pathlib.Path(f"/tmp/.X11-unix/X{number}")
            lock = pathlib.Path(f"/tmp/.X{number}-lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() or lock.exists():
                raise SystemExit(91)
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path))
            server.listen()
            lock.write_text("owned", encoding="utf-8")
            time.sleep(0.3)
            server.close()
            path.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
            """
        ),
        encoding="utf-8",
    )
    fake_xvfb.chmod(0o755)

    term_marker = tmp_path / "server-received-term"
    fake_server = tmp_path / "server"
    fake_server.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import pathlib
            import signal
            import time

            marker = pathlib.Path(os.environ["SERVER_TERM_MARKER"])

            def stop(*_args):
                marker.write_text("term", encoding="utf-8")
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, stop)
            while True:
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    fake_server.chmod(0o755)

    env = {
        **os.environ,
        "DISPLAY": display,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "SERVER_TERM_MARKER": str(term_marker),
    }
    process = subprocess.Popen(
        [bash, str(_ENTRYPOINT_PATH), str(fake_server)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Kill the whole fake process group, which is exactly what a bare
            # subprocess.run(timeout=...) fails to do. A broken supervisor must
            # not leave the fake server sleeping after the test has failed.
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    finally:
        socket_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)

    assert process.returncode != 0, (stdout, stderr)
    assert term_marker.read_text(encoding="utf-8") == "term"
