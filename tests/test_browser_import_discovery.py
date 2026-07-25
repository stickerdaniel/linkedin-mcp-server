"""Tests for browser/profile discovery (pure file I/O, locale-independent)."""

import json

import pytest

from linkedin_mcp_server.browser_import import discovery
from linkedin_mcp_server.browser_import.discovery import (
    SUPPORTED_BROWSERS,
    browser_roots,
    discover_profiles,
    enumerate_profiles,
    resolve_cookies_db,
)


def _write_local_state(root, info_cache):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Local State").write_text(
        json.dumps({"profile": {"info_cache": info_cache}})
    )


def _make_profile_dir(root, name, *, network=False, flat=True, preferences=True):
    profile = root / name
    profile.mkdir(parents=True, exist_ok=True)
    if preferences:
        (profile / "Preferences").write_text("{}")
    if network:
        (profile / "Network").mkdir(parents=True, exist_ok=True)
        (profile / "Network" / "Cookies").write_text("db")
    if flat:
        (profile / "Cookies").write_text("db")
    return profile


def test_enumerate_profiles_skips_ephemeral_and_guest(tmp_path):
    root = tmp_path / "Chrome"
    _write_local_state(
        root,
        {
            "Default": {"name": "Personal"},
            "Profile 1": {"name": "Work"},
            "Profile 2": {"name": "Throwaway", "is_ephemeral": True},
            "Guest Profile": {"name": "Guest"},
        },
    )
    for name in ("Default", "Profile 1", "Profile 2", "Guest Profile"):
        _make_profile_dir(root, name)

    profiles = enumerate_profiles(root)

    names = {dir_name for dir_name, _ in profiles}
    assert names == {"Default", "Profile 1"}
    assert ("Default", "Personal") in profiles
    assert ("Profile 1", "Work") in profiles


def test_resolve_cookies_db_prefers_network_then_flat(tmp_path):
    root = tmp_path / "Chrome"
    both = _make_profile_dir(root, "Both", network=True, flat=True)
    flat_only = _make_profile_dir(root, "Flat", network=False, flat=True)
    neither = _make_profile_dir(root, "Neither", network=False, flat=False)

    assert resolve_cookies_db(both) == both / "Network" / "Cookies"
    assert resolve_cookies_db(flat_only) == flat_only / "Cookies"
    assert resolve_cookies_db(neither) is None


def test_enumerate_profiles_fallback_globs_when_local_state_missing(tmp_path):
    root = tmp_path / "Chrome"
    root.mkdir(parents=True)
    _make_profile_dir(root, "Default")
    _make_profile_dir(root, "Profile 1")
    # A dir without Preferences must not be treated as a profile.
    (root / "Profile 9").mkdir(parents=True)

    profiles = enumerate_profiles(root)

    names = {dir_name for dir_name, _ in profiles}
    assert names == {"Default", "Profile 1"}
    # Display name falls back to dir name when Local State is absent.
    assert all(dir_name == display for dir_name, display in profiles)


def test_discover_profiles_filters_to_resolvable_cookies_db(tmp_path, monkeypatch):
    root = tmp_path / "Chrome"
    _write_local_state(
        root,
        {"Default": {"name": "Personal"}, "Profile 1": {"name": "Work"}},
    )
    _make_profile_dir(root, "Default", flat=True)
    # Profile 1 has no Cookies DB -> dropped.
    p1 = root / "Profile 1"
    p1.mkdir(parents=True)
    (p1 / "Preferences").write_text("{}")

    monkeypatch.setattr(
        discovery, "browser_roots", lambda browser=None: [("chrome", root)]
    )

    profiles = discover_profiles()

    assert len(profiles) == 1
    assert profiles[0].profile_dir_name == "Default"
    assert profiles[0].browser == "chrome"
    assert profiles[0].cookies_db == root / "Default" / "Cookies"


def test_browser_roots_only_returns_existing(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    chrome = base / SUPPORTED_BROWSERS["chrome"]["mac_subpath"]
    brave = base / SUPPORTED_BROWSERS["brave"]["mac_subpath"]
    _write_local_state(chrome, {"Default": {"name": "x"}})
    _write_local_state(brave, {"Default": {"name": "y"}})

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    all_roots = {key for key, _ in browser_roots()}
    assert {"chrome", "brave"} <= all_roots
    assert "edge" not in all_roots

    edge_roots = browser_roots("edge")
    assert edge_roots == []


def test_browser_roots_discovers_sibling_channel_without_duplicates(
    tmp_path, monkeypatch
):
    base = tmp_path / "Application Support"
    exact = base / SUPPORTED_BROWSERS["chrome"]["mac_subpath"]  # Google/Chrome
    sibling = exact.with_name(exact.name + " Beta")  # Google/Chrome Beta
    _write_local_state(exact, {"Default": {"name": "x"}})
    _write_local_state(sibling, {"Default": {"name": "y"}})

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    roots = browser_roots("chrome")

    # Both the exact root and the sibling channel are discovered, each once,
    # keyed to the same browser ("chrome").
    assert all(key == "chrome" for key, _ in roots)
    found = {root for _, root in roots}
    assert found == {exact, sibling}
    assert len(roots) == 2  # no duplicates


def test_browser_roots_dedups_root_reachable_via_two_base_dirs(tmp_path, monkeypatch):
    # Two base dirs that resolve to the same place (e.g. Windows LOCALAPPDATA and
    # APPDATA pointing at one user-data tree) must not yield the root twice.
    base = tmp_path / "Application Support"
    alias = tmp_path / "alias"
    alias.symlink_to(base, target_is_directory=True)
    chrome = base / SUPPORTED_BROWSERS["chrome"]["mac_subpath"]
    _write_local_state(chrome, {"Default": {"name": "x"}})

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base, alias]))

    roots = browser_roots("chrome")

    # Resolved paths collide, so the second base contributes nothing.
    assert len(roots) == 1
    assert roots[0][0] == "chrome"


