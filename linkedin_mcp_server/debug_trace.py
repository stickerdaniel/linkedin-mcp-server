"""Best-effort trace capture for debugging scrape failures."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

_TRACE_DIR = Path("~/.linkedin-mcp/traces").expanduser()
_TRACE_COUNTER = itertools.count(1)


def _trace_enabled() -> bool:
    return os.getenv("LINKEDIN_DEBUG_TRACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def record_page_trace(page: Any, step: str, *, extra: dict[str, Any] | None = None) -> None:
    """Persist a screenshot and basic page state when trace capture is enabled."""
    if not _trace_enabled():
        return

    _TRACE_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_dir = _TRACE_DIR / "screens"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    step_id = next(_TRACE_COUNTER)

    try:
        title = await page.title()
    except Exception as exc:
        title = f"<error: {exc}>"

    try:
        body_text = await page.evaluate("() => document.body?.innerText || ''")
    except Exception as exc:
        body_text = f"<error: {exc}>"

    if not isinstance(body_text, str):
        body_text = ""

    try:
        remember_me = (await page.locator("#rememberme-div").count()) > 0
    except Exception:
        remember_me = False

    try:
        cookies = await page.context.cookies()
    except Exception:
        cookies = []

    linkedin_cookie_names = sorted(
        {cookie["name"] for cookie in cookies if "linkedin.com" in cookie.get("domain", "")}
    )

    screenshot_path = screenshot_dir / f"{step_id:03d}-{step}.png"
    screenshot: str | None = None
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        screenshot = str(screenshot_path)
    except Exception as exc:
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

    with (_TRACE_DIR / "trace.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
