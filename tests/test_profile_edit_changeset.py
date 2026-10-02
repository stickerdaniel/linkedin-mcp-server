"""Change-set planning: validation, limits, diffs, fingerprints and states."""

from __future__ import annotations

import pytest

from linkedin_mcp_server.profile_edit.changeset import (
    ChangeSet,
    ChangeSetStatus,
    SkillsRequest,
    TextRequest,
    build_change_set,
    fingerprint,
    render_diff,
    skill_key,
    stale_fields,
)
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)

NOW = "2026-10-02T18:30:00+00:00"


def headline(
    after: str, current: str = "Senior Software Developer", limit: int | None = 220
) -> TextRequest:
    return TextRequest(
        "headline", "headline", "headline", "Headline", after, current, limit
    )


def about(
    after: str, current: str = "Old about.", limit: int | None = 2600
) -> TextRequest:
    return TextRequest("about", "about", "about", "About", after, current, limit)


def build(
    *texts: TextRequest,
    skills: SkillsRequest = SkillsRequest(),
    current_skills: tuple[str, ...] = (),
) -> ChangeSet:
    return build_change_set(texts, skills, current_skills, now=NOW)


class TestPlanning:
    def test_a_headline_change_records_before_after_and_baseline(self):
        cs = build(
            headline(
                "Senior Product Engineer | React, TypeScript, Node.js | AI Products"
            )
        )
        assert cs.status is ChangeSetStatus.PENDING_APPROVAL
        assert cs.id.startswith("cs_") and len(cs.id) == 19
        [c] = cs.changes
        assert (c.key, c.before, c.after) == (
            "headline",
            "Senior Software Developer",
            "Senior Product Engineer | React, TypeScript, Node.js | AI Products",
        )
        assert cs.baseline == {"headline": "Senior Software Developer"}

    def test_an_over_long_value_is_refused_with_lengths_and_never_truncated(self):
        with pytest.raises(ProfileEditError) as e:
            build(headline("x" * 230))
        assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
        [p] = e.value.details["problems"]
        assert p == {
            "field": "Headline",
            "reason": "too long",
            "proposedLength": 230,
            "allowedLength": 220,
            "overflow": 10,
        }

    def test_the_ui_limit_wins_over_the_default(self):
        with pytest.raises(ProfileEditError):
            build(headline("x" * 150, limit=120))
        build(about("y" * 2600))  # exactly at the default About limit is fine

    def test_the_default_limit_applies_when_the_form_reports_none(self):
        with pytest.raises(ProfileEditError) as e:
            build(about("y" * 2601, limit=None))
        assert e.value.details["problems"][0]["allowedLength"] == 2600

    def test_single_line_fields_refuse_newlines_and_control_characters_are_refused(
        self,
    ):
        with pytest.raises(ProfileEditError) as e:
            build(headline("Line one\nLine two"), about("Bell\x07"))
        reasons = {(p["field"], p["reason"]) for p in e.value.details["problems"]}
        assert reasons == {
            ("Headline", "must be a single line"),
            ("About", "contains control characters"),
        }

    def test_about_keeps_paragraphs(self):
        cs = build(about("First paragraph.\r\n\r\nSecond paragraph.  "))
        assert cs.changes[0].after == "First paragraph.\n\nSecond paragraph."

    def test_an_unchanged_value_is_skipped_with_a_warning_and_nothing_left_is_refused(
        self,
    ):
        with pytest.raises(ProfileEditError) as e:
            build(headline("  Senior Software Developer "))
        assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
        assert "identical" in e.value.details["warnings"][0]

    def test_an_empty_headline_is_refused_but_clearing_about_is_allowed_with_a_warning(
        self,
    ):
        with pytest.raises(ProfileEditError):
            build(headline(""))
        cs = build(about(""))
        assert cs.changes[0].after == "" and any("clears" in w for w in cs.warnings)

    def test_the_same_field_twice_is_refused(self):
        with pytest.raises(ProfileEditError) as e:
            build(headline("A"), headline("B"))
        assert e.value.details["problems"][0]["reason"] == "requested more than once"