def test_browser_roots_requires_local_state(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    chrome = base / SUPPORTED_BROWSERS["chrome"]["mac_subpath"]
    chrome.mkdir(parents=True)
    # No Local State file -> not an install.

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    assert browser_roots("chrome") == []


def test_safe_storage_label_distinct_from_key(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    edge = base / SUPPORTED_BROWSERS["edge"]["mac_subpath"]
    _write_local_state(edge, {"Default": {"name": "Edge user"}})
    _make_profile_dir(edge, "Default", flat=True)

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("edge")

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.browser == "edge"
    assert profile.safe_storage_label == "Microsoft Edge"
    assert profile.browser_label == "Microsoft Edge"
    assert profile.safe_storage_label != profile.browser


@pytest.mark.parametrize("browser", list(SUPPORTED_BROWSERS))
def test_every_browser_has_distinct_metadata(browser):
    spec = SUPPORTED_BROWSERS[browser]
    assert spec["label"]
    assert spec["safe_storage"]
    assert "mac_subpath" in spec
    assert spec.get("layout", "profiles") in {"profiles", "flat"}


@pytest.mark.parametrize(
    "key,mac_subpath,safe_storage",
    [
        ("helium", "net.imput.helium", "Helium"),
        ("yandex", "Yandex/YandexBrowser", "Yandex"),
        ("whale", "Naver/Whale", "Whale"),
        # The dir leaf "Coccoc" (lowercase) and keychain label "CocCoc" (camel)
        # differ deliberately; a typo that aligns them breaks decryption.
        ("coccoc", "Coccoc", "CocCoc"),
        # Opera / Opera GX share the "Opera" keychain account/label.
        ("opera", "com.operasoftware.Opera", "Opera"),
        ("opera_gx", "com.operasoftware.OperaGX", "Opera"),
    ],
)
def test_new_browsers_registered_with_expected_layout(key, mac_subpath, safe_storage):
    # Pin the live-verified Helium path and the Yandex/Whale tokens that gate the
    # macOS root lookup and keystore access, so a typo in the constants is caught.
    spec = SUPPORTED_BROWSERS[key]
    assert spec["mac_subpath"] == mac_subpath
    assert spec["safe_storage"] == safe_storage


@pytest.mark.parametrize("key", ["opera", "opera_gx"])
def test_opera_registered_as_flat_layout(key):
    assert SUPPORTED_BROWSERS[key]["layout"] == "flat"


def test_helium_uses_custom_keychain_service():
    # Helium renames the keychain item via change-keychain-name.patch: the
    # service is "Helium Storage Key", not the "<label> Safe Storage" pattern.
    # Live-verified against the local Helium install (imputnet/helium-macos).
    assert SUPPORTED_BROWSERS["helium"]["mac_keychain_service"] == "Helium Storage Key"


def test_mac_keychain_account_override_flows_into_profile(tmp_path, monkeypatch):
    # A registry entry whose keychain account diverges from its safe_storage
    # label (the fork-rename scenario) must propagate onto the BrowserProfile so
    # the keystore is queried for the right account. A synthetic entry exercises
    # the spec -> BrowserProfile wiring without relying on any live install.
    base = tmp_path / "Application Support"
    root = base / "Fork/User Data"
    _write_local_state(root, {"Default": {"name": "Fork user"}})
    _make_profile_dir(root, "Default", flat=True)

    monkeypatch.setitem(
        SUPPORTED_BROWSERS,
        "fork",
        {
            "label": "Fork",
            "safe_storage": "ForkLabel",
            "mac_keychain_account": "ForkAccount",
            "mac_subpath": "Fork/User Data",
            "linux_subpaths": (),
            "win_subpath": "Fork/User Data",
        },
    )
    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("fork")

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.safe_storage_label == "ForkLabel"
    assert profile.mac_keychain_account == "ForkAccount"


def test_mac_keychain_account_defaults_empty_without_override(tmp_path, monkeypatch):
    # Without the override key the field stays empty so the extract resolver
    # falls back to safe_storage_label. None of the 12 current entries set it
    # (account == safe_storage everywhere today), so a real browser stands in.
    base = tmp_path / "Application Support"
    root = base / SUPPORTED_BROWSERS["chrome"]["mac_subpath"]
    _write_local_state(root, {"Default": {"name": "Chrome user"}})
    _make_profile_dir(root, "Default", flat=True)

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("chrome")

    assert len(profiles) == 1
    assert profiles[0].mac_keychain_account == ""


@pytest.mark.parametrize(
    "key,expected_token",
    [
        ("chrome", "chrome"),
        ("chromium", "chromium"),
        ("brave", "brave"),
        ("edge", "microsoft-edge"),
        ("vivaldi", "vivaldi"),
        ("yandex", "yandex-browser"),
        ("whale", "naver-whale"),
    ],
)
def test_linux_app_token_in_registry(key, expected_token):
    # Every Linux-capable browser carries an explicit Secret Service token in the
    # registry so the resolver never falls back to a space-containing default
    # (e.g. "microsoft edge") derived from the label.
    spec = SUPPORTED_BROWSERS[key]
    assert spec["linux_subpaths"]
    assert spec["linux_app_token"] == expected_token


def test_every_linux_capable_browser_has_a_token():
    for key, spec in SUPPORTED_BROWSERS.items():
        if spec["linux_subpaths"]:
            assert spec.get("linux_app_token"), key


def test_coccoc_discovers_standard_layout(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    root = base / SUPPORTED_BROWSERS["coccoc"]["mac_subpath"]
    _write_local_state(root, {"Default": {"name": "CocCoc user"}})
    _make_profile_dir(root, "Default", flat=True)

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("coccoc")

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.browser == "coccoc"
    assert profile.safe_storage_label == "CocCoc"
    assert profile.layout == "profiles"


def test_coccoc_paths_match_hackbrowserdata():
    # macOS subpath, keychain label, and the Windows path were each re-confirmed
    # against HackBrowserData (browser_darwin.go + browser_windows.go). Pin the
    # Windows path so the source-verified value cannot silently drift.
    spec = SUPPORTED_BROWSERS["coccoc"]
    assert spec["mac_subpath"] == "Coccoc"
    assert spec["safe_storage"] == "CocCoc"
    assert spec["win_subpath"] == "CocCoc/Browser/User Data"


# ---------------------------------------------------------------------------
# Opera / Opera GX flat layout: the user-data root is the single profile
# ---------------------------------------------------------------------------


def test_opera_flat_layout_single_root_profile(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    root = base / SUPPORTED_BROWSERS["opera"]["mac_subpath"]
    # Local State at the root (gate), cookies at the root with no Default/ subdir.
    _write_local_state(root, {"Default": {"name": "Opera user"}})
    (root / "Cookies").write_text("db")

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("opera")

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.browser == "opera"
    assert profile.layout == "flat"
    assert profile.profile_dir_name == "."
    assert profile.profile_path == root
    assert profile.cookies_db == root / "Cookies"


def test_opera_flat_layout_prefers_network_cookies(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    root = base / SUPPORTED_BROWSERS["opera"]["mac_subpath"]
    _write_local_state(root, {"Default": {"name": "Opera user"}})
    (root / "Network").mkdir(parents=True)
    (root / "Network" / "Cookies").write_text("db")

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("opera")

    assert len(profiles) == 1
    assert profiles[0].cookies_db == root / "Network" / "Cookies"


def test_opera_gx_flat_layout_uses_opera_account(tmp_path, monkeypatch):
    base = tmp_path / "Application Support"
    root = base / SUPPORTED_BROWSERS["opera_gx"]["mac_subpath"]
    _write_local_state(root, {"Default": {"name": "GX user"}})
    (root / "Cookies").write_text("db")

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    profiles = discover_profiles("opera_gx")

    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.browser == "opera_gx"
    assert profile.layout == "flat"
    # GX shares the "Opera" keychain account/label.
    assert profile.safe_storage_label == "Opera"


def test_flat_layout_no_cookies_db_dropped(tmp_path, monkeypatch):
    # A flat root with Local State but no Cookies DB must still be filtered out.
    base = tmp_path / "Application Support"
    root = base / SUPPORTED_BROWSERS["opera"]["mac_subpath"]
    _write_local_state(root, {"Default": {"name": "Opera user"}})

    monkeypatch.setattr(discovery, "_os_base_dirs", lambda: ("mac", [base]))

    assert discover_profiles("opera") == []


def test_enumerate_profiles_flat_layout_returns_root_dot():
    # Driven off the layout field, not the browser name: a flat layout yields a
    # single "." profile_dir_name regardless of info_cache contents.
    from pathlib import Path

    profiles = enumerate_profiles(Path("/nonexistent/root"), layout="flat")
    assert profiles == [(".", "root")]


def test_opera_windows_path_under_appdata():
    # Windows Opera lives under %APPDATA% (Roaming); pin the Roaming path against
    # a regression that would point it at LOCALAPPDATA.
    assert SUPPORTED_BROWSERS["opera"]["win_subpath"] == "Opera Software/Opera Stable"
    assert (
        SUPPORTED_BROWSERS["opera_gx"]["win_subpath"]
        == "Opera Software/Opera GX Stable"
    )
