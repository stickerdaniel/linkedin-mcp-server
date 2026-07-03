"""Locate Chromium-family browsers and their profiles (pure file I/O).

No cryptography and no Playwright here: this module only finds where a
browser's user-data root lives, which profiles it holds, and where each
profile's Cookies database is. Classification is locale-independent --
it keys off directory names (``Default`` / ``Profile N`` are never localized)
and ``Local State`` JSON structure, never display strings.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserProfile:
    """One discoverable browser profile with a resolvable Cookies database."""

    browser: str  # canonical registry key, see SUPPORTED_BROWSERS
    browser_label: str  # human label for TTY prompts: "Google Chrome"
    safe_storage_label: str  # macOS keychain service token: "Chrome", "Brave", ...
    profile_dir_name: str  # "Default" | "Profile 1" | ...
    display_name: str  # Local State info_cache "name" (TTY only, never logged)
    user_data_root: Path  # the dir containing "Local State"
    profile_path: Path  # user_data_root / profile_dir_name
    cookies_db: Path  # resolved Cookies path (Network/Cookies preferred, else Cookies)
    local_state_path: Path  # user_data_root / "Local State"
    # Full macOS keychain service name. Empty -> the default "<safe_storage>
    # Safe Storage" pattern. Set for forks that rename it (e.g. Helium uses
    # "Helium Storage Key", not the "... Safe Storage" suffix).
    mac_keychain_service: str = ""
    # macOS keychain ACCOUNT (-a). Stays the bare product name even when a fork
    # renames the SERVICE (e.g. Helium account "Helium" but service
    # "Helium Storage Key"). Empty -> defaults to safe_storage_label. The
    # account-first lookup is the primary key; mac_keychain_service is the
    # fallback.
    mac_keychain_account: str = ""
    # On-disk profile layout. "profiles" = standard Default/Profile N subdirs;
    # "flat" = cookies at the user-data root with no Default/ subdir (Opera and
    # Opera GX, see docs/browser-import-support.md).
    layout: str = "profiles"


# canonical_key -> per-OS layout. ``safe_storage`` is the macOS keychain service
# token (``<safe_storage> Safe Storage``); it is a distinct token from both the
# canonical key and the human label. Subpaths are relative to the per-OS base
# directory resolved in ``_os_base_dirs``.
#
# ``chromium_versioned`` marks browsers whose on-disk version string leads with
# the Chromium engine major (Chrome/Chromium/Edge/Arc report it directly, Brave
# prefixes it, Helium tracks upstream) — the input user_agent.py needs to
# synthesize the frozen UA for an imported session. Browsers that version
# independently of the engine (Opera, Vivaldi, Yandex, Whale, Cốc Cốc) omit it
# and get no synthesized UA. ``ua_brand_suffix`` is the extra brand token some
# forks append to the frozen UA (Edge: ``Edg/<major>.0.0.0``).
SUPPORTED_BROWSERS: dict[str, dict[str, object]] = {
    "chrome": {
        "label": "Google Chrome",
        "safe_storage": "Chrome",
        "mac_subpath": "Google/Chrome",
        "linux_subpaths": ("google-chrome",),
        "linux_app_token": "chrome",
        "win_subpath": "Google/Chrome/User Data",
        "chromium_versioned": True,
    },
    "chromium": {
        "label": "Chromium",
        "safe_storage": "Chromium",
        "mac_subpath": "Chromium",
        "linux_subpaths": ("chromium",),
        "linux_app_token": "chromium",
        "win_subpath": "Chromium/User Data",
        "chromium_versioned": True,
    },
    "brave": {
        "label": "Brave",
        "safe_storage": "Brave",
        "mac_subpath": "BraveSoftware/Brave-Browser",
        "linux_subpaths": ("BraveSoftware/Brave-Browser",),
        "linux_app_token": "brave",
        "win_subpath": "BraveSoftware/Brave-Browser/User Data",
        "chromium_versioned": True,
    },
    "edge": {
        "label": "Microsoft Edge",
        "safe_storage": "Microsoft Edge",
        "mac_subpath": "Microsoft Edge",
        "linux_subpaths": ("microsoft-edge",),
        "linux_app_token": "microsoft-edge",
        "win_subpath": "Microsoft/Edge/User Data",
        "chromium_versioned": True,
        "ua_brand_suffix": "Edg",
    },
    "arc": {
        "label": "Arc",
        "safe_storage": "Arc",
        "mac_subpath": "Arc/User Data",
        # Arc has no stable Linux build; omit on Linux.
        "linux_subpaths": (),
        "win_subpath": "Arc/User Data",
        "chromium_versioned": True,
    },
    "vivaldi": {
        "label": "Vivaldi",
        "safe_storage": "Vivaldi",
        "mac_subpath": "Vivaldi",
        "linux_subpaths": ("vivaldi",),
        "linux_app_token": "vivaldi",
        "win_subpath": "Vivaldi/User Data",
    },
    # Helium (imput.net): standard Chromium layout verified on macOS
    # (~/Library/Application Support/net.imput.helium, flat Default/Cookies,
    # multiple profiles). The keychain token is created on first cookie
    # encryption; "Helium" is the product name. No Linux build today.
    "helium": {
        "label": "Helium",
        "safe_storage": "Helium",
        # Helium renames the keychain item via change-keychain-name.patch:
        # service "Helium Storage Key" (NOT "Helium Safe Storage"), account
        # "Helium". Verified against imputnet/helium-macos.
        "mac_keychain_service": "Helium Storage Key",
        "mac_subpath": "net.imput.helium",
        "linux_subpaths": (),
        "win_subpath": "net.imput.helium/User Data",
        "chromium_versioned": True,
    },
    # Standard-Chromium browsers. Paths and keychain labels cross-checked against
    # yt-dlp (yt_dlp/cookies.py) and HackBrowserData (browser/browser_darwin.go):
    # both use the standard "<label> Safe Storage" service. A wrong token still
    # fails closed (KeystoreUnavailableError -> "undecryptable"), and a root
    # without a Local State file is never treated as installed.
    # See docs/browser-import-support.md.
    "yandex": {
        "label": "Yandex",
        "safe_storage": "Yandex",
        "mac_subpath": "Yandex/YandexBrowser",
        "linux_subpaths": ("yandex-browser",),
        "linux_app_token": "yandex-browser",
        "win_subpath": "Yandex/YandexBrowser/User Data",
    },
    "whale": {
        "label": "Naver Whale",
        "safe_storage": "Whale",
        "mac_subpath": "Naver/Whale",
        "linux_subpaths": ("naver-whale",),
        "linux_app_token": "naver-whale",
        "win_subpath": "Naver/Naver Whale/User Data",
    },
    "coccoc": {
        "label": "Cốc Cốc",
        "safe_storage": "CocCoc",  # macOS keychain service is "CocCoc Safe Storage"
        # dir leaf "Coccoc" (lowercase c's) vs keychain label "CocCoc" (camel) is
        # intentional; cross-checked against HackBrowserData browser_darwin.go.
        "mac_subpath": "Coccoc",
        "linux_subpaths": (),  # no verified Linux build in the sources
        "linux_app_token": "",  # unused (no Linux build)
        "win_subpath": "CocCoc/Browser/User Data",
    },
    # Opera / Opera GX: flat layout (cookies at the user-data ROOT, no Default/
    # subdir). Local State still sits at the root, so the install gate is
    # unchanged. macOS keychain account "Opera" for BOTH (cross-checked:
    # HackBrowserData browser_darwin.go KeychainLabel "Opera", yt-dlp
    # cookies.py keyring_name "Opera"). Windows path is under %APPDATA%
    # (Roaming), which _os_base_dirs already searches. No Opera GX Linux build.
    "opera": {
        "label": "Opera",
        "safe_storage": "Opera",
        "mac_subpath": "com.operasoftware.Opera",
        "linux_subpaths": ("opera",),
        "linux_app_token": "opera",
        "win_subpath": "Opera Software/Opera Stable",
        "layout": "flat",
    },
    "opera_gx": {
        "label": "Opera GX",
        "safe_storage": "Opera",  # GX shares the "Opera" keychain account/label
        "mac_subpath": "com.operasoftware.OperaGX",
        "linux_subpaths": (),  # no Opera GX build on Linux
        "linux_app_token": "",
        "win_subpath": "Opera Software/Opera GX Stable",
        "layout": "flat",
    },
}


def _os_base_dirs() -> tuple[str, list[Path]]:
    """Return the current OS key and the base directories browsers live under."""
    if sys.platform == "darwin":
        return "mac", [Path.home() / "Library" / "Application Support"]
    if os.name == "nt":
        bases: list[Path] = []
        for env_var in ("LOCALAPPDATA", "APPDATA"):
            value = os.environ.get(env_var)
            if value:
                bases.append(Path(value))
        return "win", bases
    # Default to Linux/XDG layout.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return "linux", [base]


def _subpaths_for(browser: str, os_key: str) -> tuple[str, ...]:
    spec = SUPPORTED_BROWSERS[browser]
    if os_key == "mac":
        return (str(spec["mac_subpath"]),)
    if os_key == "win":
        return (str(spec["win_subpath"]),)
    linux_subpaths = cast("tuple[str, ...]", spec["linux_subpaths"])
    return tuple(str(p) for p in linux_subpaths)


def _has_local_state(root: Path) -> bool:
    return (root / "Local State").is_file()


def browser_roots(browser: str | None = None) -> list[tuple[str, Path]]:
    """Return ``(browser_key, user_data_root)`` for installed roots on this OS.

    Restricts to *browser* when given. Globs sibling channel dirs (e.g.
    ``Chrome Beta``, ``Brave-Browser-Nightly``). Only returns roots that exist
    and contain a ``Local State`` file (so a stray empty dir is not treated as
    an install).
    """
    os_key, base_dirs = _os_base_dirs()
    keys = [browser] if browser else list(SUPPORTED_BROWSERS)
    roots: list[tuple[str, Path]] = []
    seen: set[Path] = set()

    for key in keys:
        if key not in SUPPORTED_BROWSERS:
            continue
        for subpath in _subpaths_for(key, os_key):
            for base in base_dirs:
                exact = base / subpath
                candidates = [exact]
                # Sibling channels share the parent dir and a name prefix.
                parent = exact.parent
                if parent.is_dir():
                    prefix = exact.name
                    candidates.extend(
                        sorted(
                            p
                            for p in parent.glob(f"{prefix}*")
                            if p.is_dir() and p != exact
                        )
                    )
                for root in candidates:
                    resolved = root.resolve() if root.exists() else root
                    if resolved in seen:
                        continue
                    if root.is_dir() and _has_local_state(root):
                        seen.add(resolved)
                        roots.append((key, root))
    return roots


def _read_info_cache(local_state_path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(local_state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        return {}
    info_cache = profile.get("info_cache")
    if not isinstance(info_cache, dict):
        return {}
    return {k: v for k, v in info_cache.items() if isinstance(v, dict)}


def _glob_profile_dirs(user_data_root: Path) -> list[str]:
    """Fallback: dir-glob ``Default`` + ``Profile *`` with a ``Preferences`` file."""
    names: list[str] = []
    for candidate in (
        user_data_root / "Default",
        *sorted(user_data_root.glob("Profile *")),
    ):
        if candidate.is_dir() and (candidate / "Preferences").is_file():
            names.append(candidate.name)
    return names


def _flat_display_name(user_data_root: Path) -> str:
    """TTY label for a flat-layout (Opera) root that holds a single profile."""
    info_cache = _read_info_cache(user_data_root / "Local State")
    # Flat browsers still write a Default entry in info_cache when present.
    default = info_cache.get("Default") if info_cache else None
    name = default.get("name") if isinstance(default, dict) else None
    return name if isinstance(name, str) and name else user_data_root.name


def enumerate_profiles(
    user_data_root: Path, *, layout: str = "profiles"
) -> list[tuple[str, str]]:
    """Return ``(profile_dir_name, display_name)`` for real sign-in profiles.

    Parses ``<root>/Local State`` ``profile.info_cache``; skips ephemeral and
    the special ``Guest``/``System Profile`` directories. Falls back to globbing
    ``Default`` + ``Profile *`` dirs that contain a ``Preferences`` file when
    ``Local State`` is missing or corrupt.

    With ``layout="flat"`` (Opera) the user-data root itself is the single
    profile: cookies live at the root with no ``Default/`` subdir, represented
    by the ``"."`` profile_dir_name so ``root / "." == root`` resolves them.
    """
    if layout == "flat":
        return [(".", _flat_display_name(user_data_root))]

    skip = {"Guest Profile", "System Profile"}
    info_cache = _read_info_cache(user_data_root / "Local State")
    profiles: list[tuple[str, str]] = []

    if info_cache:
        for dir_name, info in sorted(info_cache.items()):
            if dir_name in skip:
                continue
            if info.get("is_ephemeral"):
                continue
            if not (user_data_root / dir_name).is_dir():
                continue
            raw_name = info.get("name")
            display = raw_name if isinstance(raw_name, str) and raw_name else dir_name
            profiles.append((dir_name, display))
        if profiles:
            return profiles

    # Local State missing/corrupt or held no usable profiles: glob the disk.
    return [(name, name) for name in _glob_profile_dirs(user_data_root)]


def resolve_cookies_db(profile_path: Path) -> Path | None:
    """Prefer ``<p>/Network/Cookies``, then ``<p>/Cookies``. ``None`` when neither.

    Never branches on browser or version: the Network-first probe is correct
    regardless of which on-disk layout a Chromium build uses.
    """
    network_cookies = profile_path / "Network" / "Cookies"
    if network_cookies.is_file():
        return network_cookies
    flat_cookies = profile_path / "Cookies"
    if flat_cookies.is_file():
        return flat_cookies
    return None


def discover_profiles(browser: str | None = None) -> list[BrowserProfile]:
    """Cross-product ``browser_roots()`` x ``enumerate_profiles()``.

    Keeps only profiles with a resolvable Cookies DB. Does not decrypt; ``li_at``
    candidacy is decided later during extraction.
    """
    discovered: list[BrowserProfile] = []
    for browser_key, root in browser_roots(browser):
        spec = SUPPORTED_BROWSERS[browser_key]
        layout = str(spec.get("layout", "profiles"))
        for dir_name, display_name in enumerate_profiles(root, layout=layout):
            profile_path = root / dir_name  # root / "." == root for flat layout
            cookies_db = resolve_cookies_db(profile_path)
            if cookies_db is None:
                continue
            discovered.append(
                BrowserProfile(
                    browser=browser_key,
                    browser_label=str(spec["label"]),
                    safe_storage_label=str(spec["safe_storage"]),
                    profile_dir_name=dir_name,
                    display_name=display_name,
                    user_data_root=root,
                    profile_path=profile_path,
                    cookies_db=cookies_db,
                    local_state_path=root / "Local State",
                    mac_keychain_service=str(spec.get("mac_keychain_service", "")),
                    mac_keychain_account=str(spec.get("mac_keychain_account", "")),
                    layout=layout,
                )
            )
    logger.debug(
        "Discovered %d browser profile(s)%s",
        len(discovered),
        f" for {browser}" if browser else "",
    )
    return discovered
