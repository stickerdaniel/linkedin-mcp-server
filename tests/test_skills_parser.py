"""Tests for the skills text -> structured parser (scraping/skills_parser.py)."""

from linkedin_mcp_server.scraping.skills_parser import (
    parse_skills,
    skill_names_from_aria_labels,
)

# A realistic skills panel innerText (post noise-truncation), modelled on live
# data: a 99+ skill, a skill with an inline associated-experience preview + a
# "Show all N details" expander (both must NOT become skills), an assessment-only
# skill, and a bare skill with no metadata.
SKILLS_TEXT = """Skills
All
Industry Knowledge
Tools & Technologies
Interpersonal Skills

Set Top Box
6 experiences at Acme and 3 other companies
Endorsed by Muhammad Khan and 13 others who are highly skilled at this
Endorsed by 29 mutual connections
99+ endorsements

Software Architecture
Director, Solutions Architecture at Acme
Show all 4 details
Endorsed by 2 colleagues at Acme
32 endorsements

IPTV
Passed LinkedIn Skill Assessment

Conditional Access"""

NAMES = ["Set Top Box", "Software Architecture", "IPTV", "Conditional Access"]


class TestSkillNamesFromAriaLabels:
    def test_extracts_edit_and_endorse_variants_in_order(self):
        labels = [
            "Navigate back to profile main screen",
            "Edit Set Top Box skill",
            "Endorse Software Architecture",
            "Endorsed IPTV",
            "Dismiss",
            "Report this ad",
        ]
        assert skill_names_from_aria_labels(labels) == [
            "Set Top Box",
            "Software Architecture",
            "IPTV",
        ]

    def test_dedupes_preserving_first_order(self):
        labels = ["Edit DASH skill", "Endorse DASH", "Edit VOD skill"]
        assert skill_names_from_aria_labels(labels) == ["DASH", "VOD"]

    def test_ignores_non_skill_labels(self):
        assert skill_names_from_aria_labels(["Dismiss", "Submit", ""]) == []


class TestParseSkillsKeyed:
    def test_returns_one_record_per_authoritative_name(self):
        skills = parse_skills(SKILLS_TEXT, NAMES)
        assert [s["name"] for s in skills] == NAMES

    def test_parses_capped_endorsement_count(self):
        stb = parse_skills(SKILLS_TEXT, NAMES)[0]
        assert stb["endorsements"] == 99
        assert stb["endorsements_display"] == "99+"
        assert len(stb["endorsers"]) == 2

    def test_parses_numeric_endorsement_count(self):
        sa = parse_skills(SKILLS_TEXT, NAMES)[1]
        assert sa["endorsements"] == 32
        assert sa["endorsements_display"] == "32"
        assert sa["endorsers"] == ["Endorsed by 2 colleagues at Acme"]

    def test_excludes_associated_experience_and_show_all_details(self):
        # "Director, ... at Acme" and "Show all 4 details" must not appear as skills
        names = [s["name"] for s in parse_skills(SKILLS_TEXT, NAMES)]
        assert "Director, Solutions Architecture at Acme" not in names
        assert not any(n.startswith("Show all") for n in names)

    def test_skill_without_metadata_has_zero_endorsements(self):
        by_name = {s["name"]: s for s in parse_skills(SKILLS_TEXT, NAMES)}
        assert by_name["Conditional Access"]["endorsements"] == 0
        assert by_name["Conditional Access"]["endorsers"] == []
        assert by_name["IPTV"]["endorsements"] == 0


class TestParseSkillsHeuristicFallback:
    def test_finds_real_skills_without_names(self):
        skills = parse_skills(SKILLS_TEXT, None)
        names = [s["name"] for s in skills]
        for expected in ["Set Top Box", "Software Architecture", "IPTV", "Conditional Access"]:
            assert expected in names

    def test_fallback_filters_tab_headers_and_show_all(self):
        names = [s["name"] for s in parse_skills(SKILLS_TEXT, None)]
        assert "All" not in names
        assert "Industry Knowledge" not in names
        assert not any(n.startswith("Show all") for n in names)

    def test_empty_text_returns_empty(self):
        assert parse_skills("", NAMES) == []
        assert parse_skills("", None) == []
