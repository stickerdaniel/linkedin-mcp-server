"""Small shared helpers used across diagnostics and session-state modules."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
import re
import tempfile

_PRIVATE_DIR_MODE = 0o700


def slugify_fragment(value: str) -> str:
    """Return a lowercase URL/file-safe fragment."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def utcnow_iso() -> str:
    """Return the current UTC timestamp in a compact ISO-8601 form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def secure_mkdir(path: Path, mode: int = 0o700) -> None:
    """Create a directory tree with restrictive permissions.

    Unlike ``Path.mkdir(parents=True, mode=...)``, this applies *mode* to
    every newly created directory in the chain, not just the leaf.
    """
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"Path exists and is not a directory: {path}")

    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for part in reversed(missing):
        part.mkdir(mode=mode, exist_ok=True)


def harden_linkedin_tree(path: Path) -> None:
    """Ensure dirs from *path* up to ``.linkedin-mcp`` are owner-only (``0o700``).

    Complements :func:`secure_mkdir` by hardening pre-existing directories that
    may have been created with default umask permissions. No-op on Windows or
    when *path* is not inside a ``.linkedin-mcp`` directory.
    """
    if os.name == "nt":
        return
    d = path if path.is_dir() else path.parent
    # Bail out early when the path is not inside a .linkedin-mcp tree.
    if not any(p.name == ".linkedin-mcp" for p in (d, *d.parents)):
        return
    for p in (d, *d.parents):
        if p.is_dir() and stat.S_IMODE(p.stat().st_mode) != _PRIVATE_DIR_MODE:
            p.chmod(_PRIVATE_DIR_MODE)
        if p.name == ".linkedin-mcp":
            return


def secure_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Atomically write *content* to *path* with owner-only permissions.

    Uses a temp file + ``os.replace`` in the same directory so the write is
    atomic on the same filesystem and avoids TOCTOU permission races.
    """
    secure_mkdir(path.parent)
    fd_int, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd_int, "w") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
