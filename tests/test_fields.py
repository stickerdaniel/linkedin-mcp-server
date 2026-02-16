"""Tests for scraping field flag enums and section parsers."""

from linkedin_mcp_server.scraping.fields import (
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
            PersonScrapingFields.ACCOMPLISHMENTS,
            PersonScrapingFields.CONTACTS,
        ]
        for i, a in enumerate(flags):
            for b in flags[i + 1 :]:
                assert a & b == PersonScrapingFields(0)

    def test_flag_bitwise_or(self):
        combined = PersonScrapingFields.BASIC_INFO | PersonScrapingFields.CONTACTS
        assert PersonScrapingFields.BASIC_INFO in combined
        assert PersonScrapingFields.CONTACTS in combined
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
        assert parse_person_sections(None) == PersonScrapingFields.BASIC_INFO

    def test_empty_string_returns_basic_info_only(self):
        assert parse_person_sections("") == PersonScrapingFields.BASIC_INFO

    def test_single_section(self):
        result = parse_person_sections("contacts")
        assert result == PersonScrapingFields.BASIC_INFO | PersonScrapingFields.CONTACTS

    def test_multiple_sections(self):
        result = parse_person_sections("experience,education")
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        assert result == expected

    def test_invalid_names_ignored(self):
        result = parse_person_sections("experience,bogus,education")
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        assert result == expected

    def test_whitespace_and_case_handling(self):
        result = parse_person_sections(" Experience , EDUCATION ")
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
        )
        assert result == expected

    def test_all_sections(self):
        result = parse_person_sections(
            "experience,education,interests,accomplishments,contacts"
        )
        expected = (
            PersonScrapingFields.BASIC_INFO
            | PersonScrapingFields.EXPERIENCE
            | PersonScrapingFields.EDUCATION
            | PersonScrapingFields.INTERESTS
            | PersonScrapingFields.ACCOMPLISHMENTS
            | PersonScrapingFields.CONTACTS
        )
        assert result == expected


class TestParseCompanySections:
    def test_none_returns_about_only(self):
        assert parse_company_sections(None) == CompanyScrapingFields.ABOUT

    def test_empty_string_returns_about_only(self):
        assert parse_company_sections("") == CompanyScrapingFields.ABOUT

    def test_single_section(self):
        result = parse_company_sections("posts")
        assert result == CompanyScrapingFields.ABOUT | CompanyScrapingFields.POSTS

    def test_multiple_sections(self):
        result = parse_company_sections("posts,jobs")
        expected = (
            CompanyScrapingFields.ABOUT
            | CompanyScrapingFields.POSTS
            | CompanyScrapingFields.JOBS
        )
        assert result == expected

    def test_invalid_names_ignored(self):
        result = parse_company_sections("posts,bogus")
        assert result == CompanyScrapingFields.ABOUT | CompanyScrapingFields.POSTS

    def test_whitespace_and_case_handling(self):
        result = parse_company_sections(" Posts , JOBS ")
        expected = (
            CompanyScrapingFields.ABOUT
            | CompanyScrapingFields.POSTS
            | CompanyScrapingFields.JOBS
        )
        assert result == expected
