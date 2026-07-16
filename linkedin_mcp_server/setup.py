"""
Interactive setup flows for LinkedIn MCP Server authentication.

Handles session creation through an isolated interactive browser. Only a
validated cookie snapshot, optional Camoufox identity, and source metadata are
published under the configured authentication root.
"""

import asyncio
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.core import (
    BrowserManager,
    BrowserTeardownError,
    resolve_remember_me_prompt,
    wait_for_manual_login,
)
from linkedin_mcp_server.common_utils import secure_mkdir
from linkedin_mcp_server.session_state import (
    acquire_pending_profile_lease,
    auth_root_dir,
    commit_source_session,
    source_session_lock,
    stage_bound_camoufox_identity,
)

from linkedin_mcp_server.drivers.browser import get_profile_dir


async def interactive_login(user_data_dir: Path | None = None) -> bool:
    """Run manual login while exclusively owning the canonical source session."""
    profile_dir = user_data_dir or get_profile_dir()
    async with source_session_lock(profile_dir):
        return await _interactive_login_unlocked(profile_dir)


async def _interactive_login_unlocked(user_data_dir: Path | None = None) -> bool:
    """
    Open an isolated browser for manual LinkedIn login.

    Opens a non-headless browser, navigates to LinkedIn login page,
    and waits for user to complete authentication (including 2FA, captcha, etc.).
    The disposable browser profile is removed after confirmed teardown; the
    canonical source profile is never opened or mutated.

    Args:
        user_data_dir: Path to browser profile. Defaults to config's user_data_dir.

    Returns:
        True if login was successful

    Raises:
        Exception: If login fails or times out
    """
    if user_data_dir is None:
        user_data_dir = get_profile_dir()

    config = get_config()
    login_timeout_ms = int(config.browser.login_timeout_seconds * 1000)

    if config.browser.login_timeout_seconds:
        budget = f"{config.browser.login_timeout_seconds / 60:.0f} minutes"
    else:
        budget = "no time limit"

    print("Opening browser for LinkedIn login...")
    print(f"   Please log in manually. You have {budget} to complete authentication.")
    print("   (This handles 2FA, captcha, and any security challenges)")

    launch_options: dict[str, Any] = {}
    if config.browser.chrome_path and config.browser.browser_engine == "patchright":
        launch_options["executable_path"] = config.browser.chrome_path
    if proxy := config.browser.proxy_settings():
        launch_options["proxy"] = proxy
        print(f"   Using proxy: {proxy['server']}")  # credential-free by construction

    viewport = {
        "width": config.browser.viewport_width,
        "height": config.browser.viewport_height,
    }
    canonical_profile_dir = user_data_dir.expanduser().resolve()
    pending_root = (
        auth_root_dir(canonical_profile_dir) / f".login-pending-{uuid4().hex}"
    )
    secure_mkdir(pending_root)
    pending_lease = acquire_pending_profile_lease(pending_root)
    login_profile_dir = pending_root / "profile"
    staged_cookie_path = pending_root / "cookies.json"
    staged_identity_path = (
        pending_root / "camoufox-identity.json"
        if config.browser.browser_engine == "camoufox"
        else None
    )
    try:
        if staged_identity_path is not None:
            # A corrupt/unbound canonical artifact is intentionally not copied.
            # The isolated login then creates a fresh pending identity without
            # touching the canonical source generation.
            stage_bound_camoufox_identity(staged_identity_path, canonical_profile_dir)

        async with BrowserManager(
            user_data_dir=login_profile_dir,
            headless=False,
            slow_mo=config.browser.slow_mo,
            user_agent=config.browser.user_agent,
            viewport=viewport,
            engine=config.browser.browser_engine,
            camoufox_identity_path=staged_identity_path,
            **launch_options,
        ) as browser:
            # Navigate to LinkedIn login
            await browser.page.goto("https://www.linkedin.com/login")
            # Let LinkedIn finish rendering the saved-account chooser, then retry the
            # same exact click target a few times before falling back to the normal
            # manual-login wait loop.
            for _ in range(3):
                await asyncio.sleep(2)
                if await resolve_remember_me_prompt(browser.page):
                    break

            # Wait for manual login completion. The budget comes from
            # LOGIN_TIMEOUT (config.browser.login_timeout_seconds); 0 = unlimited.
            await wait_for_manual_login(browser.page, timeout=login_timeout_ms)

            # Wait for persistent context to flush cookies to disk
            await asyncio.sleep(2)

            # Verify session cookie was persisted
            cookies = await browser.context.cookies()
            li_at = [
                cookie
                for cookie in cookies
                if cookie.get("name") == "li_at"
                and isinstance(cookie.get("value"), str)
                and bool(cookie["value"].strip())
            ]
            login_ready = bool(li_at)
            if not login_ready:
                print(
                    "   Warning: Session cookie not found. Login may not have persisted."
                )
                print("   Waiting longer for cookie propagation...")
                await asyncio.sleep(5)
                cookies = await browser.context.cookies()
                li_at = [
                    cookie
                    for cookie in cookies
                    if cookie.get("name") == "li_at"
                    and isinstance(cookie.get("value"), str)
                    and bool(cookie["value"].strip())
                ]
                login_ready = bool(li_at)
                if not login_ready:
                    print("   Error: no usable li_at cookie was persisted after login.")

            # Stage the portable snapshot, but do not publish it or source-state
            # until __aexit__ confirms that the isolated browser released its profile.
            if login_ready and not await browser.export_cookies(staged_cookie_path):
                print(
                    "   Warning: cookie export failed; Docker bridge may not work. "
                    "Run --login again to retry."
                )
                login_ready = False
    except BrowserTeardownError:
        # The context manager reports uncertain teardown by raising, possibly
        # while propagating another error. Keep the owner lease open so logout
        # cannot delete a profile that a browser may still hold.
        pending_lease.retain_until_exit()
        raise
    except BaseException:
        # All other context-manager exits confirmed teardown. Remove staged
        # cookies immediately so ordinary network/login errors leave no secret
        # pending artifact behind in a long-lived caller.
        pending_lease.release()
        shutil.rmtree(pending_root, ignore_errors=True)
        raise

    # __aexit__ confirmed teardown, so no process needs to guard this profile.
    pending_lease.release()

    # Reaching this point proves BrowserManager.__aexit__ confirmed teardown.
    # Remove the disposable browser profile before publishing any canonical
    # session artifact. An uncertain teardown raises above and leaves this
    # unique directory abandoned rather than risking a live-profile deletion.
    if login_profile_dir.exists():
        shutil.rmtree(login_profile_dir)
    if staged_identity_path is not None:
        staged_identity_path.with_name(f".{staged_identity_path.name}.lock").unlink(
            missing_ok=True
        )

    if not login_ready:
        shutil.rmtree(pending_root, ignore_errors=True)
        return False

    # Record the effective override UA the cookie was minted under. Camoufox
    # deliberately ignores overrides to keep its Firefox fingerprint coherent.
    effective_user_agent = (
        None
        if config.browser.browser_engine == "camoufox"
        else config.browser.user_agent
    )
    commit_options: dict[str, Any] = {"user_agent": effective_user_agent}
    if staged_identity_path is not None:
        commit_options["staged_camoufox_identity_path"] = staged_identity_path
    try:
        source_state = commit_source_session(
            staged_cookie_path, canonical_profile_dir, **commit_options
        )
    finally:
        # The browser profile was already released and removed, so cleaning the
        # now-private pending directory is safe even if commit rolled back.
        shutil.rmtree(pending_root, ignore_errors=True)
    print("   Cookies exported for Docker portability")
    print(f"   Source session generation: {source_state.login_generation}")
    print(f"Session saved under {auth_root_dir(canonical_profile_dir)}")
    return True


def run_profile_creation(user_data_dir: str | None = None) -> bool:
    """
    Create profile via interactive login with persistent context.

    Args:
        user_data_dir: Path to profile directory. Defaults to config's user_data_dir.

    Returns:
        True if profile was created successfully
    """
    if user_data_dir:
        profile_dir = Path(user_data_dir).expanduser()
    else:
        profile_dir = get_profile_dir()

    print("LinkedIn MCP Server - Profile Creation")
    print(f"   Profile will be saved to: {profile_dir}")

    try:
        success = asyncio.run(interactive_login(profile_dir))
        return success
    except Exception as e:
        print(f"Profile creation failed: {e}")
        return False


def run_interactive_setup() -> bool:
    """
    Run interactive setup - browser login only.

    Returns:
        True if setup completed successfully
    """
    print("LinkedIn MCP Server Setup")
    print("   Opening browser for manual login...")

    try:
        return asyncio.run(interactive_login())
    except Exception as e:
        print(f"Login failed: {e}")
        return False
