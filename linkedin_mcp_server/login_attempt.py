"""A manual login's own browser directory, and how its session becomes the source.

Each login runs Chromium in ``<auth-root>/login-attempts/<uuid4>/profile``,
never on the source profile, so no login holds the profile lease while somebody
types. The attempt's lock is the sibling ``<uuid4>.lock``, outside the
directory that gets deleted, and it is held by the owning process and, on
POSIX, by a crash guardian until the attempt's browser is provably gone.

The first attempt with a session installs it under a short source lease
(:func:`promote`). A guarded attempt that finds a peer's session there stands
down, and one that started before a logout finds the epoch changed and is
discarded.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import shutil
import stat
from typing import Literal
from uuid import UUID, uuid4

from linkedin_mcp_server.private_state import PrivateStateError, harden_directory_entry
from linkedin_mcp_server.process_tree import BrowserGuardian, start_browser_guardian
from linkedin_mcp_server.profile_lease import _release_locked_fd, acquire_locked_fd
from linkedin_mcp_server.session_state import (
    UNGUARDED,
    PeerSessionInPlaceError,
    _owned,
    auth_root_dir,
    portable_cookie_path,
    read_logout_epoch,
    restore_source_profile,
    rotate_source_profile,
    write_source_state,
)

logger = logging.getLogger(__name__)

ATTEMPTS_DIR = "login-attempts"
_LOCK_SUFFIX = ".lock"
_WINDOWS = os.name == "nt"

Promotion = Literal["promoted", "peer", "fenced"]


def attempts_root(source_profile_dir: Path) -> Path:
    """Where every login attempt for *source_profile_dir* lives."""
    return auth_root_dir(source_profile_dir) / ATTEMPTS_DIR


@dataclass
class PendingLogin:
    """One login attempt: its directory, its held lock and its guardian."""

    source: Path
    directory: Path
    lock_path: Path
    lock_fd: int
    logout_epoch: str
    guardian: BrowserGuardian | None = None

    @property
    def profile_dir(self) -> Path:
        return self.directory / "profile"

    @property
    def cookie_path(self) -> Path:
        return self.directory / "cookies.json"

    def discard(self) -> None:
        """Delete the attempt and let its lock go.

        Only after the attempt's browser closed with a confirmed teardown: the
        guardian is released first, because unlocking the descriptor it shares
        would free the lock for both.
        """
        if self.guardian is not None:
            self.guardian.release()
            self.guardian = None
        try:
            if _is_plain_directory(self.directory):
                shutil.rmtree(self.directory)
        except OSError as exc:
            logger.warning(
                "Could not remove login attempt %s, a later sweep retries: %s",
                self.directory,
                exc,
            )
            _release_locked_fd(self.lock_fd)
            return
        _dispose_lock(self.lock_path, self.lock_fd)


def _is_plain(entry: os.stat_result) -> bool:
    if stat.S_ISLNK(entry.st_mode):
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return not (_WINDOWS and getattr(entry, "st_file_attributes", 0) & reparse)


def _is_plain_directory(path: Path) -> bool:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return False
    return _is_plain(entry) and stat.S_ISDIR(entry.st_mode)


def _lock(lock_path: Path) -> int | None:
    """Lock *lock_path* exclusively, or ``None`` while another holder has it.

    A planted link or a non-regular entry is refused, before the open and
    again on what the open returned: ``acquire_locked_fd`` follows links.
    """
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and not (_is_plain(before) and stat.S_ISREG(before.st_mode)):
        raise PrivateStateError(f"{lock_path} is not a regular lock file")
    fd = acquire_locked_fd(lock_path, exclusive=True)
    if fd is None:
        return None
    try:
        opened = os.fstat(fd)
        after = lock_path.lstat()
        if not (
            _is_plain(after)
            and stat.S_ISREG(opened.st_mode)
            and (opened.st_dev, opened.st_ino) == (after.st_dev, after.st_ino)
        ):
            raise PrivateStateError(f"{lock_path} changed while it was locked")
    except BaseException:
        _release_locked_fd(fd)
        raise
    return fd


def _dispose_lock(lock_path: Path, fd: int) -> None:
    """Remove a held lock file and release it, in the order each platform allows.

    POSIX unlinks while still holding, so nobody can lock the name in between.
    Windows cannot delete a file that is open, so it releases first; a sweeper
    that locks the name in that moment finds no attempt directory and removes
    the file itself.
    """
    if _WINDOWS:
        _release_locked_fd(fd)
        with contextlib.suppress(FileNotFoundError, PermissionError):
            os.unlink(lock_path)
        return
    try:
        held = os.fstat(fd)
        with contextlib.suppress(FileNotFoundError):
            current = lock_path.lstat()
            if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                os.unlink(lock_path)
    finally:
        _release_locked_fd(fd)


def _attempt_name(name: str) -> str | None:
    """The UUID stem of a lock file name, or ``None`` for anything else."""
    if not name.endswith(_LOCK_SUFFIX):
        return None
    stem = name.removesuffix(_LOCK_SUFFIX)
    try:
        parsed = UUID(stem)
    except ValueError:
        return None
    return stem if str(parsed) == stem else None


def sweep(root: Path) -> bool:
    """Remove every abandoned attempt under *root*, and say whether all went.

    An attempt is abandoned when its lock can be taken: a live owner, or a
    guardian still draining a crashed owner's browser, holds it. Chromium's
    ``SingletonLock`` is no signal here, because the headless shell never
    writes one. A directory without a lock file is left alone, since an owner
    creates the lock before the directory.
    """
    try:
        names = [entry.name for entry in os.scandir(root)]
    except FileNotFoundError:
        return True
    complete = True
    for name in names:
        stem = _attempt_name(name)
        if stem is None:
            continue
        lock_path = root / name
        try:
            fd = _lock(lock_path)
        except (FileNotFoundError, PrivateStateError):
            continue
        if fd is None:
            continue
        directory = root / stem
        try:
            entry = directory.lstat()
        except FileNotFoundError:
            entry = None
        if entry is not None and not (_is_plain(entry) and stat.S_ISDIR(entry.st_mode)):
            _release_locked_fd(fd)
            continue
        try:
            if entry is not None:
                shutil.rmtree(directory)
        except OSError as exc:
            logger.warning(
                "Could not remove abandoned login attempt %s: %s", directory, exc
            )
            _release_locked_fd(fd)
            complete = False
            continue
        _dispose_lock(lock_path, fd)
    return complete


def sweep_abandoned(source_profile_dir: Path) -> bool:
    """Remove abandoned attempts of *source_profile_dir*, as a logout does.

    Only a real ``login-attempts`` directory is swept: a planted link there
    would lead the deletion somewhere else. Attempts still held are left to
    their owners, which discard them once their browser has closed.
    """
    root = attempts_root(source_profile_dir)
    return sweep(root) if _is_plain_directory(root) else True


def open_pending_login(source_profile_dir: Path) -> PendingLogin:
    """Create a new attempt for *source_profile_dir*, its lock and its guardian.

    Raises:
        ProfileRootRefusedError: The source root is not one this server owns.
            Nothing is created.
        PrivateStateError: ``login-attempts`` is not a private directory, or
            an entry in it is not what this module creates.
    """
    source = _owned(source_profile_dir)
    root = attempts_root(source)
    harden_directory_entry(root)
    sweep(root)

    name = str(uuid4())
    lock_path = root / f"{name}{_LOCK_SUFFIX}"
    fd = _lock(lock_path)
    if fd is None:
        raise PrivateStateError(f"{lock_path} is already held")
    directory = root / name
    try:
        os.mkdir(directory, 0o700)
        pending = PendingLogin(
            source=source,
            directory=directory,
            lock_path=lock_path,
            lock_fd=fd,
            logout_epoch=read_logout_epoch(source),
        )
        pending.guardian = start_browser_guardian(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            directory.rmdir()
        _dispose_lock(lock_path, fd)
        raise
    return pending


def promote(
    pending: PendingLogin, *, superseded_by: str | None | object = UNGUARDED
) -> Promotion:
    """Install *pending*'s session as the source session.

    The caller holds the source lease and the attempt's browser has closed with
    a confirmed teardown. ``source-state.json`` is written last, because
    readiness is false until it exists.

    Returns ``"fenced"`` when a logout happened since the attempt started and
    ``"peer"`` when a guarded attempt finds a peer's session in place; neither
    touches the source.

    Raises:
        RuntimeError: Another browser holds the source. Nothing was moved.
        OSError: A move failed. The previous session is put back first, or,
            when there was none, the attempt's files go back into the attempt.
    """
    source = pending.source
    if read_logout_epoch(source) != pending.logout_epoch:
        return "fenced"
    try:
        retired = rotate_source_profile(source, superseded_by=superseded_by)
    except PeerSessionInPlaceError:
        return "peer"

    cookie_path = portable_cookie_path(source)
    profile_moved = cookies_moved = False
    try:
        os.rename(pending.profile_dir, source)
        profile_moved = True
        os.replace(pending.cookie_path, cookie_path)
        cookies_moved = True
        write_source_state(source)
    except BaseException as exc:
        if retired is not None:
            if not restore_source_profile(retired, source):
                raise OSError(
                    f"The new session could not be installed and the previous "
                    f"one could not be restored; it is kept at {retired}."
                ) from exc
        else:
            if cookies_moved:
                os.replace(cookie_path, pending.cookie_path)
            if profile_moved:
                os.rename(source, pending.profile_dir)
        raise
    return "promoted"
