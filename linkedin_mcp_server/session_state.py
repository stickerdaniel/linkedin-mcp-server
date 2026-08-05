"""Runtime-aware authentication state for cross-platform profile reuse."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
import functools
import json
import logging
import os
import platform
from pathlib import Path
import re
import shutil
import socket
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

from linkedin_mcp_server.common_utils import (
    secure_mkdir,
    secure_write_text,
    utcnow_iso,
)
from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)

_SOURCE_STATE_FILE = "source-state.json"
_RUNTIME_STATE_FILE = "runtime-state.json"
_RUNTIME_PROFILES_DIR = "runtime-profiles"

# Prefix of the timestamped directories retired auth state is moved into.
QUARANTINE_PREFIX = "invalid-state-"

# Chromium writes a profile it owns three Singleton* links and removes them on a
# clean exit. Only this one encodes the owner as ``<hostname>-<pid>``; the
# siblings hold a socket path and an opaque token, so they cannot be attributed
# and are ignored. A crash leaves the link behind, so presence alone proves
# nothing — see ``profile_in_use_by``.
_CHROMIUM_LOCK_NAME = "SingletonLock"


@dataclass
class SourceState:
    version: int
    source_runtime_id: str
    login_generation: str
    created_at: str
    profile_path: str
    cookies_path: str


@dataclass
class RuntimeState:
    version: int
    runtime_id: str
    source_runtime_id: str
    source_login_generation: str
    created_at: str
    committed_at: str
    profile_path: str
    storage_state_path: str
    commit_method: str


_SOURCE_STATE_FIELDS = frozenset(field.name for field in fields(SourceState))
_RUNTIME_STATE_FIELDS = frozenset(field.name for field in fields(RuntimeState))


def canonical(profile_dir: Path) -> Path:
    """Expand and resolve one profile path, the only way this module spells it.

    Both halves, everywhere, and the pairing is what was missing. Expanding
    without resolving and resolving without expanding used to sit side by side
    here: ``get_source_profile_dir`` did the first, ``auth_root_dir`` the
    second. An ordinary relative path survives that split, because it denotes
    the same object as its ``resolve()`` for as long as the working directory
    holds. **A symlink does not.** ``shutil.move`` relocates the link itself
    while the sidecars are computed from the target's parent, so a rotation
    would move the profile out of one directory and its cookies out of another,
    and afterwards the same name would resolve against its lexical parent
    instead. One session, split across two roots, with no error anywhere.
    """
    return profile_dir.expanduser().resolve()


def get_source_profile_dir() -> Path:
    """Return the configured source profile directory."""
    return canonical(Path(get_config().browser.user_data_dir))


def auth_root_dir(source_profile_dir: Path | None = None) -> Path:
    """Return the root directory containing auth artifacts."""
    profile_dir = source_profile_dir or get_source_profile_dir()
    return canonical(profile_dir).parent


def portable_cookie_path(source_profile_dir: Path | None = None) -> Path:
    """Return the portable cookie export path."""
    return auth_root_dir(source_profile_dir) / "cookies.json"


def source_state_path(source_profile_dir: Path | None = None) -> Path:
    """Return the source session metadata path."""
    return auth_root_dir(source_profile_dir) / _SOURCE_STATE_FILE


def runtime_profiles_root(source_profile_dir: Path | None = None) -> Path:
    """Return the root directory for derived runtime profiles."""
    return auth_root_dir(source_profile_dir) / _RUNTIME_PROFILES_DIR


def runtime_dir(runtime_id: str, source_profile_dir: Path | None = None) -> Path:
    """Return the directory for one runtime's derived session."""
    return runtime_profiles_root(source_profile_dir) / runtime_id


def runtime_profile_dir(
    runtime_id: str, source_profile_dir: Path | None = None
) -> Path:
    """Return the profile directory for one runtime's derived session."""
    return runtime_dir(runtime_id, source_profile_dir) / "profile"


def runtime_state_path(runtime_id: str, source_profile_dir: Path | None = None) -> Path:
    """Return the metadata path for one runtime's derived session."""
    return runtime_dir(runtime_id, source_profile_dir) / _RUNTIME_STATE_FILE


def runtime_storage_state_path(
    runtime_id: str, source_profile_dir: Path | None = None
) -> Path:
    """Return the storage-state snapshot path for one runtime's derived session."""
    return runtime_dir(runtime_id, source_profile_dir) / "storage-state.json"


def profile_exists(profile_dir: Path | None = None) -> bool:
    """Check if a browser profile directory exists and is non-empty."""
    profile_dir = canonical(profile_dir or get_source_profile_dir())
    return profile_dir.is_dir() and any(profile_dir.iterdir())


