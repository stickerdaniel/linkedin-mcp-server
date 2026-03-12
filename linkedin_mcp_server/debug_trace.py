"""Best-effort trace capture with on-error retention."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Literal

from linkedin_mcp_server.session_state import auth_root_dir

TraceMode = Literal["off", "on_error", "always"]

_TRACE_COUNTER = itertools.count(1)
_TRACE_DIR: Path | None = None
_TRACE_KEEP = False
_EXPLICIT_TRACE_DIR = False


def _trace_mode() -> TraceMode:
    raw = os.getenv("LINKEDIN_TRACE_MODE", "").strip().lower()
    if raw in {"off", "false", "0", "no"}:
        return "off"
    if raw in {"always", "keep", "persist"}:
        return "always"
    return "on_error"


def _trace_root() -> Path:
    source_profile = Path(
        os.getenv("USER_DATA_DIR", "~/.linkedin-mcp/profile")
    ).expanduser()
    root = auth_root_dir(source_profile) / "trace-runs"
    root.mkdir(parents=True, exist_ok=True)
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
        _TRACE_DIR = Path(
            tempfile.mkdtemp(
                prefix="run-",
                dir=_trace_root(),
            )
        ).resolve()
    return _TRACE_DIR


def mark_trace_for_retention() -> Path | None:
    global _TRACE_KEEP
    trace_dir = get_trace_dir()
    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        _TRACE_KEEP = True
    return trace_dir


def should_keep_traces() -> bool:
    return _EXPLICIT_TRACE_DIR or _TRACE_KEEP or _trace_mode() == "always"


def cleanup_trace_dir() -> None:
    global _TRACE_DIR, _TRACE_KEEP, _EXPLICIT_TRACE_DIR

    trace_dir = _TRACE_DIR
    if trace_dir is None or should_keep_traces():
        return
    try:
        shutil.rmtree(trace_dir)
    except OSError:
        return
    _TRACE_DIR = None
    _TRACE_KEEP = False
    _EXPLICIT_TRACE_DIR = False


def reset_trace_state_for_testing() -> None:
    global _TRACE_DIR, _TRACE_KEEP, _EXPLICIT_TRACE_DIR
    _TRACE_DIR = None
    _TRACE_KEEP = False
    _EXPLICIT_TRACE_DIR = False


def _slugify_step(step: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", step.lower()).strip("-")


async def record_page_trace(
    page: Any, step: str, *, extra: dict[str, Any] | None = None
) -> None:
    """Persist a screenshot and basic page state when trace capture is enabled."""
    trace_dir = get_trace_dir()
    if trace_dir is None:
        return

    trace_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir = trace_dir / "screens"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
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

    with (trace_dir / "trace.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
