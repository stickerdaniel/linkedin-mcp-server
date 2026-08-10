"""Short-lived noVNC access to the Docker login browser."""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import quote

from linkedin_mcp_server.config.schema import DEFAULT_USER_DATA_DIR
from linkedin_mcp_server.session_state import canonical

VIEWER_WALL_SECONDS = 1800.0
VIEWER_PORT = 6080
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class LoginViewerError(RuntimeError):
    """The Docker login viewer could not be started safely."""


def _decode_mount_field(value: str) -> str:
    """Decode the octal escapes used by procfs mountinfo fields."""
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_points(mountinfo: str) -> tuple[Path, ...]:
    """Return canonical-looking mount points from Linux mountinfo text."""
    points: list[Path] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) >= 5 and "-" in fields:
            points.append(Path(_decode_mount_field(fields[4])))
    return tuple(points)


def require_persistent_profile_mount(
    profile_dir: Path,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Refuse a viewer login whose result would die with the container."""
    profile = canonical(profile_dir)
    try:
        points = mount_points(mountinfo_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LoginViewerError(
            f"Cannot verify the Docker profile mount from {mountinfo_path}: {exc}"
        ) from exc

    persistent = any(
        point != Path("/") and (point == profile or point in profile.parents)
        for point in points
    )
    if persistent:
        return

    default_profile = canonical(Path(DEFAULT_USER_DATA_DIR))
    if profile == default_profile:
        remedy = "-v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp"
    else:
        remedy = f"mount a host directory or named volume at {profile.parent}"
    raise LoginViewerError(
        "--login-viewer requires the configured profile to be on a distinct "
        f"persistent mount. Use {remedy} and try again."
    )


def viewer_url(token: str) -> str:
    """Build the token-private noVNC URL understood by Debian vnc_lite.html."""
    path = quote(f"websockify?token={token}", safe="")
    return f"http://127.0.0.1:{VIEWER_PORT}/vnc_lite.html#?path={path}&scale=true"


class LoginViewer:
    """Supervise Openbox and the two remote-control exposure layers."""

    def __init__(self, *, display: str | None = None) -> None:
        self.display = display or os.environ.get("DISPLAY", ":99")
        self._directory: Path | None = None
        self._token_file: Path | None = None
        self._logs: dict[str, Path] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def start_window_manager(self) -> None:
        """Start Openbox and wait until it owns the X11 root window."""
        self._ensure_directory()
        self._start("openbox", ["openbox", "--sm-disable"])
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            self._require_alive("openbox")
            result = subprocess.run(
                ["obxprop", "--root", "_NET_SUPPORTING_WM_CHECK"],
                env={**os.environ, "DISPLAY": self.display},
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return
            time.sleep(0.1)
        raise self._failure("openbox", "did not become ready")

    def start_remote_control(self) -> str:
        """Start loopback VNC and token-authenticated WebSocket access."""
        directory = self._ensure_directory()
        token = secrets.token_urlsafe(32)
        token_file = directory / "tokens"
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{token}: 127.0.0.1:5900\n")
        self._token_file = token_file

        try:
            self._start(
                "x11vnc",
                [
                    "x11vnc",
                    "-display",
                    self.display,
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
            )
            self._wait_for_port("x11vnc", 5900)
            self._start(
                "websockify",
                [
                    "websockify",
                    "--web=/usr/share/novnc",
                    "--token-plugin=ReadOnlyTokenFile",
                    f"--token-source={token_file}",
                    "0.0.0.0:6080",
                ],
            )
            self._wait_for_port("websockify", VIEWER_PORT)
        except BaseException:
            try:
                self.stop_remote_control()
            except Exception:
                # Preserve the launch/readiness error, whose component log is the
                # useful diagnosis. Final cleanup gets another attempt.
                pass
            raise
        return viewer_url(token)

    def stop_remote_control(self) -> None:
        """Remove network exposure in reverse order, attempting every layer."""
        failure: Exception | None = None
        for name in ("websockify", "x11vnc"):
            try:
                self._stop(name)
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure

    def stop_window_manager(self) -> None:
        """Stop Openbox and remove the per-run credential and logs."""
        failure: Exception | None = None
        try:
            self.stop_remote_control()
        except Exception as exc:
            failure = exc
        try:
            self._stop("openbox")
        except Exception as exc:
            failure = failure or exc
        finally:
            if self._token_file is not None:
                self._token_file.unlink(missing_ok=True)
                self._token_file = None
            if self._directory is not None:
                shutil.rmtree(self._directory, ignore_errors=True)
                self._directory = None
        if failure is not None:
            raise failure

    def _ensure_directory(self) -> Path:
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="linkedin-mcp-viewer-"))
            self._directory.chmod(0o700)
        return self._directory

    def _start(self, name: str, command: list[str]) -> None:
        log_path = self._ensure_directory() / f"{name}.log"
        self._logs[name] = log_path
        log = log_path.open("wb")
        try:
            try:
                process = subprocess.Popen(
                    command,
                    env={**os.environ, "DISPLAY": self.display},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            except OSError as exc:
                raise LoginViewerError(
                    f"Docker login viewer could not launch {name}: {exc}"
                ) from exc
        finally:
            log.close()
        self._processes[name] = process
        time.sleep(0.05)
        self._require_alive(name)

    def _wait_for_port(self, name: str, port: int) -> None:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            self._require_alive(name)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.1)
        raise self._failure(name, f"did not listen on 127.0.0.1:{port}")

    def _require_alive(self, name: str) -> None:
        process = self._processes[name]
        status = process.poll()
        if status is not None:
            raise self._failure(name, f"exited early with status {status}")

    def _failure(self, name: str, reason: str) -> LoginViewerError:
        log_path = self._logs.get(name)
        details = ""
        if log_path is not None:
            try:
                details = log_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
        suffix = f"\n{name} log:\n{details}" if details else ""
        return LoginViewerError(f"Docker login viewer {name} {reason}.{suffix}")

    def _stop(self, name: str) -> None:
        process = self._processes.pop(name, None)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
