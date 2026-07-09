"""Small shared helpers used across diagnostics and session-state modules."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
import re
import tempfile
from typing import Any, Literal

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


def _render_result_text(result: dict[str, Any]) -> str:
    """Render a tool result dict as readable plain text for .txt/.md dumps."""
    parts: list[str] = []
    url = result.get("url")
    if url:
        parts.append(f"URL: {url}")
    for name, text in (result.get("sections") or {}).items():
        parts.append(f"\n## {name}\n{text}")
    job_ids = result.get("job_ids")
    if job_ids:
        parts.append("\nJOB_IDS: " + ", ".join(job_ids))
    return "\n".join(parts) + "\n"


def _resolve_export_path(output_path: str) -> Path:
    """Resolve *output_path* inside the dedicated LinkedIn MCP export directory."""
    export_root_path = Path.home().resolve() / ".linkedin-mcp" / "exports"
    export_root = export_root_path.resolve()
    if export_root != export_root_path:
        raise ValueError("LinkedIn MCP export directory must not be a symlink")

    requested = Path(output_path).expanduser()
    path = (requested if requested.is_absolute() else export_root / requested).resolve()
    if not path.is_relative_to(export_root):
        raise ValueError(
            "output_path must be inside the LinkedIn MCP export directory "
            f"({export_root})"
        )
    return path


def apply_output_mode(
    result: dict[str, Any],
    output_path: str | None,
    output_mode: Literal["display", "file", "both"],
) -> dict[str, Any]:
    """Optionally persist *result* to disk and shape what is returned to the caller.

    - ``display`` (default): return the full result, write nothing.
    - ``file``: write to *output_path*, return a compact confirmation only.
    - ``both``: write to *output_path* and return the full result plus saved path.

    Relative paths resolve under ``~/.linkedin-mcp/exports``. Absolute paths
    must also stay inside that directory. File format follows the extension:
    ``.json`` dumps the full dict; anything else writes a readable text rendering
    of url/sections/job_ids.
    """
    if output_mode == "display":
        return result
    if not output_path:
        raise ValueError("output_path is required when output_mode is 'file' or 'both'")

    path = _resolve_export_path(output_path)
    if path.suffix == ".json":
        secure_write_text(path, json.dumps(result, ensure_ascii=False, indent=2))
    else:
        secure_write_text(path, _render_result_text(result))

    if output_mode == "both":
        return {**result, "saved_path": str(path)}
    confirmation: dict[str, Any] = {"saved_path": str(path)}
    if "url" in result:
        confirmation["url"] = result["url"]
    if "job_ids" in result:
        confirmation["job_ids"] = result["job_ids"]
    confirmation["section_names"] = sorted((result.get("sections") or {}).keys())
    return confirmation