def get_runtime_id() -> str:
    """Return a deterministic identity for the current browser runtime."""
    os_name = _normalize_os(platform.system())
    arch = _normalize_arch(platform.machine())
    runtime_kind = "container" if _is_container_runtime() else "host"
    return f"{os_name}-{arch}-{runtime_kind}"


def _normalize_os(system: str) -> str:
    mapping = {
        "Darwin": "macos",
        "Linux": "linux",
        "Windows": "windows",
    }
    return mapping.get(system, system.lower() or "unknown")


def _normalize_arch(machine: str) -> str:
    value = machine.lower()
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value or "unknown"


def _is_container_runtime() -> bool:
    """Whether *this* process runs inside a container.

    Every signal here has to describe our own process. That sounds obvious and
    is exactly what an earlier version got wrong: it searched ``mountinfo`` and
    ``cgroup`` for the substrings ``docker``, ``containerd`` and friends
    anywhere in the file. Those files list what the *namespace* can see, not
    what we are, so an ordinary Linux workstation running a Docker daemon for
    unrelated services — a local Postgres, a Redis — matched every time. The
    misdetection was permanent and unrecoverable: it sends
    ``get_runtime_policy`` to DOCKER, which answers every tool call with "run
    --login on the host machine" no matter how valid the session on disk is.

    A false negative is the worse failure, so the remaining signals are the
    conservative ones rather than the clever ones: a container that believed it
    was a host would try to auto-import from a browser keychain that is not
    there and offer login flows it cannot run.

    Deliberately *not* consulted: ``/run/systemd/container``, systemd's
    documented container interface. It answers a different question than this
    one. An OrbStack Linux machine reports ``lxc`` there, yet it is a full
    system with its own systemd, a persistent disk and a desktop-class user —
    everything the DOCKER policy assumes is missing. Trusting the marker
    classified it as a container and put it straight back into "run --login on
    the host machine", which is this bug wearing a different hat. What this
    module needs to know is not "is there a boundary" but "is there a browser
    and a keychain on the other side of it", and no single flag answers that.
    LXC and nspawn therefore stay reachable only through their cgroup layout,
    and where that is not enough, ``LINKEDIN_MCP_CONTAINER=true`` is.

    ``LINKEDIN_MCP_CONTAINER`` overrides the whole thing. Detection is a
    heuristic over other people's kernels, so it will be wrong somewhere, and
    without an override being wrong means editing installed source: the
    misdetection blocks every tool call and no flag reaches it.
    """
    override = _container_override()
    if override is not None:
        return override

    if any(
        path.exists()
        for path in (
            Path("/.dockerenv"),
            Path("/run/.containerenv"),
            Path("/run/containerenv"),
        )
    ):
        return True

    for probe in _CGROUP_PROBES:
        if _cgroup_path_is_containerised(probe):
            return True

    for probe in _MOUNTINFO_PROBES:
        if _root_mount_uses_overlay(probe):
            return True

    return False


#: Escape hatch for a machine this detection gets wrong. Spelled the same way
#: as every other boolean environment variable this server reads
#: (``config/loaders.py``), but read here rather than through the config
#: layer: runtime identity is resolved before a configuration exists, and
#: importing the loaders would close a cycle.
_CONTAINER_OVERRIDE_ENV = "LINKEDIN_MCP_CONTAINER"
_OVERRIDE_TRUE = ("1", "true", "yes", "on")
_OVERRIDE_FALSE = ("0", "false", "no", "off")

#: Named rather than inlined so a test can point the decision at a fixture.
#: Both pid 1 and self are read: a process can be in a different namespace than
#: init, and either being containerised is enough.
_CGROUP_PROBES = (Path("/proc/1/cgroup"), Path("/proc/self/cgroup"))
_MOUNTINFO_PROBES = (Path("/proc/1/mountinfo"), Path("/proc/self/mountinfo"))


def _container_override() -> bool | None:
    """An explicit answer from the operator, or None to keep detecting."""
    raw = os.environ.get(_CONTAINER_OVERRIDE_ENV)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _OVERRIDE_TRUE:
        return True
    if value in _OVERRIDE_FALSE:
        return False
    # An unreadable value is not a decision. Falling through to detection beats
    # guessing, and beats crashing at import time over an environment variable.
    logger.warning(
        "Ignoring %s=%r: expected one of %s",
        _CONTAINER_OVERRIDE_ENV,
        raw,
        ", ".join(_OVERRIDE_TRUE + _OVERRIDE_FALSE),
    )
    return None