class TestSkills:
    def test_skill_matching_is_case_insensitive_and_keeps_linkedins_display_value(self):
        cs = build(
            skills=SkillsRequest(add=["typescript", "Node.js"], remove=["react"]),
            current_skills=("React", "TypeScript"),
        )
        assert [(c.action, c.target) for c in cs.changes] == [
            ("add", "Node.js"),
            ("remove", "React"),
        ]
        assert any(
            "already on the profile" in w and "TypeScript" in w for w in cs.warnings
        )

    def test_duplicate_adds_collapse(self):
        cs = build(skills=SkillsRequest(add=["Node.js", "node.js", " NODE.JS "]))
        assert len(cs.changes) == 1

    def test_removing_a_skill_that_is_not_there_names_the_current_skills(self):
        with pytest.raises(ProfileEditError) as e:
            build(skills=SkillsRequest(remove=["COBOL"]), current_skills=("React",))
        assert e.value.code is ProfileEditErrorCode.SKILL_NOT_FOUND
        assert e.value.details["currentSkills"] == ["React"]

    def test_adding_and_removing_the_same_skill_is_refused(self):
        with pytest.raises(ProfileEditError):
            build(
                skills=SkillsRequest(add=["React"], remove=["react"]),
                current_skills=("React",),
            )

    def test_skill_key_ignores_case_width_and_spacing(self):
        # A full-width "N" (U+FF2E), written as an escape so the source stays plain ASCII.
        assert (
            skill_key("\uff2eode.js") == skill_key("node.js") == skill_key("  NODE.JS ")
        )


class TestFingerprintAndStale:
    def test_fingerprint_is_stable_and_sensitive(self):
        a, _ = fingerprint({"headline": "A", "about": "B"})
        b, _ = fingerprint({"about": "B", "headline": "A"})
        c, _ = fingerprint({"headline": "A ", "about": "B"})
        assert a == b and a != c

    def test_a_manual_edit_after_proposal_is_reported_by_field(self):
        cs = build(headline("New"), about("New about"))
        assert (
            stale_fields(
                cs, {"headline": "Senior Software Developer", "about": "Old about."}
            )
            == []
        )
        [s] = stale_fields(cs, {"headline": "Edited by hand", "about": "Old about."})
        assert s == {
            "field": "headline",
            "expected": "Senior Software Developer",
            "actual": "Edited by hand",
        }

    def test_a_field_that_can_no_longer_be_read_is_stale(self):
        cs = build(headline("New"))
        assert stale_fields(cs, {})[0]["reason"] == "no longer readable"


class TestStatesAndRecords:
    def test_only_forward_transitions_are_allowed(self):
        cs = build(headline("New"))
        cs.transition(ChangeSetStatus.APPLYING, NOW)
        cs.transition(ChangeSetStatus.APPLIED, NOW)
        for to in ChangeSetStatus:
            with pytest.raises(ProfileEditError) as e:
                cs.transition(to, NOW)
            assert e.value.code is ProfileEditErrorCode.CHANGE_SET_NOT_PENDING

    def test_a_discarded_change_set_cannot_start_applying(self):
        cs = build(headline("New"))
        cs.transition(ChangeSetStatus.DISCARDED, NOW)
        with pytest.raises(ProfileEditError):
            cs.transition(ChangeSetStatus.APPLYING, NOW)

    def test_a_change_set_survives_a_round_trip(self):
        cs = build(headline("New"), skills=SkillsRequest(add=["Python"]))
        again = ChangeSet.from_dict(cs.as_dict())
        assert again.as_dict() == cs.as_dict()

    def test_the_diff_shows_sections_before_after_and_skill_signs(self):
        cs = build(
            headline("Senior Product Engineer"),
            skills=SkillsRequest(add=["TypeScript"], remove=["jQuery"]),
            current_skills=("jQuery",),
        )
        assert render_diff(cs) == (
            "HEADLINE\nbefore: Senior Software Developer\nafter:  Senior Product Engineer\n\n"
            "SKILLS\n+ TypeScript\n- jQuery"
        )
