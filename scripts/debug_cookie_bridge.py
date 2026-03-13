"""Manual cookie-bridge debugger for cross-platform LinkedIn sessions.

This script is intentionally not part of the automated test suite. Use it
sparingly to inspect how a host-authenticated session behaves when replayed
into a fresh browser profile, including Docker/Linux runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, cast

from linkedin_mcp_server.common_utils import slugify_fragment
from linkedin_mcp_server.core.auth import detect_auth_barrier, is_logged_in
from linkedin_mcp_server.core.browser import BrowserManager


DEFAULT_TARGET_URL = "https://www.linkedin.com/in/williamhgates/"
_SETTLE_DELAY_SECONDS = 10.0

COOKIE_PRESETS: dict[str, set[str] | None] = {
    "li_at_only": {"li_at"},
    "auth_minimal": {"li_at", "JSESSIONID", "bcookie", "bscookie", "lidc"},
    "auth_only": {"li_at", "li_rm"},
    "bridge_core": {
        "li_at",
        "li_rm",
        "JSESSIONID",
        "bcookie",
        "bscookie",
        "liap",
        "lidc",
        "li_gc",
        "lang",
        "timezone",
        "li_mc",
    },
    "full": None,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cookie-path",
        type=Path,
        default=Path.home() / ".linkedin-mcp" / "cookies.json",
        help="Path to portable LinkedIn cookie JSON",
    )
    parser.add_argument(
        "--candidate",
        choices=sorted(COOKIE_PRESETS),
        default="bridge_core",
        help="Cookie subset to replay",
    )
    parser.add_argument(
        "--target-url",
        default=DEFAULT_TARGET_URL,
        help="Authenticated page to probe after bridge replay",
    )
    parser.add_argument(
        "--pre-nav",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Navigate to /feed before importing cookies",
    )
    parser.add_argument(
        "--clear-existing",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Clear fresh browser cookies before import",
    )
    parser.add_argument(
        "--body-lines",
        type=int,
        default=20,
        help="Number of non-empty body lines to include in the report",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write JSON report",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="Optional directory for screenshots and other debug artifacts",
    )
    parser.add_argument(
        "--checkpoint-restart",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Close and reopen the same profile after a successful bridge replay",
    )
    return parser.parse_args()


def load_portable_cookies(
    cookie_path: Path,
    candidate: str,
) -> list[dict[str, Any]]:
    all_cookies = json.loads(cookie_path.read_text())
    normalized = [
        BrowserManager._normalize_cookie_domain(cookie)
        for cookie in all_cookies
        if "linkedin.com" in cookie.get("domain", "")
    ]
    keep_names = COOKIE_PRESETS[candidate]
    if keep_names is None:
        return normalized
    return [cookie for cookie in normalized if cookie.get("name") in keep_names]


async def capture_page_state(page, *, body_lines: int) -> dict[str, Any]:
    try:
        title = await page.title()
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        title = f"<error: {exc}>"

    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        body_text = f"<error: {exc}>"

    body_lines_trimmed = []
    if isinstance(body_text, str) and not body_text.startswith("<error:"):
        body_lines_trimmed = [
            line.strip() for line in body_text.splitlines() if line.strip()
        ][:body_lines]

    cookies = await page.context.cookies()
    linkedin_cookie_names = sorted(
        {
            cookie["name"]
            for cookie in cookies
            if "linkedin.com" in cookie.get("domain", "")
        }
    )

    return {
        "url": page.url,
        "title": title,
        "logged_in": await is_logged_in(page),
        "auth_barrier": await detect_auth_barrier(page),
        "body_length": len(body_text) if isinstance(body_text, str) else None,
        "body_head": body_lines_trimmed,
        "linkedin_cookie_names": linkedin_cookie_names,
    }


def _slugify_step(step: str) -> str:
    return slugify_fragment(step)


def _resolve_artifact_dir(args: argparse.Namespace) -> Path | None:
    if args.artifact_dir:
        return args.artifact_dir.expanduser().resolve()
    if args.output:
        return args.output.expanduser().resolve().with_suffix("").parent / (
            args.output.stem + "_artifacts"
        )
    return None


async def capture_screenshot(page, step: str, artifact_dir: Path | None) -> str | None:
    if artifact_dir is None:
        return None

    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{_slugify_step(step)}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        return f"<error: {exc}>"


async def safe_goto(page, url: str) -> dict[str, Any]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return {"ok": True}
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def settle_page(page) -> None:
    """Give LinkedIn time to finish redirects and hydrate content."""
    await asyncio.sleep(_SETTLE_DELAY_SECONDS)
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:  # pragma: no cover - best effort diagnostics
        pass
    await asyncio.sleep(1)


async def _capture_step(
    report: dict[str, Any],
    page,
    *,
    step: str,
    body_lines: int,
    artifact_dir: Path | None,
) -> None:
    await settle_page(page)
    report[f"{step}_screenshot"] = await capture_screenshot(page, step, artifact_dir)
    report[step] = await capture_page_state(page, body_lines=body_lines)


async def run_debug(args: argparse.Namespace) -> dict[str, Any]:
    imported_cookies = load_portable_cookies(args.cookie_path, args.candidate)
    artifact_dir = _resolve_artifact_dir(args)

    temp_dir = Path(tempfile.mkdtemp(prefix="linkedin-cookie-debug-"))
    profile_dir = temp_dir / "profile"

    report: dict[str, Any] = {
        "cookie_path": str(args.cookie_path),
        "candidate": args.candidate,
        "import_cookie_names": [cookie["name"] for cookie in imported_cookies],
        "pre_nav": args.pre_nav,
        "clear_existing": args.clear_existing,
        "checkpoint_restart": args.checkpoint_restart,
        "target_url": args.target_url,
        "temp_profile_dir": str(profile_dir),
    }
    if artifact_dir is not None:
        report["artifact_dir"] = str(artifact_dir)

    browser = BrowserManager(user_data_dir=profile_dir, headless=True)
    browser_closed = False
    try:
        await browser.start()
        await _capture_step(
            report,
            browser.page,
            step="start",
            body_lines=args.body_lines,
            artifact_dir=artifact_dir,
        )

        if args.pre_nav:
            report["pre_nav_result"] = await safe_goto(
                browser.page,
                "https://www.linkedin.com/feed/",
            )
            await _capture_step(
                report,
                browser.page,
                step="after_pre_nav",
                body_lines=args.body_lines,
                artifact_dir=artifact_dir,
            )

        if args.clear_existing:
            await browser.context.clear_cookies()

        await browser.context.add_cookies(cast(Any, imported_cookies))
        await _capture_step(
            report,
            browser.page,
            step="after_import",
            body_lines=args.body_lines,
            artifact_dir=artifact_dir,
        )

        report["feed_nav_result"] = await safe_goto(
            browser.page,
            "https://www.linkedin.com/feed/",
        )
        await _capture_step(
            report,
            browser.page,
            step="after_feed_nav",
            body_lines=args.body_lines,
            artifact_dir=artifact_dir,
        )

        report["target_nav_result"] = await safe_goto(browser.page, args.target_url)
        await _capture_step(
            report,
            browser.page,
            step="after_target_nav",
            body_lines=args.body_lines,
            artifact_dir=artifact_dir,
        )

        if args.checkpoint_restart:
            storage_state_path = temp_dir / "storage-state.json"
            report["storage_state_exported"] = await browser.export_storage_state(
                storage_state_path, indexed_db=True
            )
            report["storage_state_path"] = str(storage_state_path)
            await browser.close()
            browser_closed = True

            reopened = BrowserManager(user_data_dir=profile_dir, headless=True)
            try:
                await reopened.start()
                report["reopened_feed_nav_result"] = await safe_goto(
                    reopened.page,
                    "https://www.linkedin.com/feed/",
                )
                await _capture_step(
                    report,
                    reopened.page,
                    step="after_reopened_feed_nav",
                    body_lines=args.body_lines,
                    artifact_dir=artifact_dir,
                )

                report["reopened_target_nav_result"] = await safe_goto(
                    reopened.page,
                    args.target_url,
                )
                await _capture_step(
                    report,
                    reopened.page,
                    step="after_reopened_target_nav",
                    body_lines=args.body_lines,
                    artifact_dir=artifact_dir,
                )
            finally:
                await reopened.close()
        return report
    finally:
        if not browser_closed:
            await browser.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    args = parse_args()
    report = asyncio.run(run_debug(args))
    rendered = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
