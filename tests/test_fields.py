"""Tests for scraping section config dicts and section parsers."""

from linkedin_mcp_server.scraping.fields import (
    COMPANY_SECTIONS,
    PERSON_SECTIONS,
    parse_company_sections,
    parse_person_sections,
)


class TestPersonSections:
    def test_expected_keys(self):
        expected = {
            "main_profile",
            "experience",
            "education",
            "interests",
            "honors",
            "languages",
            "contact_info",
            "posts",
        }
        assert set(PERSON_SECTIONS) == expected

    def test_contact_info_is_overlay(self):
        _suffix, is_overlay = PERSON_SECTIONS["contact_info"]
        assert is_overlay is True

    def test_non_overlay_sections(self):
        for name, (_suffix, is_overlay) in PERSON_SECTIONS.items():
            if name != "contact_info":
                assert is_overlay is False, f"{name} should not be an overlay"

    def test_all_suffixes_start_with_slash(self):
        for name, (suffix, _) in PERSON_SECTIONS.items():
            assert suffix.startswith("/"), f"{name} suffix should start with /"


class TestCompanySections:
    def test_expected_keys(self):
        assert set(COMPANY_SECTIONS) == {"about", "posts", "jobs"}

    def test_no_overlays(self):
        for name, (_suffix, is_overlay) in COMPANY_SECTIONS.items():
            assert is_overlay is False, f"{name} should not be an overlay"


class TestParsePersonSections:
    def test_none_returns_baseline_only(self):
        requested, unknown = parse_person_sections(None)
        assert requested == {"main_profile"}
        assert unknown == []

    def test_empty_string_returns_baseline_only(self):
        requested, unknown = parse_person_sections("")
        assert requested == {"main_profile"}
        assert unknown == []

    def test_single_section(self):
        requested, unknown = parse_person_sections("contact_info")
        assert requested == {"main_profile", "contact_info"}
        assert unknown == []

    def test_multiple_sections(self):
        requested, unknown = parse_person_sections("experience,education")
        assert requested == {"main_profile", "experience", "education"}
        assert unknown == []

    def test_invalid_names_returned(self):
        requested, unknown = parse_person_sections("experience,bogus,education")
        assert requested == {"main_profile", "experience", "education"}
        assert unknown == ["bogus"]

    def test_multiple_invalid_names(self):
        requested, unknown = parse_person_sections("experience,foo,bar")
        assert requested == {"main_profile", "experience"}
        assert unknown == ["foo", "bar"]

    def test_whitespace_and_case_handling(self):
        requested, unknown = parse_person_sections(" Experience , EDUCATION ")
        assert requested == {"main_profile", "experience", "education"}
        assert unknown == []

    def test_all_sections(self):
        requested, unknown = parse_person_sections(
            "experience,education,interests,honors,languages,contact_info,posts"
        )
        assert requested == set(PERSON_SECTIONS)
        assert unknown == []


class TestParseCompanySections:
    def test_none_returns_baseline_only(self):
        requested, unknown = parse_company_sections(None)
        assert requested == {"about"}
        assert unknown == []

    def test_empty_string_returns_baseline_only(self):
        requested, unknown = parse_company_sections("")
        assert requested == {"about"}
        assert unknown == []

    def test_single_section(self):
        requested, unknown = parse_company_sections("posts")
        assert requested == {"about", "posts"}
        assert unknown == []

    def test_multiple_sections(self):
        requested, unknown = parse_company_sections("posts,jobs")
        assert requested == {"about", "posts", "jobs"}
        assert unknown == []

    def test_invalid_names_returned(self):
        requested, unknown = parse_company_sections("posts,bogus")
        assert requested == {"about", "posts"}
        assert unknown == ["bogus"]

    def test_whitespace_and_case_handling(self):
        requested, unknown = parse_company_sections(" Posts , JOBS ")
        assert requested == {"about", "posts", "jobs"}
        assert unknown == []


class TestConfigCompleteness:
    """Ensure every config dict section has a valid suffix."""

    def test_person_sections_all_have_suffixes(self):
        for name, (suffix, _) in PERSON_SECTIONS.items():
            assert isinstance(suffix, str) and len(suffix) > 0, (
                f"{name} has empty suffix"
            )

    def test_company_sections_all_have_suffixes(self):
        for name, (suffix, _) in COMPANY_SECTIONS.items():
            assert isinstance(suffix, str) and len(suffix) > 0, (
                f"{name} has empty suffix"
            )