#: Whole cgroup path segments that mean a container runtime owns this process.
#: Matched as segments rather than substrings, which is the entire point: a
#: systemd unit called ``app-docker\x2ddesktop.scope`` on an ordinary desktop
#: contains "docker" and is not a container.
_CONTAINER_CGROUP_SEGMENTS = frozenset(
    {
        "docker",
        "containerd",
        "kubepods",
        "kubepods.slice",
        "podman",
        "machine",
        # containerd's default namespace, which is what a plain `ctr run` and
        # anything built on moby writes.
        "moby",
    }
)

#: Runtimes that name the cgroup segment after the container instance, so there
#: is no bare segment to match. The identifier is required, not just the
#: prefix: ``docker-backup.scope`` is a perfectly ordinary host service, and an
#: earlier version of this read it as a container. Measured on a real host.
#:
#: 32 hex minimum rather than a token length. These runtimes write a full
#: container id — measured at 64 characters for a Docker systemd scope — while
#: a service someone names by hand does not reach that even when every letter
#: happens to be a-f. ``docker-beefcafedeadbeef.scope`` is contrived but would
#: pass a shorter bound.
_CONTAINER_CGROUP_INSTANCE = re.compile(
    r"^(?:libpod-|libpod_|crio-|docker-|containerd-)[0-9a-f]{32,}$"
)

#: LXC and systemd-nspawn name the segment after the container, so there is no
#: id to require. The *prefix* is the runtime's, though, and a host does not
#: write it: ``lxc.payload.<name>`` and ``machine-<name>.scope`` under
#: ``machine.slice``. Kept separate from the id regex because these carry
#: arbitrary user text after the prefix.
_CONTAINER_CGROUP_NAMED = ("lxc.payload.", "lxc.monitor.", "machine-")


def _cgroup_path_is_containerised(path: Path) -> bool:
    """Whether our own cgroup path sits under a container runtime's hierarchy.

    A cgroup line is ``hierarchy:controllers:path``. Only the path describes
    where this process sits; the rest is the kernel's bookkeeping. Reading the
    whole line as text is what let an unrelated controller name pass as
    evidence.

    Two host cases have to keep reading as *not* a container, and both are
    ordinary:

    * ``0::/system.slice/docker.service`` — the Docker daemon itself. It is a
      normal service on a normal host, and it is the very machine that has
      other containers' mounts visible.
    * ``app-docker\\x2ddesktop.scope`` — systemd escapes dashes, so a desktop
      app's unit contains the word.
    """
    if not path.exists():
        return False

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    for line in text.splitlines():
        fields = line.split(":", maxsplit=2)
        if len(fields) < 3:
            continue
        for raw in fields[2].split("/"):
            segment = raw.replace("\\x2d", "-").lower()
            # A unit is a host service, not a container: docker.service is the
            # daemon that *runs* containers.
            if segment.endswith((".service", ".socket", ".mount")):
                continue
            segment = segment.removesuffix(".scope")
            if segment in _CONTAINER_CGROUP_SEGMENTS:
                return True
            if _CONTAINER_CGROUP_INSTANCE.match(segment):
                return True
            if segment.startswith(_CONTAINER_CGROUP_NAMED):
                return True

    return False


#: Directory layouts a container runtime creates for a rootfs it hands out.
#: Compared against the *mount root* — the subtree of the device that got
#: mounted, which the kernel reports — and never against the mount source. The
#: source is a label the other end chooses: an NFS server exporting
#: ``nas:/var/lib/containers/workstations/alice`` describes somebody's laptop,
#: not a container, and reading it turned a legitimate host into the very
#: misdetection this module exists to prevent.
_CONTAINER_ROOT_LAYOUTS = (
    "/var/lib/docker/",
    "/var/lib/containerd/",
    "/var/lib/containers/",
    "/var/lib/rancher/",
    "/run/containerd/",
    # LXC and LXD. Measured on LXC 5.0.3: the advertised ``lxc.payload.<name>``
    # cgroup prefix does not appear when the host is itself containerised —
    # both the outer machine and the nested container read ``0::/.lxc`` — but
    # the kernel's mount root still separates them, because only the nested one
    # is rooted under the container's rootfs.
    "/var/lib/lxc/",
    "/var/lib/lxd/",
    "/var/snap/lxd/",
)

#: A remote filesystem is somebody else's namespace by definition, so its paths
#: say nothing about ours.
_NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "afs", "ceph", "fuse.sshfs", "9p", "virtiofs"}
)


