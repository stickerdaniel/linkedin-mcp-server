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
3. Authoritative confirm: in that order, decrypt the one profile (keychain
   prompt for that browser only), inject the cookies into the source profile,
   and prove ``/feed/``. The first that passes is imported; a profile whose
   cookie is present but server-rejected falls through to the next.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from linkedin_mcp_server.browser_import.discovery import (
    BrowserProfile,
    discover_profiles,
)
from linkedin_mcp_server.browser_import.extract import (
    LiAtMeta,
    extract_linkedin_cookies,
    read_li_at_meta,
)
from linkedin_mcp_server.browser_import.user_agent import synthesize_user_agent
from linkedin_mcp_server.common_utils import harden_linkedin_tree, secure_write_text

from linkedin_mcp_server.exceptions import (
    CookieDecryptionError,
    NoLinkedInSessionFoundError,
)
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    write_source_state,
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


def _extract_and_stage(profile: BrowserProfile, cookie_path: Path) -> bool:
    """Decrypt *profile*'s cookies and stage them at ``cookie_path``.

    Returns ``True`` when an ``li_at`` was extracted and written (ready for
    validation), ``False`` when the profile yielded nothing usable. Holds all
    the blocking work: the OS keystore read (keychain subprocess on macOS), the
    SQLite copy/read, AES decryption, and the cookie-file write. Kept
    synchronous so the caller offloads the whole unit to a worker thread.
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
        return False
    except Exception as exc:  # noqa: BLE001 - one bad profile must not abort the run
        logger.info(
            "Skipping %s/%s: %s",
            profile.browser,
            profile.profile_dir_name,
            exc,
        )
        return False
    if not any(c.name == "li_at" for c in cookies):
        return False

    payload = json.dumps([c.to_playwright() for c in cookies], indent=2)
    secure_write_text(cookie_path, payload, mode=_PRIVATE_FILE_MODE)
    harden_linkedin_tree(cookie_path.parent)
    logger.info(
        "Validating %d LinkedIn cookies from %s/%s",
        len(cookies),
        profile.browser,
        profile.profile_dir_name,
    )
    return True


async def import_session_from_browser(
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
    cookie_path = portable_cookie_path(user_data_dir)

    staged_any = False
    for profile, _meta in live:
        if not await asyncio.to_thread(_extract_and_stage, profile, cookie_path):
            continue
        staged_any = True

        # Synthesize the source browser's UA so validation and every later
        # runtime session replay the cookie under the fingerprint it was minted
        # with (None keeps the runtime default; file I/O, so off the loop).
        user_agent = await asyncio.to_thread(synthesize_user_agent, profile)
        if await validate_imported_cookies(
            cookie_path, user_data_dir, user_agent=user_agent
        ):
            write_source_state(user_data_dir, user_agent=user_agent)
            logger.info(
                "Imported LinkedIn session from %s/%s",
                profile.browser,
                profile.profile_dir_name,
            )
            return True

        # Cookie was present but LinkedIn rejected it (revoked/remote logout).
        # Drop the partial artifacts and try the next-freshest browser.
        cookie_path.unlink(missing_ok=True)
        _reset_profile_dir(user_data_dir)
        logger.info(
            "%s/%s had an li_at but LinkedIn rejected the session; trying the "
            "next browser",
            profile.browser,
            profile.profile_dir_name,
        )

    if not staged_any:
        # Live li_at cookies were found on disk but none could be decrypted
        # (keychain key unavailable, or app-bound v20). Distinct from "decrypted
        # but LinkedIn rejected it" (False below) so the caller tells the user to
        # fix keychain access rather than to re-login.
        raise CookieDecryptionError(
            "Found a logged-in browser session but could not decrypt its "
            "cookies (the keychain key was unavailable, or the cookies use "
            "app-bound encryption). Run --login to create a session instead."
        )
    return False


def _reset_profile_dir(user_data_dir: Path) -> None:
    """Clear the seeded profile between failed attempts so cookies don't mix."""
    import shutil

    shutil.rmtree(user_data_dir, ignore_errors=True)
