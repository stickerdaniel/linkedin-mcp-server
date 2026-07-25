"""Synthesize the source browser's user agent for an imported session.

LinkedIn associates a session token with the browser fingerprint it was minted
under. Replaying an imported cookie under the runtime browser's own (different)
user agent is a needless mismatch signal, so the import derives the UA the
source browser would send and the runtime browser adopts it.

This is exact, not a guess: since Chromium's user-agent reduction (Chromium
101+), every desktop Chromium browser sends a FROZEN user agent that varies
only in the platform token and the major version — minor/build/patch are
always ``0.0.0`` and the OS token never changes. Reconstructing it therefore
needs only two inputs this module can read from disk:

1. the OS (the frozen platform token per ``sys.platform``), and
2. the browser's Chromium major version, read from ``<user_data_root>/Last
   Version`` (written by Chromium on every run) with the ``Local State``
   ``user_experience_metrics.stability.stats_version`` as fallback.

Only browsers whose version string leads with the Chromium major are eligible
(``chromium_versioned`` in the discovery registry): Chrome, Chromium, Edge and
Arc report the engine version directly, Brave prefixes it (``138.1.80.113`` =
Chromium 138 + Brave 1.80.113), Helium tracks upstream. Opera, Vivaldi,
Yandex, Whale and Cốc Cốc version independently of the engine, so no UA is
synthesized for them and the import keeps today's behavior (runtime default).
"""

from __future__ import annotations

import json
import logging
import sys

from linkedin_mcp_server.browser_import.discovery import (
    SUPPORTED_BROWSERS,
    BrowserProfile,
)

logger = logging.getLogger(__name__)

# Frozen desktop platform tokens (unchanged since the UA reduction).
_PLATFORM_TOKENS: dict[str, str] = {
    "mac": "Macintosh; Intel Mac OS X 10_15_7",
    "win": "Windows NT 10.0; Win64; x64",
    "linux": "X11; Linux x86_64",
}

# Sanity window for a Chromium major. Chromium crossed 100 in 2022; a value
# outside this window means the version file belongs to a browser's own
# (non-engine) versioning scheme and must not produce a UA.
_MIN_CHROMIUM_MAJOR = 100
_MAX_CHROMIUM_MAJOR = 999


def _platform_token() -> str | None:
    if sys.platform == "darwin":
        return _PLATFORM_TOKENS["mac"]
    if sys.platform.startswith("win"):
        return _PLATFORM_TOKENS["win"]
    if sys.platform.startswith("linux"):
        return _PLATFORM_TOKENS["linux"]
    return None


def _leading_int(component: str) -> int | None:
    """The leading digits of a version component, or None when it has none."""
    digits = ""
    for char in component:
        if char.isdigit():
            digits += char
        else:
            break
    return int(digits) if digits else None


def read_engine_version(profile: BrowserProfile) -> str | None:
    """Read the browser's version string from its user-data root.

    Prefers ``Last Version`` (a bare version string Chromium rewrites on every
    run). Falls back to ``Local State``'s
    ``user_experience_metrics.stability.stats_version`` (same number, possibly
    with a ``-64``/``-devel`` suffix). Returns None when neither is readable.
    """
    last_version = profile.user_data_root / "Last Version"
    try:
        text = last_version.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass

    try:
        payload = json.loads(profile.local_state_path.read_text(encoding="utf-8"))
        stats = (
            payload.get("user_experience_metrics", {})
            .get("stability", {})
            .get("stats_version")
        )
        if isinstance(stats, str) and stats.strip():
            # "148.0.7778.179-64" -> "148.0.7778.179"
            return stats.strip().split("-")[0]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def chromium_major(version: str) -> int | None:
    """The Chromium major from a version string, or None when implausible."""
    major = _leading_int(version.split(".")[0])
    if major is None:
        return None
    if not (_MIN_CHROMIUM_MAJOR <= major <= _MAX_CHROMIUM_MAJOR):
        return None
    return major


def synthesize_user_agent(profile: BrowserProfile) -> str | None:
    """Build the frozen UA string the source browser sends, or None.

    None (keep the runtime default) whenever any input is missing: the browser
    is not ``chromium_versioned``, the version files are unreadable, the major
    is implausible, or the OS has no frozen desktop token.
    """
    spec = SUPPORTED_BROWSERS.get(profile.browser, {})
    if not spec.get("chromium_versioned"):
        return None

    platform = _platform_token()
    if platform is None:
        return None

    version = read_engine_version(profile)
    if version is None:
        logger.debug(
            "No readable version for %s/%s; keeping runtime default UA",
            profile.browser,
            profile.profile_dir_name,
        )
        return None
    major = chromium_major(version)
    if major is None:
        logger.debug(
            "Implausible engine major %r for %s; keeping runtime default UA",
            version,
            profile.browser,
        )
        return None

    ua = (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    # Brand suffix for browsers that append their own token (currently Edge,
    # whose fork major equals the Chromium major).
    suffix = spec.get("ua_brand_suffix")
    if isinstance(suffix, str) and suffix:
        ua += f" {suffix}/{major}.0.0.0"
    return ua
