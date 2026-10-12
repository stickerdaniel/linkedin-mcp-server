"""Best-effort trace capture with on-error retention."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Literal

from linkedin_mcp_server.common_utils import secure_mkdir, slugify_fragment
from linkedin_mcp_server.profile_lease import ProfileLeaseUnavailableError, try_lock
from linkedin_mcp_server.session_state import auth_root_dir, get_source_profile_dir

TraceMode = Literal["off", "on_error", "always"]

_TRACE_COUNTER = itertools.count(1)
_TRACE_DIR: Path | None = None
_TRACE_KEEP = False
_EXPLICIT_TRACE_DIR = False
#: This process's lock on its own run directory, held until it exits.
_RUN_LOCK_FD: int | None = None
_RUN_LOCK = ".alive"

#: How long a run directory under ``trace-runs`` outlives its last write. Each
#: process creates one, and a host that kills its server skips the exit-time
#: cleanup, so without a bound they accumulate (2,546 measured on one machine).
#: Long enough to attach a retained trace to an issue report.
_RETAIN_SECONDS = 14 * 24 * 60 * 60


def _trace_mode() -> TraceMode:
    raw = os.getenv("LINKEDIN_TRACE_MODE", "").strip().lower()
    if raw in {"off", "false", "0", "no"}:
        return "off"
    if raw in {"always", "keep", "persist"}:
        return "always"
    return "on_error"


def _trace_root(source_profile: Path) -> Path:
    root = auth_root_dir(source_profile) / "trace-runs"
    secure_mkdir(root)
    return root


def trace_enabled() -> bool:
    return (
        bool(os.getenv("LINKEDIN_DEBUG_TRACE_DIR", "").strip())
        or _trace_mode() != "off"
    )


def get_trace_dir() -> Path | None:
    global _TRACE_DIR, _EXPLICIT_TRACE_DIR

    explicit = os.getenv("LINKEDIN_DEBUG_TRACE_DIR", "").strip()
    if explicit:
        _EXPLICIT_TRACE_DIR = True
        if _TRACE_DIR is None:
            _TRACE_DIR = Path(explicit).expanduser().resolve()
        return _TRACE_DIR

    if _trace_mode() == "off":
        return None

    if _TRACE_DIR is None:
        # One lookup, so the root pruned is the root whose claim was checked.
        source_profile = _safe_source_profile_dir()
        root = _trace_root(source_profile)
        _prune_stale_runs(source_profile, root)
        _TRACE_DIR = Path(tempfile.mkdtemp(prefix="run-", dir=root)).resolve()
        _hold_run(_TRACE_DIR)
    return _TRACE_DIR


def _hold_run(run: Path) -> None:
    """Lock *run* for as long as this process lives, so pruning passes it by.

    The kernel releases the lock when the process dies, killed or not. Without
    file locking the run goes unlocked, and so does every other run, which
    pruning then keeps.
    """
    global _RUN_LOCK_FD
    try:
        fd = os.open(run / _RUN_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        held = try_lock(fd, exclusive=True)
    except ProfileLeaseUnavailableError:
        held = False
    if held:
        _RUN_LOCK_FD = fd
    else:
        os.close(fd)


def _release_run() -> None:
    """Drop the lock, which Windows requires before the run can be removed."""
    global _RUN_LOCK_FD
    if _RUN_LOCK_FD is not None:
        os.close(_RUN_LOCK_FD)
        _RUN_LOCK_FD = None


def _in_use(run: Path) -> bool:
    """Whether a live process holds *run*. Not knowing counts as yes."""
    try:
        fd = os.open(run / _RUN_LOCK, os.O_RDWR)
    except FileNotFoundError:
        # Written before runs were locked: its age is all there is to go on.
        return False
    except OSError:
        return True
    try:
        return not try_lock(fd, exclusive=True)
    except ProfileLeaseUnavailableError:
        return True
    finally:
        os.close(fd)


def _prune_stale_runs(source_profile: Path, root: Path) -> None:
    """Remove ``run-*`` directories under *root* untouched for the retention period.

    Only under a profile root this server owns (``require_profile_claim``), only
    in a ``trace-runs`` that is not itself a link, and only real directories
    named the way :func:`get_trace_dir` names them; a symlink is never followed.
    A run whose process is still alive is kept however quiet it has been. Best
    effort: a run that cannot be read or removed stays.
    """
    from linkedin_mcp_server.exceptions import ProfileRootRefusedError
    from linkedin_mcp_server.profile_claim import require_profile_claim

    try:
        require_profile_claim(source_profile)
    except ProfileRootRefusedError:
        return
    # The claim covers the auth root, not wherever a linked `trace-runs` points.
    if root.is_symlink() or os.path.isjunction(root):
        return
    cutoff = time.time() - _RETAIN_SECONDS
    try:
        runs = [
            entry
            for entry in os.scandir(root)
            if entry.name.startswith("run-") and entry.is_dir(follow_symlinks=False)
        ]
    except OSError:
        return
    for run in runs:
        try:
            if _last_write(Path(run.path)) < cutoff and not _in_use(Path(run.path)):
                shutil.rmtree(run.path)
        except OSError:
            continue


def _last_write(run: Path) -> float:
    """The newest modification time of *run* and its direct entries."""
    newest = run.stat(follow_symlinks=False).st_mtime
    for entry in os.scandir(run):
        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    return newest


def mark_trace_for_retention() -> Path | None:
    global _TRACE_KEEP
    trace_dir = get_trace_dir()
    if trace_dir is not None:
        secure_mkdir(trace_dir)
        _TRACE_KEEP = True
    return trace_dir


def should_keep_traces() -> bool:
    return _EXPLICIT_TRACE_DIR or _TRACE_KEEP or _trace_mode() == "always"


def cleanup_trace_dir() -> None:
    global _TRACE_DIR, _TRACE_KEEP, _EXPLICIT_TRACE_DIR

    trace_dir = _TRACE_DIR
    if trace_dir is None or should_keep_traces():
        return
    _release_run()
    try:
        shutil.rmtree(trace_dir)
    except OSError:
        return
    _TRACE_DIR = None
    _TRACE_KEEP = False
    _EXPLICIT_TRACE_DIR = False


def reset_trace_state_for_testing() -> None:
    global _TRACE_COUNTER, _TRACE_DIR, _TRACE_KEEP, _EXPLICIT_TRACE_DIR
    _TRACE_COUNTER = itertools.count(1)
    _release_run()
    _TRACE_DIR = None
    _TRACE_KEEP = False
    _EXPLICIT_TRACE_DIR = False


def _slugify_step(step: str) -> str:
    return slugify_fragment(step)


def _safe_source_profile_dir() -> Path:
    try:
        return get_source_profile_dir()
    except Exception:
        return Path("~/.linkedin-mcp/profile").expanduser()


async def record_page_trace(
    page: Any, step: str, *, extra: dict[str, Any] | None = None
) -> None:
    """Persist a screenshot and basic page state when trace capture is enabled."""
    trace_dir = get_trace_dir()
    if trace_dir is None:
        return

    secure_mkdir(trace_dir)
    screenshot_dir = trace_dir / "screens"
    secure_mkdir(screenshot_dir)
    step_id = next(_TRACE_COUNTER)
    slug = _slugify_step(step) or "step"

    try:
        title = await page.title()
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        title = f"<error: {exc}>"

    try:
        body_text = await page.evaluate("() => document.body?.innerText || ''")
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        body_text = f"<error: {exc}>"

    if not isinstance(body_text, str):
        body_text = ""

    try:
        remember_me = (await page.locator("#rememberme-div").count()) > 0
    except Exception:  # pragma: no cover - best effort diagnostics
        remember_me = False

    try:
        cookies = await page.context.cookies()
    except Exception:  # pragma: no cover - best effort diagnostics
        cookies = []

    linkedin_cookie_names = sorted(
        {
            cookie["name"]
            for cookie in cookies
            if "linkedin.com" in cookie.get("domain", "")
        }
    )

    screenshot_path = screenshot_dir / f"{step_id:03d}-{slug}.png"
    screenshot: str | None = None
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot = str(screenshot_path)
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        screenshot = f"<error: {exc}>"

    payload = {
        "step_id": step_id,
        "step": step,
        "url": getattr(page, "url", ""),
        "title": title,
        "remember_me": remember_me,
        "body_length": len(body_text),
        "body_marker": " ".join(body_text.split())[:200],
        "linkedin_cookie_names": linkedin_cookie_names,
        "screenshot": screenshot,
        "extra": extra or {},
    }

    trace_jsonl = trace_dir / "trace.jsonl"
    try:
        fd = os.open(str(trace_jsonl), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    with trace_jsonl.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
