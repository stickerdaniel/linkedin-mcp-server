"""READ -> PROPOSE -> PREVIEW -> APPLY -> VERIFY against an in-memory LinkedIn."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.profile_edit.changeset import ChangeSetStatus
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.service import (
    ExperienceEdit,
    ProfileEditService,
    Proposal,
)
from linkedin_mcp_server.profile_edit.store import AUDIT_KEYS, ProfileEditStore
from profile_edit_fakes import FakeEditor, FakePosition, similar_positions

NEW_HEADLINE = "Senior Product Engineer | React, TypeScript, Node.js | AI Products"
NEW_ABOUT = (
    "I build product software end to end.\n\nReact, TypeScript, Node and applied AI."
)


@pytest.fixture
def store(tmp_path: Path) -> ProfileEditStore:
    return ProfileEditStore(tmp_path)


def service(
    editor: FakeEditor | None, store: ProfileEditStore, *, writes: bool = True
) -> ProfileEditService:
    return ProfileEditService(
        editor,
        store,
        writes_enabled=lambda: writes,
        pacing_seconds=3.0,
        clock=lambda: "2026-10-02T18:30:00+00:00",
    )


async def code_of(awaitable) -> ProfileEditErrorCode:
    with pytest.raises(ProfileEditError) as e:
        await awaitable
    return e.value.code


class TestRead:
    async def test_the_profile_read_is_structured(self, store):
        ed = FakeEditor(positions=similar_positions(), skills=["React", "Python"])
        p = await service(ed, store).get_profile()
        assert p["headline"] == "Senior Software Developer"
        assert p["location"] == "Edinburgh"
        assert [e["id"] for e in p["experiences"]] == ["101", "102", "103"]
        assert p["skills"] == [
            {"name": "React", "position": 1},
            {"name": "Python", "position": 2},
        ]
        assert p["limits"] == {"headline": 220, "about": 2600}

    async def test_a_profile_without_about_or_skills_reads_cleanly(self, store):
        p = await service(FakeEditor(about="", skills=[]), store).get_profile()
        assert p["about"] == "" and p["skills"] == []

    async def test_one_experience_returns_its_full_form_values(self, store):
        ed = FakeEditor(positions=similar_positions())
        e = (await service(ed, store).get_experiences("103"))["experience"]
        assert (e["title"], e["description"], e["limits"]) == (
            "Lead Developer",
            "Routing platform.",
            {"title": 100, "description": 2000},
        )


class TestPropose:
    async def test_proposing_changes_nothing_on_linkedin_and_stores_the_change_set(
        self, store
    ):
        ed = FakeEditor()
        out = await service(ed, store).propose(
            Proposal(headline=NEW_HEADLINE, about=NEW_ABOUT)
        )
        assert out["status"] == "PENDING_APPROVAL" and ed.writes == []
        assert ed.headline == "Senior Software Developer"
        assert {c["field"] for c in out["changes"]} == {"headline", "about"}
        assert "Nothing has been changed on LinkedIn" in out["note"]
        assert store.load(out["changeSetId"]).baseline == {
            "headline": "Senior Software Developer",
            "about": "I build web applications.",
        }

    async def test_an_experience_is_never_chosen_by_position_or_guessed(self, store):
        ed = FakeEditor(positions=similar_positions())
        s = service(ed, store)
        assert (
            await code_of(
                s.propose(
                    Proposal(
                        experiences=[
                            ExperienceEdit(
                                company="IPG Automotive",
                                match_title="Senior Software Developer",
                                description="x",
                            )
                        ]
                    )
                )
            )
            is ProfileEditErrorCode.AMBIGUOUS_EXPERIENCE
        )
        with pytest.raises(ProfileEditError) as e:
            await s.propose(
                Proposal(
                    experiences=[
                        ExperienceEdit(company="IPG Automotive", description="x")
                    ]
                )
            )
        assert {c["id"] for c in e.value.details["candidates"]} == {"101", "102"}
        assert (
            await code_of(
                s.propose(
                    Proposal(
                        experiences=[
                            ExperienceEdit(experience_id="999", description="x")
                        ]
                    )
                )
            )
            is ProfileEditErrorCode.EXPERIENCE_NOT_FOUND
        )

    async def test_a_unique_match_or_an_id_targets_exactly_one_position(self, store):
        ed = FakeEditor(positions=similar_positions())
        s = service(ed, store)
        by_date = await s.propose(
            Proposal(
                experiences=[
                    ExperienceEdit(
                        company="ipg automotive",
                        start_date="Mar 2021",
                        description="Kafka pipelines.",
                    )
                ]
            )
        )
        assert by_date["changes"][0]["field"] == "experience/102/description"
        by_id = await s.propose(
            Proposal(
                experiences=[
                    ExperienceEdit(experience_id="101", title="Principal Engineer")
                ]
            )
        )
        assert by_id["changes"][0]["field"] == "experience/101/title"

    async def test_a_very_long_description_is_refused_with_its_overflow(self, store):
        ed = FakeEditor(positions=similar_positions())
        with pytest.raises(ProfileEditError) as e:
            await service(ed, store).propose(
                Proposal(
                    experiences=[
                        ExperienceEdit(experience_id="103", description="w" * 2050)
                    ]
                )
            )
        assert e.value.details["problems"][0]["overflow"] == 50

    async def test_an_empty_request_is_refused(self, store):
        assert (
            await code_of(service(FakeEditor(), store).propose(Proposal()))
            is ProfileEditErrorCode.VALIDATION_ERROR
        )


class TestPreviewAndStale:
    async def test_preview_reports_values_and_that_nothing_changed(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        out = await s.preview(cs["changeSetId"])
        assert out["applicable"] and out["profileUnchangedSinceProposal"]
        assert out["changes"][0]["before"] == "Senior Software Developer"

    async def test_a_manual_edit_between_proposal_and_preview_makes_it_stale_for_good(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        ed.headline = "Edited by hand on linkedin.com"
        with pytest.raises(ProfileEditError) as e:
            await s.preview(cs["changeSetId"])
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
        assert (
            e.value.details["changedFields"][0]["actual"]
            == "Edited by hand on linkedin.com"
        )
        assert (
            await code_of(s.apply(cs["changeSetId"], confirm=True))
            is ProfileEditErrorCode.CHANGE_SET_NOT_PENDING
        )
        assert ed.writes == []


class TestApplyGates:
    async def test_apply_without_confirmation_refuses_and_writes_nothing(self, store):
        ed = FakeEditor()
        cs = await service(ed, store).propose(Proposal(headline=NEW_HEADLINE))
        assert (
            await code_of(service(ed, store).apply(cs["changeSetId"], confirm=False))
            is ProfileEditErrorCode.CONFIRMATION_REQUIRED
        )
        assert (
            ed.writes == []
            and store.load(cs["changeSetId"]).status == "PENDING_APPROVAL"
        )

    async def test_apply_with_writes_disabled_refuses_safely(self, store):
        ed = FakeEditor()
        cs = await service(ed, store).propose(Proposal(headline=NEW_HEADLINE))
        assert (
            await code_of(
                service(ed, store, writes=False).apply(cs["changeSetId"], confirm=True)
            )
            is ProfileEditErrorCode.WRITES_DISABLED
        )
        assert ed.writes == []

    async def test_the_prechecks_need_no_browser(self, store):
        cs = await service(FakeEditor(), store).propose(Proposal(headline=NEW_HEADLINE))
        with pytest.raises(ProfileEditError) as e:
            service(None, store).precheck_apply(cs["changeSetId"], confirm=False)
        assert e.value.code is ProfileEditErrorCode.CONFIRMATION_REQUIRED

    async def test_an_unknown_change_set_is_reported(self, store):
        assert (
            await code_of(
                service(FakeEditor(), store).apply("cs_0000000000000000", confirm=True)
            )
            is ProfileEditErrorCode.CHANGE_SET_NOT_FOUND
        )
        assert (
            await code_of(
                service(FakeEditor(), store).apply("../../etc/passwd", confirm=True)
            )
            is ProfileEditErrorCode.CHANGE_SET_NOT_FOUND
        )

    async def test_a_discarded_change_set_cannot_be_applied(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        assert s.discard(cs["changeSetId"])["status"] == "DISCARDED"
        assert (
            await code_of(s.apply(cs["changeSetId"], confirm=True))
            is ProfileEditErrorCode.CHANGE_SET_NOT_PENDING
        )
        assert ed.writes == []


class TestApply:
    async def test_the_acceptance_sequence(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE, about=NEW_ABOUT))
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"
        assert out["results"] == [
            {"field": "headline", "status": "UPDATED", "verified": True},
            {"field": "about", "status": "UPDATED", "verified": True},
        ]
        assert (ed.headline, ed.about) == (NEW_HEADLINE, NEW_ABOUT)
        assert ed.pauses == [3.0], "writes are paced"
        # The same change set cannot be applied twice.
        assert (
            await code_of(s.apply(cs["changeSetId"], confirm=True))
            is ProfileEditErrorCode.CHANGE_SET_NOT_PENDING
        )

    async def test_a_snapshot_of_the_old_values_is_written_before_any_change(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        out = await s.apply(cs["changeSetId"], confirm=True)
        snap = json.loads(Path(out["snapshotPath"]).read_text(encoding="utf-8"))
        assert snap["values"] == {"headline": "Senior Software Developer"}
        assert set(snap) == {"takenAt", "changeSetId", "values"}

    async def test_a_manual_edit_between_proposal_and_apply_is_never_overwritten(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        ed.headline = "My own manual edit"
        assert (
            await code_of(s.apply(cs["changeSetId"], confirm=True))
            is ProfileEditErrorCode.STALE_CHANGE_SET
        )
        assert ed.headline == "My own manual edit" and ed.writes == []
        assert store.load(cs["changeSetId"]).status == "STALE"

    async def test_experiences_and_skills_apply_and_verify(self, store):
        ed = FakeEditor(positions=similar_positions(), skills=["jQuery"])
        s = service(ed, store)
        cs = await s.propose(
            Proposal(
                experiences=[
                    ExperienceEdit(
                        experience_id="103",
                        title="Lead Engineer",
                        description="Led the routing platform.",
                    )
                ],
                skills_add=["reactjs", "TypeScript"],
                skills_remove=["jquery"],
            )
        )
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"
        assert [r["status"] for r in out["results"]] == [
            "UPDATED",
            "UPDATED",
            "ADDED",
            "ADDED",
            "REMOVED",
        ]
        assert out["results"][2]["value"] == "ReactJS", (
            "LinkedIn's canonical name is kept"
        )
        assert ed.skills == ["ReactJS", "TypeScript"]
        assert (ed.positions[2].title, ed.positions[0].title) == (
            "Lead Engineer",
            "Senior Software Developer",
        )

    async def test_a_save_error_after_one_success_is_a_partial_failure_and_stops(
        self, store
    ):
        ed = FakeEditor(skills=["React"])
        s = service(ed, store)
        cs = await s.propose(
            Proposal(headline=NEW_HEADLINE, about=NEW_ABOUT, skills_add=["Python"])
        )
        ed.fail["write_about"] = ProfileEditError(
            ProfileEditErrorCode.LINKEDIN_SAVE_FAILED,
            formErrors=["Something went wrong"],
        )
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "PARTIAL_FAILURE" and out["error"] == "PARTIAL_FAILURE"
        assert [(r["field"], r["status"]) for r in out["results"]] == [
            ("headline", "UPDATED"),
            ("about", "FAILED"),
            ("skills/add/python", "NOT_ATTEMPTED"),
        ]
        assert out["results"][1]["error"] == "LINKEDIN_SAVE_FAILED"
        assert "recovery" in out and ed.skills == ["React"]

    async def test_a_missing_selector_fails_without_claiming_success(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        ed.fail["write_headline"] = ProfileEditError(
            ProfileEditErrorCode.SELECTOR_NOT_FOUND, control="headline"
        )
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert (
            out["status"] == "FAILED"
            and out["results"][0]["error"] == "SELECTOR_NOT_FOUND"
        )
        assert out["results"][0]["verified"] is False

    async def test_a_save_linkedin_stores_differently_is_a_verification_failure(
        self, store
    ):
        ed = FakeEditor(mangle={"headline": "Senior Product Engineer | React"})
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "FAILED"
        assert out["results"][0]["error"] == "VERIFICATION_FAILED"
        assert (
            out["results"][0]["details"]["observed"]
            == "Senior Product Engineer | React"
        )

    async def test_a_security_challenge_mid_apply_stops_with_authentication_required(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE, about=NEW_ABOUT))
        ed.fail["write_about"] = RateLimitError(
            "LinkedIn security checkpoint detected."
        )
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "PARTIAL_FAILURE"
        assert out["results"][1]["error"] == "AUTHENTICATION_REQUIRED"

    async def test_an_unknown_exception_is_recorded_not_retried(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        ed.fail["write_headline"] = TimeoutError("navigation timed out")
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "FAILED" and ed.writes == []
        assert store.load(cs["changeSetId"]).status == "FAILED"


class TestAudit:
    async def test_the_audit_log_records_each_field_and_nothing_sensitive(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        await s.apply(cs["changeSetId"], confirm=True)
        events = [
            json.loads(line)
            for line in store.audit_log.read_text(encoding="utf-8").splitlines()
        ]
        assert all(set(e) <= AUDIT_KEYS for e in events)
        field_events = [e for e in events if e.get("field") == "headline"]
        assert field_events == [
            {
                "at": "2026-10-02T18:30:00+00:00",
                "tool": "apply_profile_changes",
                "changeSetId": cs["changeSetId"],
                "section": "headline",
                "field": "headline",
                "result": "UPDATED",
                "verified": True,
            }
        ]
        text = store.audit_log.read_text(encoding="utf-8").lower()
        for secret in ("cookie", "li_at", "password", "authorization", "token"):
            assert secret not in text

    def test_the_audit_log_refuses_keys_outside_its_allow_list(self, store):
        with pytest.raises(ValueError):
            store.audit(at="now", cookie="li_at=secret")


class TestReviewFindings:
    """Regression tests for the findings in the first review of this feature."""

    async def test_a_change_set_applies_only_to_the_account_that_proposed_it(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        ed.account_url = "https://www.linkedin.com/in/someone-else/"
        with pytest.raises(ProfileEditError) as e:
            await s.preview(cs["changeSetId"])
        assert e.value.code is ProfileEditErrorCode.ACCOUNT_MISMATCH
        with pytest.raises(ProfileEditError) as e:
            await s.apply(cs["changeSetId"], confirm=True)
        assert e.value.code is ProfileEditErrorCode.ACCOUNT_MISMATCH
        assert e.value.details["proposedFor"] == "https://www.linkedin.com/in/jane/"
        assert ed.writes == []
        assert store.load(cs["changeSetId"]).status == "PENDING_APPROVAL", (
            "the proposer can still apply it after signing back in"
        )

    async def test_a_change_set_with_no_recorded_account_is_refused(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        record = store.load(cs["changeSetId"])
        record.account = None
        store.save(record)
        assert (
            await code_of(s.apply(cs["changeSetId"], confirm=True))
            is ProfileEditErrorCode.ACCOUNT_MISMATCH
        )

    async def test_an_interrupted_apply_is_closed_with_what_is_already_live(
        self, store
    ):
        ed = FakeEditor(skills=["React"])
        s = service(ed, store)
        cs = await s.propose(
            Proposal(headline=NEW_HEADLINE, about=NEW_ABOUT, skills_add=["Python"])
        )
        ed.fail["write_about"] = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await s.apply(cs["changeSetId"], confirm=True)
        record = store.load(cs["changeSetId"])
        assert record.status == "PARTIAL_FAILURE"
        assert [(r["field"], r["status"]) for r in record.results] == [
            ("headline", "UPDATED"),
            ("about", "OUTCOME_UNKNOWN"),
            ("skills/add/python", "NOT_ATTEMPTED"),
        ]
        assert record.results[0]["verified"] is True
        assert (
            await code_of(s.apply(cs["changeSetId"], confirm=True))
            is ProfileEditErrorCode.CHANGE_SET_NOT_PENDING
        )

    async def test_progress_is_saved_after_every_field(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE, about=NEW_ABOUT))
        seen: list[list[str]] = []
        original_save = store.save

        def spy(record):
            seen.append([r["status"] for r in record.results])
            original_save(record)

        store.save = spy  # type: ignore[method-assign]
        await s.apply(cs["changeSetId"], confirm=True)
        assert ["UPDATED"] in seen, (
            "the first field is on disk before the second is attempted"
        )

    async def test_a_record_left_applying_by_a_dead_process_can_be_discarded(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(headline=NEW_HEADLINE))
        record = store.load(cs["changeSetId"])
        record.transition(ChangeSetStatus.APPLYING, "2026-10-03T00:00:00+00:00")
        record.results = [{"field": "headline", "status": "UPDATED", "verified": True}]
        store.save(record)
        out = s.discard(cs["changeSetId"])
        assert out["status"] == "DISCARDED"
        assert out["results"] == [
            {"field": "headline", "status": "UPDATED", "verified": True}
        ]

    async def test_a_start_date_never_matches_an_end_date(self, store):
        ed = FakeEditor(
            positions=[
                FakePosition(
                    "201", "Developer", "Acme", "May 2022 - Jul 2023 · 1 yr 3 mos"
                ),
                FakePosition(
                    "202", "Developer", "Acme", "Jan 2021 - Apr 2022 · 1 yr 4 mos"
                ),
                FakePosition("203", "Developer", "Acme", "2019 \u2013 2021"),
            ]
        )
        s = service(ed, store)
        out = await s.propose(
            Proposal(
                experiences=[
                    ExperienceEdit(company="Acme", start_date="2022", description="x")
                ]
            )
        )
        assert out["changes"][0]["field"] == "experience/201/description"
        out = await s.propose(
            Proposal(
                experiences=[
                    ExperienceEdit(company="Acme", start_date="2019", description="y")
                ]
            )
        )
        assert out["changes"][0]["field"] == "experience/203/description", (
            "en-dash ranges too"
        )
