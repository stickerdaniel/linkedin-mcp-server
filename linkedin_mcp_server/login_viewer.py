"""Short-lived noVNC access to the Docker login browser."""

from __future__ import annotations

from dataclasses import dataclass
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
# A filesystem held in RAM answers every question a bind mount answers: it is a
# distinct mount, it is writable, and it is not the container root. It still
# loses the session when the container stops, which is the one thing the
# preflight exists to prevent.
_MEMORY_FILESYSTEMS = frozenset({"tmpfs", "ramfs"})


class LoginViewerError(RuntimeError):
    """The Docker login viewer could not be started safely."""


@dataclass(frozen=True)
class MountRecord:
    """One Linux mountinfo line, reduced to what the preflight judges."""

    point: Path
    filesystem: str


def _decode_mount_field(value: str) -> str:
    """Decode the octal escapes used by procfs mountinfo fields."""
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_records(mountinfo: str) -> tuple[MountRecord, ...]:
    """Parse Linux mountinfo text into mount points and filesystem types."""
    records: list[MountRecord] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        # Fields 0-5 are fixed, then a variable number of optional tags, then
        # the "-" separator with the filesystem type directly behind it.
        # Searching from index 6 keeps a mount point spelled "-" from being
        # mistaken for that separator.
        separator = next(
            (index for index in range(6, len(fields)) if fields[index] == "-"), None
        )
        if separator is None or separator + 1 >= len(fields):
            continue
        records.append(
            MountRecord(
                point=Path(_decode_mount_field(fields[4])),
                filesystem=fields[separator + 1],
            )
        )
    return tuple(records)


def mount_points(mountinfo: str) -> tuple[Path, ...]:
    """Return canonical-looking mount points from Linux mountinfo text."""
    return tuple(record.point for record in mount_records(mountinfo))


def _remedy(profile: Path) -> str:
    """Name the mount that would make this profile survive the container."""
    if profile == canonical(Path(DEFAULT_USER_DATA_DIR)):
        return "Use -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp and try again."
    return f"Mount a host directory or named volume at {profile.parent} and try again."


def require_persistent_profile_mount(
    profile_dir: Path,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Refuse a viewer login whose result would die with the container.

    The authentication root one level above the profile has to persist:
    ``cookies.json``, ``source-state.json`` and the retired sessions all live
    there, and a rotation moves the profile *within* that directory.
    """
    profile = canonical(profile_dir)
    auth_root = profile.parent
    try:
        records = mount_records(mountinfo_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LoginViewerError(
            f"Cannot verify the Docker profile mount from {mountinfo_path}: {exc}"
        ) from exc
    remedy = _remedy(profile)

    # A mount on the profile, or anywhere inside it, is worse than no mount at
    # all. Rotation moves that directory aside, and a mountpoint cannot be
    # moved: `shutil.move` falls back to copy-then-delete across devices, so the
    # session is duplicated and emptied before the rename fails with EBUSY.
    for record in records:
        if record.point == profile or profile in record.point.parents:
            raise LoginViewerError(
                f"--login-viewer cannot rotate the profile: {record.point} is "
                "itself a mount point, and retiring a session moves that "
                "directory. Mount the authentication root "
                f"{auth_root} instead, so the profile can move inside it. "
                f"{remedy}"
            )

    # The nearest covering mount is the one the authentication root lives on. An
    # ancestor further up describes a different filesystem once something closer
    # is mounted over it, so it cannot answer for this directory.
    covering = [
        record
        for record in records
        if record.point == auth_root or record.point in auth_root.parents
    ]
    boundary = max(covering, key=lambda record: len(record.point.parts), default=None)
    if boundary is None or boundary.point == Path("/"):
        raise LoginViewerError(
            "--login-viewer requires the configured profile to be on a distinct "
            f"persistent mount, but {auth_root} is part of the container "
            f"filesystem and is discarded with it. {remedy}"
        )
    if boundary.filesystem in _MEMORY_FILESYSTEMS:
        raise LoginViewerError(
            "--login-viewer requires the configured profile to be on a distinct "
            f"persistent mount, but {boundary.point} is a {boundary.filesystem} "
            f"filesystem held in memory and discarded with the container. "
            f"{remedy}"
        )
    _require_a_writable_auth_root(auth_root, profile=profile)


def _require_a_writable_auth_root(auth_root: Path, *, profile: Path) -> None:
    """Refuse before rotation when the persistent root cannot be written.

    Docker seeds a named volume from the image, so a volume for a path the image
    never created arrives as ``root:root``. So does a host directory that an
    earlier rootful ``docker run`` created. Either way the unprivileged runtime
    only finds out on its first write, which happens after the previous session
    has already been moved aside.
    """
    existing = auth_root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_dir() and os.access(existing, os.W_OK | os.X_OK):
        return

    identity = ""
    repair = "give the mounted host directory to the container user"
    if hasattr(os, "geteuid"):
        identity = f" by uid {os.geteuid()}"
        repair = f"sudo chown -R {os.geteuid()}:{os.getegid()} <host directory>"
    if profile == canonical(Path(DEFAULT_USER_DATA_DIR)):
        create = "Create it first with mkdir -p ~/.linkedin-mcp"
    else:
        create = "Create the mounted host directory before starting the container"
    raise LoginViewerError(
        f"--login-viewer cannot write the authentication root {auth_root}: "
        f"{existing} is not writable{identity}. {create}, or repair a root-owned "
        f"one with {repair}."
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
