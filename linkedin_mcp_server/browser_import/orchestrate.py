"""Discovery -> rank -> extract -> validate -> persist for browser import.

This module is imported lazily (see ``__init__``) because it pulls in
``drivers.browser`` for the validation step.

Selection is a three-tier test, cheapest first, so the macOS keychain is only
touched for the browser we actually import from:

1. Keychain-free pre-filter: a profile is a *live* candidate only if it has an
   ``li_at`` cookie whose ``expires`` is in the future. Reads the plaintext
   SQLite columns, no value decryption, no keychain prompt. Drops browsers with
   no LinkedIn login and expired/logged-out sessions.
2. Recency ranking (also keychain-free): live candidates are ordered by
   ``li_at.last_access`` descending, so the browser the user most recently used
   LinkedIn in is tried first.
3. Authoritative confirm: in that order, decrypt one profile (keychain prompt
   for that browser only), inject the cookies into a unique isolated validation
   profile, and prove ``/feed/``. Only the first successful candidate is
   transactionally published as the canonical source snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from uuid import uuid4

from linkedin_mcp_server.browser_import.discovery import (
    BrowserProfile,
    discover_profiles,
)
from linkedin_mcp_server.browser_import.extract import (
    LiAtMeta,
    LinkedInCookie,
    extract_linkedin_cookies,
    read_li_at_meta,
)
from linkedin_mcp_server.browser_import.user_agent import synthesize_user_agent
from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    secure_write_text,
)
import linkedin_mcp_server.config as config_module
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    BrowserTeardownError,
)

from linkedin_mcp_server.exceptions import (
    CookieDecryptionError,
    NoLinkedInSessionFoundError,
)
from linkedin_mcp_server.session_state import (
    acquire_pending_profile_lease,
    auth_root_dir,
    commit_source_session,
    source_session_lock,
)

logger = logging.getLogger(__name__)

_PRIVATE_FILE_MODE = 0o600


def _is_live(meta: LiAtMeta) -> bool:
    """Return whether an ``li_at`` is usable: decryptable and not expired.

    ``expires == -1.0`` marks a session cookie (no expiry) and counts as live.
    """
    if meta.app_bound:
        return False
    return meta.expires == -1.0 or meta.expires > time.time()


def rank_live_profiles(
    profiles: list[BrowserProfile],
) -> tuple[
    list[tuple[BrowserProfile, LiAtMeta]],
    list[tuple[BrowserProfile, str]],
]:
    """Filter to profiles with a live ``li_at`` and sort them by recency.

    Keychain-free: reads only plaintext cookie metadata. Returns
    ``(live, skipped)`` where ``live`` is sorted by ``li_at.last_access``
    descending (most recently used LinkedIn session first) and ``skipped``
    records why a profile with an ``li_at`` was dropped ("li_at expired" or
    "app-bound encryption") for a descriptive error when nothing is live.
    """
    live: list[tuple[BrowserProfile, LiAtMeta]] = []
    skipped: list[tuple[BrowserProfile, str]] = []

    for profile in profiles:
        try:
            meta = read_li_at_meta(profile)
        except Exception as exc:  # noqa: BLE001 - a single profile must not abort
            logger.debug(
                "Could not read li_at metadata for %s/%s: %s",
                profile.browser,
                profile.profile_dir_name,
                exc,
            )
            continue
        if meta is None:
            continue
        if meta.app_bound:
            skipped.append((profile, "app-bound encryption"))
            continue
        if not _is_live(meta):
            skipped.append((profile, "li_at expired"))
            continue
        live.append((profile, meta))

    live.sort(key=lambda pm: pm[1].last_access, reverse=True)
    return live, skipped


def _no_live_session_error(
    skipped: list[tuple[BrowserProfile, str]],
) -> Exception:
    """Build the most informative error when no live candidate was found."""
    app_bound = [p for p, reason in skipped if reason == "app-bound encryption"]
    if app_bound:
        names = ", ".join(sorted({p.browser_label for p in app_bound}))
        return CookieDecryptionError(
            f"Found a LinkedIn login in {names} but the cookies use app-bound "
            "encryption that cannot be decrypted without OS elevation. "
            "Run with --login to create a session instead."
        )
    return NoLinkedInSessionFoundError(
        "No locally logged-in browser profile with a live LinkedIn session was "
        "found. Sign into LinkedIn in a Chromium-based browser, or use --login."
    )


def _discover_and_rank(
    browser: str | None,
) -> tuple[
    list[tuple[BrowserProfile, LiAtMeta]],
    list[tuple[BrowserProfile, str]],
]:
    """Run the blocking discovery + recency ranking.

    Walks the filesystem and reads plaintext cookie metadata via SQLite. Kept
    synchronous so the caller can offload it to a worker thread in one hop.
    """
    profiles = discover_profiles(browser)
    return rank_live_profiles(profiles)


def _extract_cookies(profile: BrowserProfile) -> list[LinkedInCookie] | None:
    """Decrypt *profile*'s cookies without mutating source-session files.

    This is the only part offloaded to a worker thread. Cancellation cannot stop
    ``asyncio.to_thread``, so the worker must remain read-only: committing the
    portable snapshot happens back on the owning event-loop task while it still
    holds the cross-process source lock.
    """
    try:
        cookies = extract_linkedin_cookies(profile)
    except CookieDecryptionError as exc:
        # CookieDecryptionError also covers KeystoreUnavailableError and
        # V20EncryptedError (both subclasses).
        logger.info(
            "Skipping %s/%s: %s",
            profile.browser,
            profile.profile_dir_name,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - one bad profile must not abort the run
        logger.info(
            "Skipping %s/%s: %s",
            profile.browser,
            profile.profile_dir_name,
            exc,
        )
        return None
    if not any(
        cookie.name == "li_at" and bool(cookie.value.strip()) for cookie in cookies
    ):
        return None

    return cookies


def _stage_cookies(
    cookies: list[LinkedInCookie], profile: BrowserProfile, cookie_path: Path
) -> None:
    """Atomically publish extracted cookies from the lock-owning event-loop task."""

    payload = json.dumps([c.to_playwright() for c in cookies], indent=2)
    secure_write_text(cookie_path, payload, mode=_PRIVATE_FILE_MODE)
    harden_linkedin_tree(cookie_path.parent)
    logger.info(
        "Validating %d LinkedIn cookies from %s/%s",
        len(cookies),
        profile.browser,
        profile.profile_dir_name,
    )


async def import_session_from_browser(
    browser: str | None,
    *,
    user_data_dir: Path,
) -> bool:
    """Import while exclusively owning all canonical source-session artifacts."""
    if config_module.get_config().browser.browser_engine == "camoufox":
        raise AuthenticationError(
            "Importing a Chromium browser session into Camoufox is not supported: "
            "the LinkedIn session was minted under a different complete browser "
            "fingerprint. Run with --browser camoufox --login instead."
        )
    async with source_session_lock(user_data_dir):
        return await _import_session_from_browser_unlocked(
            browser, user_data_dir=user_data_dir
        )


async def _import_session_from_browser_unlocked(
    browser: str | None,
    *,
    user_data_dir: Path,
) -> bool:
    """Discover, rank, decrypt, validate and persist a browser LinkedIn session.

    Ranks live candidates by recency and validates them in order, importing the
    first whose cookies prove ``/feed/``. Writes the FULL LinkedIn cookie set to
    ``cookies.json`` (matching a real login's on-disk superset) and persists
    ``source-state.json`` so the same-host server reads the seeded profile back.

    The blocking steps -- profile discovery, per-profile SQLite reads, the OS
    keystore access (a ``security`` subprocess on macOS), and AES decryption --
    run in worker threads via ``asyncio.to_thread`` so the live server's event
    loop stays responsive instead of freezing for the duration of a keychain
    read. Only the patchright validation runs on the loop.

    Returns ``True`` on a validated, persisted session, ``False`` when a live
    ``li_at`` was found but no browser's session was accepted by LinkedIn.
    """
    from linkedin_mcp_server.drivers.browser import validate_imported_cookies

    live, skipped = await asyncio.to_thread(_discover_and_rank, browser)
    if not live:
        raise _no_live_session_error(skipped)

    logger.info(
        "Found %d browser profile(s) with a live LinkedIn session; trying most "
        "recently used first",
        len(live),
    )
    pending_root = auth_root_dir(user_data_dir) / f".import-pending-{uuid4().hex}"
    secure_mkdir(pending_root)
    pending_lease = acquire_pending_profile_lease(pending_root)
    pending_cookie_path = pending_root / "cookies.json"

    try:
        staged_any = False
        for profile, _meta in live:
            cookies = await asyncio.to_thread(_extract_cookies, profile)
            if not cookies:
                continue
            staged_any = True
            _stage_cookies(cookies, profile, pending_cookie_path)

            # Synthesize the source browser's UA so validation and every later
            # runtime session replay the cookie under the fingerprint it was minted
            # with (None keeps the runtime default; file I/O, so off the loop).
            user_agent = await asyncio.to_thread(synthesize_user_agent, profile)
            validation_profile = pending_root / f"validation-profile-{uuid4().hex}"
            try:
                validated = await validate_imported_cookies(
                    pending_cookie_path,
                    validation_profile,
                    user_agent=user_agent,
                )
            except BrowserTeardownError as exc:
                # The isolated validator's teardown state is inconclusive.
                # Keep its unique owner lease until this process exits.
                logger.warning(
                    "%s/%s: infra error validating session; aborting import "
                    "(isolated pending transaction preserved at %s): %s",
                    profile.browser,
                    profile.profile_dir_name,
                    pending_root,
                    exc,
                )
                pending_lease.retain_until_exit()
                raise

            # validate_imported_cookies only returns after confirmed teardown.
            if validation_profile.exists():
                shutil.rmtree(validation_profile)

            if validated:
                # No browser owns the pending directory now. Release its lease
                # before publishing/cleaning the inert staged artifacts.
                pending_lease.release()
                try:
                    commit_source_session(
                        pending_cookie_path,
                        user_data_dir,
                        user_agent=user_agent,
                    )
                finally:
                    # Commit either succeeded or restored every canonical artifact.
                    shutil.rmtree(pending_root, ignore_errors=True)
                logger.info(
                    "Imported LinkedIn session from %s/%s",
                    profile.browser,
                    profile.profile_dir_name,
                )
                return True

            # Cookie was present but LinkedIn rejected it (revoked/remote logout).
            # Canonical source artifacts remain untouched and validation never
            # opens the canonical browser profile.
            logger.info(
                "%s/%s had an li_at but LinkedIn rejected the session; trying the "
                "next browser",
                profile.browser,
                profile.profile_dir_name,
            )

        if not staged_any:
            # Live li_at cookies were found on disk but none could be decrypted
            # (keychain key unavailable, or app-bound v20). Distinct from
            # "decrypted but LinkedIn rejected it" (False below).
            raise CookieDecryptionError(
                "Found a logged-in browser session but could not decrypt its "
                "cookies (the keychain key was unavailable, or the cookies use "
                "app-bound encryption). Run --login to create a session instead."
            )
    except BaseException:
        if not pending_lease.retained:
            # No browser ownership is uncertain. Drop decrypted cookies and any
            # partial staged identity immediately on every other failure path.
            pending_lease.release()
            shutil.rmtree(pending_root, ignore_errors=True)
        raise

    pending_lease.release()
    shutil.rmtree(pending_root, ignore_errors=True)
    return False