def _root_mount_uses_overlay(path: Path) -> bool:
    """Whether our own ``/`` was assembled by a container runtime.

    Named for the common case, but overlay is not the only shape. A containerd
    container using the ``native`` snapshotter gets a plain bind mount from
    ``/var/lib/containerd/...`` on whatever filesystem the host uses — btrfs in
    the case this was measured on — so a type check alone reads it as a host.
    That is a false negative, and those are the dangerous direction: the
    container would then look for a browser keychain that is not there.

    Everything is read from the ``/`` line, and from the fields on it the
    kernel controls: the filesystem type and the mount root.
    """
    if not path.exists():
        return False

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False

    for line in lines:
        if " - " not in line:
            continue
        left, right = line.split(" - ", maxsplit=1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        if left_fields[4] != "/":
            continue

        fstype = right_fields[0].lower()
        if fstype == "overlay":
            return True
        if fstype in _NETWORK_FILESYSTEMS:
            continue
        mount_root = left_fields[3].lower()
        if any(layout in mount_root for layout in _CONTAINER_ROOT_LAYOUTS):
            return True

    return False


def load_source_state(source_profile_dir: Path | None = None) -> SourceState | None:
    """Load the source session metadata if present."""
    data = _load_json(source_state_path(source_profile_dir))
    if not data:
        return None
    try:
        return SourceState(
            **{key: value for key, value in data.items() if key in _SOURCE_STATE_FIELDS}
        )
    except TypeError:
        logger.warning("Ignoring invalid source-state.json")
        return None


def write_source_state(source_profile_dir: Path | None = None) -> SourceState:
    """Write a fresh source session generation after successful login.

    State files written before the user-agent override was removed still carry
    a ``user_agent`` key. :func:`load_source_state` filters to the fields the
    dataclass declares, so those files keep loading and shed the key the next
    time this runs.
    """
    profile_dir = canonical(source_profile_dir or get_source_profile_dir())
    state = SourceState(
        version=1,
        source_runtime_id=get_runtime_id(),
        login_generation=str(uuid4()),
        created_at=utcnow_iso(),
        profile_path=str(profile_dir),
        cookies_path=str(portable_cookie_path(profile_dir)),
    )
    _write_json(source_state_path(profile_dir), asdict(state))
    return state


def load_runtime_state(
    runtime_id: str, source_profile_dir: Path | None = None
) -> RuntimeState | None:
    """Load one derived runtime's metadata if present."""
    data = _load_json(runtime_state_path(runtime_id, source_profile_dir))
    if not data:
        return None
    try:
        return RuntimeState(
            **{
                key: value
                for key, value in data.items()
                if key in _RUNTIME_STATE_FIELDS
            }
        )
    except TypeError:
        logger.warning("Ignoring invalid runtime-state.json for %s", runtime_id)
        return None


def write_runtime_state(
    runtime_id: str,
    source_state: SourceState,
    storage_state_path: Path,
    source_profile_dir: Path | None = None,
    *,
    created_at: str | None = None,
    commit_method: str = "checkpoint_restart",
) -> RuntimeState:
    """Write metadata for a derived runtime session."""
    profile_dir = runtime_profile_dir(runtime_id, source_profile_dir).resolve()
    committed_at = utcnow_iso()
    state = RuntimeState(
        version=1,
        runtime_id=runtime_id,
        source_runtime_id=source_state.source_runtime_id,
        source_login_generation=source_state.login_generation,
        created_at=created_at or committed_at,
        committed_at=committed_at,
        profile_path=str(profile_dir),
        storage_state_path=str(storage_state_path.resolve()),
        commit_method=commit_method,
    )
    _write_json(runtime_state_path(runtime_id, source_profile_dir), asdict(state))
    return state


def _owned(source_profile_dir: Path | None) -> Path:
    """The source root, canonical, once it has proved this server owns it.

    Every operation below that moves or deletes calls this on the *source* root
    it was handed, never on a target it derived. A derived runtime profile is
    computed from the source root and is therefore downstream of a check that
    already passed; re-checking it there would ask about
    ``<auth-root>/runtime-profiles/<runtime>``, a nested root nobody claims, and
    would refuse the container.

    Raises:
        ProfileRootRefusedError: The root is neither the canonical default nor
            claimed. Nothing has been touched when this is raised, which is the
            reason it is called before any exists-or-empty short-circuit rather
            than after.
    """
    from linkedin_mcp_server.profile_claim import require_profile_claim

    return require_profile_claim(source_profile_dir or get_source_profile_dir())


def clear_runtime_profile(
    runtime_id: str, source_profile_dir: Path | None = None
) -> bool:
    """Remove one derived runtime profile and its metadata.

    Raises like its siblings, and the one caller that must not see a raise
    swallows it there rather than here. Two call sites want opposite things: the
    teardown of a failed bridge is cleanup, where an ownership complaint would
    replace the failure already in flight, while the clear at the *start* of a
    bridge is a precondition — continuing past it would import cookies over a
    derived profile from an earlier login generation, which is the profile
    mixing this module exists to prevent. Reporting ``False`` from here served
    the first and quietly broke the second.
    """
    source_profile_dir = _owned(source_profile_dir)
    target = runtime_dir(runtime_id, source_profile_dir)
    if not target.exists():
        return True
    try:
        shutil.rmtree(target)
        return True
    except OSError as exc:
        logger.warning("Could not clear runtime profile %s: %s", target, exc)
        return False


def _auth_state_targets(profile_dir: Path) -> list[Path]:
    """The four artifacts that together make up one source session."""
    return [
        profile_dir,
        portable_cookie_path(profile_dir),
        source_state_path(profile_dir),
        runtime_profiles_root(profile_dir),
    ]


def quarantine_dirs(source_profile_dir: Path | None = None) -> list[Path]:
    """Existing quarantine directories, newest name last."""
    root = auth_root_dir(source_profile_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob(f"{QUARANTINE_PREFIX}*") if path.is_dir())


async def run_deferring_cancels(
    work: Callable[[], Any],
) -> tuple[Any, bool]:
    """Run *work* in a worker thread, holding back cancels until it finishes.

    Uses ``run_in_executor`` rather than ``to_thread``: the latter registers an
    ``asyncio.Task``, which ``asyncio.run`` cancels along with everything else
    at loop teardown. Shielding an already-cancelled *task* re-raises forever,
    so the wait below would spin and the process would never exit. A bare
    Future is not in ``all_tasks()`` and never reaches that state.

    Returns the result and whether a cancel arrived, so the caller can finish
    cleaning up and re-raise once at the end.
    """
    future = asyncio.get_running_loop().run_in_executor(None, work)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(future), cancelled
        except asyncio.CancelledError:
            # A cancel here must not abandon the worker: it is already moving
            # the session, and dropping its result strands the user logged out.
            cancelled = True


async def rotate_shielded(source_profile_dir: Path) -> Path | None:
    """Rotate off the event loop without losing the backup path to a cancel.

    A bare ``await asyncio.to_thread(rotate...)`` is cancellable, and a cancel
    lands *after* the thread has already moved the session: the move stands, but
    its return value is gone, so nothing can put it back. Overlapping cancels
    are real here — a tool timeout racing a server shutdown — so every one of
    them is deferred until the session is safely accounted for.
    """
    retired, cancelled = await run_deferring_cancels(
        functools.partial(rotate_source_profile, source_profile_dir)
    )
    if not cancelled:
        return retired

    if retired is not None:
        restored, _ = await run_deferring_cancels(
            functools.partial(restore_source_profile, retired, source_profile_dir)
        )
        if not restored:
            logger.warning(
                "Rotation was cancelled and the previous session could not be "
                "restored; it is kept at %s",
                retired,
            )
    raise asyncio.CancelledError


def _runtime_profile_dirs(source_profile_dir: Path) -> list[Path]:
    """Every derived runtime profile under the auth root."""
    root = runtime_profiles_root(source_profile_dir)
    if not root.is_dir():
        return []
    return [item / "profile" for item in root.iterdir() if (item / "profile").is_dir()]


def profile_in_use_by(profile_dir: Path) -> Path | None:
    """The Chromium lock proving another *live* process owns *profile_dir*.

    Returns ``None`` when the profile is free, including when a lock is left
    over from a crash: Chromium does not clean these up on an abnormal exit, and
    treating a stale one as an owner would wedge every future login behind a
    manual file deletion.

    On Linux and macOS the lock is a symlink whose target encodes the owning
    ``<host>-<pid>``. The pid is only meaningful in that host's namespace, so it
    is probed only when the host matches ours. A lock from a *different* host —
    a container writing into the mounted auth root, most often — is treated as
    held: its pid says nothing to us, and the alternative is moving a profile
    out from under a running container. That errs toward refusing to rotate,
    which the operator can resolve by stopping the container, whereas the
    opposite corrupts two sessions silently.
    """
    candidate = profile_dir / _CHROMIUM_LOCK_NAME
    try:
        target = os.readlink(candidate)
    except OSError:
        # Not a symlink, or absent: no attributable owner.
        return None

    owner, separator, pid_text = target.rpartition("-")
    if not separator:
        return None  # Not the documented shape; nothing to attribute.
    if owner != socket.gethostname():
        return candidate  # Another host: unverifiable, so assume live.
    try:
        pid = int(pid_text)
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # Stale: the writer is gone.
    except PermissionError:
        return candidate  # Alive, owned by another user.
    except OSError:
        return None
    return candidate


@contextmanager
def _exclusive_profile(profile_dir: Path, *, action: str) -> Iterator[None]:
    """Hold the profile exclusively for the duration of an auth-state mutation.

    Checking and then releasing before the move would leave a window in which
    another process launches Chromium against the very files being moved, so the
    lease is held until the mutation finishes.

    Three independent signals, because none alone is sufficient:

    * This process's own browser. The lease is reference-counted, so asking it
      for another reference would simply succeed and prove nothing about whether
      our Chromium is still running — the flag is what answers that. It matters
      most when a close could not be confirmed: the lease is deliberately kept in
      that case because Chromium may still be alive.
    * The lease itself, which every cooperating process takes before it opens
      Chromium. Authoritative, but only among processes that know about it.
    * Chromium's ``SingletonLock``, which catches a foreign holder — an older
      version, a container, a human with a browser open on the directory. Note it
      is written only by full Chrome: the default ``chrome-headless-shell`` never
      writes one, which is precisely why the lease exists.

    Every profile is checked, not just the source: a container runs Chromium out
    of ``runtime-profiles/<runtime>/profile`` while sharing the mounted auth
    root, so checking only the source would move a live container's profile out
    from under it.
    """
    from linkedin_mcp_server.profile_lease import get_profile_lease

    lease = get_profile_lease(profile_dir)
    if lease.browser_open:
        raise RuntimeError(
            "This server still has a browser open on the profile. "
            f"Close it before {action}."
        )
    if not lease.try_acquire():
        raise RuntimeError(
            "The browser profile is in use by another process. "
            f"Stop the running server or container before {action}."
        )

    try:
        lock = next(
            (
                held
                for candidate in [profile_dir, *_runtime_profile_dirs(profile_dir)]
                if (held := profile_in_use_by(candidate)) is not None
            ),
            None,
        )
        if lock is not None:
            raise RuntimeError(
                f"The browser profile is in use by another process (found {lock.name}). "
                f"Stop the running server or container before {action}."
            )
        yield
    finally:
        lease.release()


#: Distinguishes "no generation was observed" from "nothing asked for a guard".
#: ``None`` cannot do both: a profile with no session reads as ``None``, and two
#: clients meeting that state need protecting from each other exactly as much as
#: two meeting a stale one. Measured with the two conflated: the second client
#: rotated away the session the first had just created.
UNGUARDED = object()


class PeerSessionInPlaceError(RuntimeError):
    """A peer replaced the session this rotation was asked to retire.

    Raised rather than returned as ``None``, which already means "there was
    nothing to retire". The two look identical to a caller and are not: one is a
    no-op, the other means somebody else has done the work and this caller should
    stop rather than carry on as though it had. Measured with them conflated: the
    caller went on to promise a login window that never opened.
    """


def a_peer_already_signed_in(
    user_data_dir: Path, superseded_by: str | None | object
) -> bool:
    """Whether a usable session other than *superseded_by* is on disk now.

    Asked by every path that rotates the profile to make room for a new session,
    which is the login here and the browser import next door, and asked only once
    that path holds the profile lease. That is the first moment the answer cannot
    change underneath it.

    Both halves are needed. A different generation alone is not enough: an
    abandoned attempt leaves the profile rotated and no generation at all, which
    also reads as different, and standing down there would mean nobody ever signs
    in. A usable session alone is not enough either, because the dead one still
    looks usable from disk: readiness asks whether the files are there, not
    whether LinkedIn still accepts them.

    Together they mean what is wanted, because a generation is written only after
    a login or import has validated its session and exported the cookies.
    """
    from linkedin_mcp_server.bootstrap import _auth_ready
    from linkedin_mcp_server.session_state import load_source_state

    state = load_source_state(user_data_dir)
    if state is None or state.login_generation == superseded_by:
        return False
    # The same directory the generation came from. Asking about the configured
    # one instead would answer a different question than the one asked, and
    # quietly: a caller handed an explicit profile would get the default
    # profile's verdict while rotating theirs.
    return _auth_ready(user_data_dir)


def rotate_source_profile(
    source_profile_dir: Path | None = None,
    *,
    superseded_by: str | None | object = UNGUARDED,
) -> Path | None:
    """Retire the current source session so the next one starts clean.

    Chromium mints ``machine_id``, ``session_id_generator_last_value`` and
    friends into ``Local State`` once and then keeps them for the life of the
    profile. Reusing the directory for a different LinkedIn account hands
    LinkedIn the same device identity twice, which is exactly the signal that
    links accounts to one another. Every path that establishes a new source
    session therefore rotates first.

    The retired artifacts are moved, not deleted, so a session that turns out to
    have been fine is still recoverable. ``--logout`` clears the quarantines.

    *superseded_by* is the generation the caller found broken. Given one, this
    retires nothing if a *different* usable session is on disk by the time the
    profile is held, because somebody else has replaced it in the meantime.

    The comparison belongs here rather than at the call site, and that is the
    whole point of the argument. A caller deciding first and rotating afterwards
    leaves a gap: measured, a peer completing a login inside it had its fresh
    session quarantined by a process acting on a view formed before it existed.
    Under the lease below, the answer cannot change between reading it and acting
    on it.

    Returns the quarantine directory, or ``None`` when there was nothing to
    retire.

    Raises:
        ProfileRootRefusedError: The configured root is not one this server owns,
            so nothing was moved. Checked before the no-op return above, because
            a foreign directory with none of our artifacts in it reaches that
            return without ever being judged.
        PeerSessionInPlaceError: Another client signed in first, so its session
            is on disk and this rotation must not touch it. Distinct from the
            ``None`` above, and the distinction is load-bearing: both mean "did
            not rotate", but only one of them means a usable session now exists.
            Conflated, the caller promised a login window it never opened.
        RuntimeError: Another process holds the profile. Rotating underneath a
            live Chromium corrupts both the old and the new session.
        OSError: A move failed. Whatever had already moved is put back first, so
            the caller always sees either the old session intact or a complete
            retirement, never one session split across both.
    """
    profile_dir = _owned(source_profile_dir)
    existing = [
        target for target in _auth_state_targets(profile_dir) if target.exists()
    ]
    if not existing:
        return None

    with _exclusive_profile(profile_dir, action="creating a new session"):
        if superseded_by is not UNGUARDED and a_peer_already_signed_in(
            profile_dir, superseded_by
        ):
            logger.info(
                "Another client already signed in; leaving its session in place"
            )
            raise PeerSessionInPlaceError(
                "Another LinkedIn MCP client has already signed in. Retry this "
                "tool to use its session."
            )

        # utcnow_iso() is second-resolution and rotation is now routine rather
        # than exceptional, so two rotations can land in the same second. The
        # suffix keeps them from merging into one directory.
        stamp = utcnow_iso().replace(":", "-")
        backup_dir = (
            auth_root_dir(profile_dir) / f"{QUARANTINE_PREFIX}{stamp}-{uuid4().hex[:8]}"
        )
        secure_mkdir(backup_dir)
        moved: list[Path] = []
        try:
            for target in existing:
                shutil.move(str(target), str(backup_dir / target.name))
                moved.append(target)
        except OSError:
            _restore(backup_dir, moved)
            raise
        logger.info("Retired previous session to %s", backup_dir)
        return backup_dir


def _restore(backup_dir: Path, targets: list[Path]) -> None:
    """Move *targets* back out of *backup_dir*, best effort."""
    for target in targets:
        try:
            shutil.move(str(backup_dir / target.name), str(target))
        except OSError as exc:
            logger.warning("Could not restore %s: %s", target, exc)
    try:
        backup_dir.rmdir()
    except OSError:
        pass


def restore_source_profile(
    backup_dir: Path, source_profile_dir: Path | None = None
) -> bool:
    """Put a retired session back, undoing ``rotate_source_profile``.

    A rotation happens *before* the replacement session exists, so a login that
    is cancelled or an import where every candidate is rejected would otherwise
    leave the user logged out of a session that was working. Callers restore on
    that path, passing the same directory they rotated: ``--user-data-dir`` can
    point somewhere other than the configured default, and restoring to the
    configured one would strand the artifacts in a foreign auth root.

    Like rotation, this moves the active auth artifacts, so it holds the profile
    exclusively while it does. Every caller already owns the lease (login,
    import, and the cancelled-rotation path), which is re-entrant within a
    process; the guard is here so a future caller cannot forget.

    Returns ``False`` when the active paths are already occupied — the
    replacement succeeded after all, and overwriting it would be the very
    fingerprint mixing this module exists to prevent.

    Raises:
        ProfileRootRefusedError: Either the root is not ours, or *backup_dir* is
            not one of our quarantines directly inside it. Both are checked, and
            the second matters on its own: this reads whatever *backup_dir*
            contains and moves the matching names into the live session, so an
            unchecked one is a way to write a foreign directory's contents into
            an owned root.
    """
    profile_dir = _owned(source_profile_dir)
    backup_dir = _our_quarantine(backup_dir, profile_dir)
    if not backup_dir.is_dir():
        return False
    with _exclusive_profile(profile_dir, action="restoring the previous session"):
        return _restore_source_profile_locked(backup_dir, profile_dir)


def _our_quarantine(backup_dir: Path, profile_dir: Path) -> Path:
    """Check *backup_dir* is a quarantine this server made in *profile_dir*'s root.

    Name and location, not just location. ``_restore_source_profile_locked``
    also parks debris at ``<backup_dir>-superseded`` and finally ``rmdir``s the
    directory, so anything accepted here is written beside as well as read from.
    """
    from linkedin_mcp_server.exceptions import ProfileRootRefusedError

    candidate = canonical(backup_dir)
    root = auth_root_dir(profile_dir)
    if candidate.parent != root or not candidate.name.startswith(QUARANTINE_PREFIX):
        raise ProfileRootRefusedError(
            f"Refusing to restore from {candidate}: a retired session lives in "
            f"{root} under a name starting with {QUARANTINE_PREFIX!r}."
        )
    return candidate


def _restore_source_profile_locked(backup_dir: Path, profile_dir: Path) -> bool:
    """Put a retired session back; the caller holds the profile exclusively."""
    targets = {target.name: target for target in _auth_state_targets(profile_dir)}

    # Only a successful login or import commits all three of these together.
    # Anything less is debris — most often the profile directory Chromium
    # creates on launch and abandons when the login is cancelled, or a
    # half-written marker — and reading it as a replacement would strand the
    # working session in quarantine.
    replacement_committed = (
        load_source_state(profile_dir) is not None
        and profile_exists(profile_dir)
        and portable_cookie_path(profile_dir).exists()
    )
    if replacement_committed:
        logger.debug("Not restoring %s: a newer session is in place", backup_dir)
        return False

    # Park the debris beside the backup instead of deleting it: if a move fails
    # halfway the caller ends up with neither session, and an uncommitted
    # profile may still hold a Chromium login worth inspecting.
    debris_dir = backup_dir.parent / f"{backup_dir.name}-superseded"
    for target in _auth_state_targets(profile_dir):
        if not target.exists():
            continue
        try:
            secure_mkdir(debris_dir)
            shutil.move(str(target), str(debris_dir / target.name))
        except OSError as exc:
            logger.warning("Could not clear %s before restoring: %s", target, exc)
            return False

    restorable = [
        (item, targets[item.name])
        for item in backup_dir.iterdir()
        if item.name in targets
    ]
    restored: list[Path] = []
    for source, target in restorable:
        try:
            shutil.move(str(source), str(target))
            restored.append(target)
        except OSError as exc:
            # Undo the partial restore, so the session stays wholly quarantined
            # rather than split across both places, where the auth preflight
            # rejects it and the next rotation would divide it again.
            logger.warning("Could not restore %s: %s", target, exc)
            _retire(backup_dir, restored)
            return False
    try:
        backup_dir.rmdir()
    except OSError:
        pass
    logger.info("Restored the previous session from %s", backup_dir)
    return True


def _retire(backup_dir: Path, targets: list[Path]) -> None:
    """Move *targets* back into *backup_dir*, best effort."""
    for target in targets:
        try:
            shutil.move(str(target), str(backup_dir / target.name))
        except OSError as exc:
            logger.warning("Could not re-retire %s: %s", target, exc)


def clear_auth_state(source_profile_dir: Path | None = None) -> bool:
    """Remove source auth artifacts, derived runtime profiles and quarantines.

    The ownership marker is deliberately not among the targets. Logout is
    exactly when the next run needs it: erasing it would leave a custom root
    unclaimed, and the login that follows would be refused.

    Raises:
        ProfileRootRefusedError: The root is not one this server owns.
        RuntimeError: Another process is using the profile. Deleting it out from
            under a live browser corrupts that session and, with several clients,
            destroys everyone's rather than just this caller's.
    """
    profile_dir = _owned(source_profile_dir)
    with _exclusive_profile(profile_dir, action="clearing the stored session"):
        # Quarantines hold previous sessions' cookies, so a logout that left them
        # behind would not be the "clear all stored auth state" the CLI
        # advertises.
        targets = _auth_state_targets(profile_dir) + quarantine_dirs(profile_dir)

        success = True
        for target in targets:
            if not target.exists():
                continue
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as exc:
                logger.warning("Could not clear auth artifact %s: %s", target, exc)
                success = False
        return success


def reset_source_profile(source_profile_dir: Path | None = None) -> None:
    """Drop a half-staged session so the next attempt cannot mix with it.

    Lives here rather than beside its one caller in the import path, which is
    the point: as a private helper over there it was a bare
    ``rmtree(user_data_dir, ignore_errors=True)`` that no guard could see. Both
    artifacts go together because the import writes both — the staged cookies
    first, then whatever Chromium put in the profile while validating them — and
    leaving either behind is how the next candidate browser's session would be
    read through the previous one's.

    Best effort by design: this runs between attempts, and a failure to clean up
    must not end an import that still has candidates left.
    """
    profile_dir = _owned(source_profile_dir)
    portable_cookie_path(profile_dir).unlink(missing_ok=True)
    shutil.rmtree(profile_dir, ignore_errors=True)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable auth state file: %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed auth state file: %s", path)
        return None
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    secure_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
