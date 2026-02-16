"""Tests for scraping field flag enums and section parsers."""

from linkedin_mcp_server.scraping.fields import (
    COMPANY_SECTION_MAP,
    PERSON_SECTION_MAP,
    CompanyScrapingFields,
    PersonScrapingFields,
    parse_company_sections,
    parse_person_sections,
)


class TestPersonScrapingFields:
    def test_atomic_flags_are_distinct(self):
        flags = [
            PersonScrapingFields.BASIC_INFO,
            PersonScrapingFields.EXPERIENCE,
            PersonScrapingFields.EDUCATION,
            PersonScrapingFields.INTERESTS,
            PersonScrapingFields.HONORS,
            PersonScrapingFields.LANGUAGES,
            PersonScrapingFields.CONTACT_INFO,
        ]
        for i, a in enumerate(flags):
            for b in flags[i + 1 :]:
                assert a & b == PersonScrapingFields(0)

    def test_flag_bitwise_or(self):
        combined = PersonScrapingFields.BASIC_INFO | PersonScrapingFields.CONTACT_INFO
        assert PersonScrapingFields.BASIC_INFO in combined
        assert PersonScrapingFields.CONTACT_INFO in combined
        assert PersonScrapingFields.EXPERIENCE not in combined


class TestCompanyScrapingFields:
    def test_atomic_flags_are_distinct(self):
        flags = [
            CompanyScrapingFields.ABOUT,
            CompanyScrapingFields.POSTS,
            CompanyScrapingFields.JOBS,
        ]
        for i, a in enumerate(flags):
            for b in flags[i + 1 :]:
                assert a & b == CompanyScrapingFields(0)


class TestParsePersonSections:
    def test_none_returns_basic_info_only(self):
        flags, unknown = parse_person_sections(None)
        assert flags == PersonScrapingFields.BASIC_INFO
        assert unknown == []

    def test_empty_string_returns_basic_info_only(self):
        flags, unknown = parse_person_sections("")
        assert flags == PersonScrapingFields.BASIC_INFO
        assert unknown == []

    def test_single_section(self):
        flags, unknown = parse_person_sections("contact_info")
        assert (
            flags == PersonScrapingFields.BASIC_INFO | PersonScrapingFields.CONTACT_INFO
        )
        assert unknown == []

    def test_multiple_sections(self):
        flags, unknown = parse_person_sections("experience,education")
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        assert flags == expected
        assert unknown == []

    def test_invalid_names_returned(self):
        flags, unknown = parse_person_sections("experience,bogus,education")
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        assert flags == expected
        assert unknown == ["bogus"]

    def test_multiple_invalid_names(self):
        flags, unknown = parse_person_sections("experience,foo,bar")
        assert (
            flags == PersonScrapingFields.BASIC_INFO | PersonScrapingFields.EXPERIENCE
        )
        assert unknown == ["foo", "bar"]

    def test_whitespace_and_case_handling(self):
        flags, unknown = parse_person_sections(" Experience , EDUCATION ")
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        assert flags == expected
        assert unknown == []

    def test_all_sections(self):
        flags, unknown = parse_person_sections(
            "experience,education,interests,honors,languages,contact_info"
        )
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
            | PersonScrapingFields.INTERESTS
            | PersonScrapingFields.HONORS
            | PersonScrapingFields.LANGUAGES
            | PersonScrapingFields.CONTACT_INFO
        )
        assert flags == expected
        assert unknown == []


class TestParseCompanySections:
    def test_none_returns_about_only(self):
        flags, unknown = parse_company_sections(None)
        assert flags == CompanyScrapingFields.ABOUT
        assert unknown == []

    def test_empty_string_returns_about_only(self):
        flags, unknown = parse_company_sections("")
        assert flags == CompanyScrapingFields.ABOUT
        assert unknown == []

    def test_single_section(self):
        flags, unknown = parse_company_sections("posts")
        assert flags == CompanyScrapingFields.ABOUT | CompanyScrapingFields.POSTS
        assert unknown == []

    def test_multiple_sections(self):
        flags, unknown = parse_company_sections("posts,jobs")
        expected = (
            CompanyScrapingFields.ABOUT
            | CompanyScrapingFields.POSTS
            | CompanyScrapingFields.JOBS
        )
        assert flags == expected
        assert unknown == []

    def test_invalid_names_returned(self):
        flags, unknown = parse_company_sections("posts,bogus")
        assert flags == CompanyScrapingFields.ABOUT | CompanyScrapingFields.POSTS
        assert unknown == ["bogus"]

    def test_whitespace_and_case_handling(self):
        flags, unknown = parse_company_sections(" Posts , JOBS ")
        expected = (
            CompanyScrapingFields.ABOUT
            | CompanyScrapingFields.POSTS
            | CompanyScrapingFields.JOBS
        )
        assert flags == expected
        assert unknown == []


class TestSectionMapCoverage:
    """Ensure every non-baseline flag has a section map entry (drift risk)."""

    def test_person_section_map_covers_all_flags(self):
        baseline = PersonScrapingFields.BASIC_INFO
        mapped_flags = set(PERSON_SECTION_MAP.values())
        for flag in PersonScrapingFields:
            if flag is baseline:
                continue
            assert flag in mapped_flags, f"{flag.name} missing from PERSON_SECTION_MAP"

    def test_company_section_map_covers_all_flags(self):
        baseline = CompanyScrapingFields.ABOUT
        mapped_flags = set(COMPANY_SECTION_MAP.values())
        for flag in CompanyScrapingFields:
            if flag is baseline:
                continue
            assert flag in mapped_flags, f"{flag.name} missing from COMPANY_SECTION_MAP"
