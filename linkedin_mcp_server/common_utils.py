"""Small shared helpers used across diagnostics and session-state modules."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
import re
import tempfile


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
    missing: list[Path] = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for part in reversed(missing):
        part.mkdir(mode=mode, exist_ok=True)


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
