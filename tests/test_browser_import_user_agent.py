"""Tests for the imported-session user-agent synthesis (pure file I/O)."""

import json

import pytest

from linkedin_mcp_server.browser_import import user_agent as ua_mod
from linkedin_mcp_server.browser_import.discovery import BrowserProfile
from linkedin_mcp_server.browser_import.user_agent import (
    chromium_major,
    read_engine_version,
    synthesize_user_agent,
)


def _profile(tmp_path, browser="chrome"):
    root = tmp_path / browser
    root.mkdir(parents=True, exist_ok=True)
    return BrowserProfile(
        browser=browser,
        browser_label=browser,
        safe_storage_label="Chrome",
        profile_dir_name="Default",
        display_name="Personal",
        user_data_root=root,
        profile_path=root / "Default",
        cookies_db=root / "Default" / "Cookies",
        local_state_path=root / "Local State",
    )


def _force_platform(monkeypatch, platform):
    monkeypatch.setattr(ua_mod.sys, "platform", platform)


def test_chromium_major_parses_and_gates_plausibility():
    assert chromium_major("148.0.7778.179") == 148
    # Brave leads with the Chromium major before its own version.
    assert chromium_major("138.1.80.113") == 138
    # A browser-own versioning scheme (Vivaldi-style) is implausible as an
    # engine major and must not produce a UA.
    assert chromium_major("7.4.3684.55") is None
    assert chromium_major("garbage") is None
    assert chromium_major("") is None


def test_read_engine_version_prefers_last_version(tmp_path):
    profile = _profile(tmp_path)
    (profile.user_data_root / "Last Version").write_text("148.0.7778.179")
    profile.local_state_path.write_text(
        json.dumps(
            {"user_experience_metrics": {"stability": {"stats_version": "9.9.9.9-64"}}}
        )
    )
    assert read_engine_version(profile) == "148.0.7778.179"


def test_read_engine_version_falls_back_to_stats_version(tmp_path):
    profile = _profile(tmp_path)
    profile.local_state_path.write_text(
        json.dumps(
            {
                "user_experience_metrics": {
                    "stability": {"stats_version": "149.0.7827.200-64-devel"}
                }
            }
        )
    )
    # The -64/-devel suffix is stripped to the bare version.
    assert read_engine_version(profile) == "149.0.7827.200"


def test_read_engine_version_none_when_unreadable(tmp_path):
    assert read_engine_version(_profile(tmp_path)) is None


@pytest.mark.parametrize(
    ("platform", "token"),
    [
        ("darwin", "Macintosh; Intel Mac OS X 10_15_7"),
        ("win32", "Windows NT 10.0; Win64; x64"),
        ("linux", "X11; Linux x86_64"),
    ],
)
def test_synthesize_frozen_ua_per_platform(tmp_path, monkeypatch, platform, token):
    _force_platform(monkeypatch, platform)
    profile = _profile(tmp_path, "chrome")
    (profile.user_data_root / "Last Version").write_text("148.0.7778.179")
    assert synthesize_user_agent(profile) == (
        f"Mozilla/5.0 ({token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    )


def test_synthesize_brave_uses_leading_chromium_major(tmp_path, monkeypatch):
    _force_platform(monkeypatch, "darwin")
    profile = _profile(tmp_path, "brave")
    (profile.user_data_root / "Last Version").write_text("138.1.80.113")
    ua = synthesize_user_agent(profile)
    assert ua is not None and "Chrome/138.0.0.0" in ua


def test_synthesize_edge_appends_brand_suffix(tmp_path, monkeypatch):
    _force_platform(monkeypatch, "win32")
    profile = _profile(tmp_path, "edge")
    (profile.user_data_root / "Last Version").write_text("137.0.3296.52")
    ua = synthesize_user_agent(profile)
    assert ua is not None
    assert ua.endswith("Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0")


def test_synthesize_none_for_non_chromium_versioned_browser(tmp_path, monkeypatch):
    _force_platform(monkeypatch, "darwin")
    # Opera versions independently of the engine; a UA from its own major
    # would be wrong, so none is synthesized.
    profile = _profile(tmp_path, "opera")
    (profile.user_data_root / "Last Version").write_text("119.0.5497.70")
    assert synthesize_user_agent(profile) is None


def test_synthesize_none_when_version_missing_or_implausible(tmp_path, monkeypatch):
    _force_platform(monkeypatch, "darwin")
    profile = _profile(tmp_path, "chrome")
    assert synthesize_user_agent(profile) is None  # nothing on disk
    (profile.user_data_root / "Last Version").write_text("7.4.3684.55")
    assert synthesize_user_agent(profile) is None  # implausible major


def test_synthesize_none_on_unknown_platform(tmp_path, monkeypatch):
    _force_platform(monkeypatch, "sunos5")
    profile = _profile(tmp_path, "chrome")
    (profile.user_data_root / "Last Version").write_text("148.0.7778.179")
    assert synthesize_user_agent(profile) is None
