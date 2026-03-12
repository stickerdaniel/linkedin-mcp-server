"""Best-effort page tracing for manual LinkedIn debugging."""

from __future__ import annotations

import itertools
import json
import os
import re
from pathlib import Path
from typing import Any

_TRACE_COUNTER = itertools.count(1)


def get_trace_dir() -> Path | None:
    raw = os.getenv("LINKEDIN_DEBUG_TRACE_DIR", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _slugify_step(step: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", step.lower()).strip("-")


async def record_page_trace(
    page: Any, step: str, *, extra: dict[str, Any] | None = None
) -> None:
    """Persist a screenshot and basic page state when trace debugging is enabled."""
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
