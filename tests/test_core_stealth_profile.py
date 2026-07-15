"""Tests for named stealth-posture presets (core/stealth_profile.py)."""

import pytest

from linkedin_mcp_server.core.stealth_profile import (
    DEFAULT_STEALTH_PROFILE_NAME,
    STEALTH_PROFILE_NAMES,
    NavigationMode,
    SimulationLevel,
    StealthProfile,
    get_stealth_profile,
)


class TestGetStealthProfile:
    def test_default_is_minimal_stealth(self):
        assert DEFAULT_STEALTH_PROFILE_NAME == "MINIMAL_STEALTH"
        profile = get_stealth_profile()
        assert profile.name == "MINIMAL_STEALTH"

    def test_none_resolves_to_default(self):
        assert get_stealth_profile(None).name == DEFAULT_STEALTH_PROFILE_NAME

    def test_case_insensitive(self):
        assert get_stealth_profile("maximum_stealth").name == "MAXIMUM_STEALTH"
        assert get_stealth_profile("Maximum_Stealth").name == "MAXIMUM_STEALTH"

    def test_whitespace_tolerant(self):
        assert get_stealth_profile("  no_stealth  ").name == "NO_STEALTH"

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="Unknown stealth profile"):
            get_stealth_profile("bogus")

    def test_unknown_name_error_lists_valid_names(self):
        with pytest.raises(ValueError, match="MINIMAL_STEALTH"):
            get_stealth_profile("bogus")

    def test_returns_a_fresh_instance_each_call(self):
        """Mutating one caller's profile must not leak into another's."""
        a = get_stealth_profile("NO_STEALTH")
        b = get_stealth_profile("NO_STEALTH")
        assert a is not b
        a.rate_limit_per_minute = 999
        assert b.rate_limit_per_minute != 999

    def test_all_names_constant_matches_presets(self):
        assert set(STEALTH_PROFILE_NAMES) == {
            "NO_STEALTH",
            "MINIMAL_STEALTH",
            "MODERATE_STEALTH",
            "MAXIMUM_STEALTH",
        }
        for name in STEALTH_PROFILE_NAMES:
            assert get_stealth_profile(name).name == name


class TestPresetShapes:
    """Spot-check the characteristic, behaviorally-load-bearing fields per
    preset -- not every field (that would just restate the source)."""

    def test_no_stealth_disables_everything(self):
        p = get_stealth_profile("NO_STEALTH")
        assert p.navigation == NavigationMode.DIRECT
        assert p.simulation == SimulationLevel.NONE
        assert p.lazy_loading is False
        assert p.enable_fingerprint_masking is False
        assert p.session_warming is False

    def test_minimal_stealth_is_the_default_posture(self):
        p = get_stealth_profile("MINIMAL_STEALTH")
        assert p.navigation == NavigationMode.DIRECT
        assert p.simulation == SimulationLevel.BASIC
        assert p.lazy_loading is True

    def test_moderate_stealth_still_navigates_direct(self):
        p = get_stealth_profile("MODERATE_STEALTH")
        assert p.navigation == NavigationMode.DIRECT
        assert p.simulation == SimulationLevel.MODERATE

    def test_maximum_stealth_uses_search_first_navigation(self):
        p = get_stealth_profile("MAXIMUM_STEALTH")
        assert p.navigation == NavigationMode.SEARCH_FIRST
        assert p.simulation == SimulationLevel.COMPREHENSIVE

    def test_rate_limit_gets_stricter_with_more_stealth(self):
        no_stealth = get_stealth_profile("NO_STEALTH")
        minimal = get_stealth_profile("MINIMAL_STEALTH")
        moderate = get_stealth_profile("MODERATE_STEALTH")
        maximum = get_stealth_profile("MAXIMUM_STEALTH")
        assert (
            no_stealth.rate_limit_per_minute
            > minimal.rate_limit_per_minute
            >= moderate.rate_limit_per_minute
            >= maximum.rate_limit_per_minute
        )

    def test_delay_ranges_are_valid_low_high_pairs(self):
        for name in STEALTH_PROFILE_NAMES:
            delays = get_stealth_profile(name).delays
            for pair in (
                delays.base,
                delays.reading,
                delays.navigation,
                delays.typing,
                delays.scroll,
            ):
                low, high = pair
                assert 0 <= low <= high


def test_stealth_profile_is_a_plain_dataclass_not_a_singleton():
    a = StealthProfile(
        name="custom",
        navigation=NavigationMode.DIRECT,
        delays=get_stealth_profile("MINIMAL_STEALTH").delays,
        simulation=SimulationLevel.NONE,
    )
    assert a.name == "custom"
    assert a.rate_limit_per_minute == 1  # dataclass default, not preset-derived
