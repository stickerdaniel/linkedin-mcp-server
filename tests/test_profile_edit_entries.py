"""Names, new entries and rehearsals, end to end against an in-memory LinkedIn."""

from __future__ import annotations

from pathlib import Path

import pytest

from linkedin_mcp_server.profile_edit.changeset import ChangeSetStatus, EntryRequest
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)
from linkedin_mcp_server.profile_edit.service import ProfileEditService, Proposal
from linkedin_mcp_server.profile_edit.store import ProfileEditStore
from profile_edit_fakes import FakeEditor, similar_positions

ROLE = {
    "title": "Lecturer",
    "company": "Example University",
    "employmentType": "Full-time",
    "startMonth": 9,
    "startYear": 2022,
}
SCHOOL = {
    "school": "Example University",
    "degree": "Master of Science - MS",
    "fieldOfStudy": "Cybersecurity",
    "startYear": 2022,
    "endYear": 2024,
}
PAPER = {
    "title": "Personalized federated learning for patient records",
    "publisher": "Example Conference 2026",
    "date": "05/14/2026",
    "url": "https://doi.org/10.0000/example.2026.1",
}


@pytest.fixture
def store(tmp_path: Path) -> ProfileEditStore:
    return ProfileEditStore(tmp_path)


def service(editor: FakeEditor, store: ProfileEditStore, *, writes: bool = True):
    return ProfileEditService(
        editor,
        store,
        writes_enabled=lambda: writes,
        pacing_seconds=3.0,
        clock=lambda: "2026-10-12T10:00:00+00:00",
    )


def entries(*items: tuple[str, dict]) -> Proposal:
    return Proposal(new_entries=[EntryRequest(k, v) for k, v in items])


async def problems_of(awaitable) -> list[dict]:
    with pytest.raises(ProfileEditError) as e:
        await awaitable
    assert e.value.code is ProfileEditErrorCode.VALIDATION_ERROR
    return e.value.details["problems"]


class TestNames:
    async def test_a_name_change_is_applied_and_read_back(self, store):
        ed = FakeEditor(first_name="Old", last_name="Name")
        s = service(ed, store)
        cs = await s.propose(Proposal(first_name="Jane", last_name="Doe"))
        assert [c["before"] for c in cs["changes"]] == ["Old", "Name"]
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"
        assert (ed.first_name, ed.last_name) == ("Jane", "Doe")
        assert [r["status"] for r in out["results"]] == ["UPDATED", "UPDATED"]

    async def test_an_empty_name_is_refused(self, store):
        problems = await problems_of(
            service(FakeEditor(), store).propose(Proposal(first_name="  "))
        )
        assert problems == [{"field": "First name", "reason": "cannot be empty"}]

    async def test_a_name_changed_by_hand_makes_the_change_set_stale(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(last_name="Smith"))
        ed.last_name = "Changed"
        with pytest.raises(ProfileEditError) as e:
            await s.apply(cs["changeSetId"], confirm=True)
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
        assert ed.writes == []

    async def test_a_name_linkedin_stores_differently_fails_verification(self, store):
        ed = FakeEditor(mangle={"last_name": "Smyth"})
        s = service(ed, store)
        cs = await s.propose(Proposal(last_name="Smith"))
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "FAILED"
        assert out["results"][0]["error"] == "VERIFICATION_FAILED"


class TestNewEntries:
    async def test_entries_are_created_and_each_is_verified(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(
            entries(("position", ROLE), ("education", SCHOOL), ("publication", PAPER))
        )
        assert cs["affectedSections"] == ["position", "education", "publication"]
        assert "+ New position: Lecturer · Example University" in cs["diff"]
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"
        assert [r["status"] for r in out["results"]] == ["CREATED"] * 3
        assert all(r["entryId"] for r in out["results"])
        assert ed.writes == [
            "position+Lecturer",
            "education+Example University",
            "publication+Personalized federated learning for patient records",
        ]

    async def test_two_entries_of_one_kind_are_each_verified(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        second = {**PAPER, "title": "A review of deep packet inspection", "url": None}
        cs = await s.propose(entries(("publication", PAPER), ("publication", second)))
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"
        assert len(ed.entries["publication"]) == 2

    async def test_a_save_that_creates_nothing_is_not_reported_done(self, store):
        ed = FakeEditor(drop_creates={"education"})
        s = service(ed, store)
        cs = await s.propose(entries(("education", SCHOOL), ("publication", PAPER)))
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "FAILED"
        assert out["results"][0]["error"] == "VERIFICATION_FAILED"
        assert out["results"][1]["status"] == "NOT_ATTEMPTED"
        assert "publication" not in ed.entries

    async def test_a_lookalike_of_an_existing_entry_is_refused(self, store):
        ed = FakeEditor(positions=similar_positions())
        s = service(ed, store)
        problems = await problems_of(
            s.propose(
                entries(
                    (
                        "position",
                        {
                            "title": "Lead Developer",
                            "company": "Liftango",
                            "startYear": 2019,
                        },
                    )
                )
            )
        )
        assert problems[0]["reason"].startswith("looks like an existing entry")
        assert problems[0]["existing"]["id"] == "103"

    async def test_allow_duplicate_adds_it_with_a_warning(self, store):
        ed = FakeEditor(positions=similar_positions())
        s = service(ed, store)
        req = EntryRequest(
            "position",
            {"title": "Lead Developer", "company": "Liftango", "startYear": 2022},
            allow_duplicate=True,
        )
        cs = await s.propose(Proposal(new_entries=[req]))
        assert any("resembles an existing entry" in w for w in cs["warnings"])

    async def test_an_entry_added_by_hand_meanwhile_makes_the_change_set_stale(
        self, store
    ):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(entries(("education", SCHOOL)))
        ed.entries["education"] = [("999", {"school": "Other"})]
        with pytest.raises(ProfileEditError) as e:
            await s.apply(cs["changeSetId"], confirm=True)
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET

    @pytest.mark.parametrize(
        ("kind", "values", "reason"),
        [
            ("position", {"title": "X"}, "is required"),
            ("position", {**ROLE, "endYear": 2021}, "is before the start"),
            (
                "position",
                {**ROLE, "current": True, "endYear": 2024},
                "a current position has no end date",
            ),
            ("position", {**ROLE, "startMonth": 13}, "must be 1-12"),
            ("position", {**ROLE, "title": "a\nb"}, "must be a single line"),
            ("position", {**ROLE, "salary": "1"}, "not a position field"),
            ("education", {"degree": "MSc"}, "is required"),
            ("education", {**SCHOOL, "grade": "x" * 81}, "too long"),
            ("education", {**SCHOOL, "endMonth": 6, "endYear": None}, "needs endYear"),
            ("publication", {**PAPER, "date": "2026-05-14"}, "must be mm/dd/yyyy"),
            ("publication", {**PAPER, "url": "doi.org/10.1"}, "must be an http(s) URL"),
        ],
    )
    async def test_invalid_entries_are_refused_with_a_reason(
        self, store, kind, values, reason
    ):
        problems = await problems_of(
            service(FakeEditor(), store).propose(entries((kind, values)))
        )
        assert any(p["reason"] == reason for p in problems), problems

    async def test_a_position_without_an_end_is_current(self, store):
        cs = await service(FakeEditor(), store).propose(entries(("position", ROLE)))
        assert '"current": true' in cs["changes"][0]["after"]


class TestRehearse:
    async def test_a_rehearsal_fills_every_form_and_saves_nothing(self, store):
        ed = FakeEditor(first_name="Old")
        s = service(ed, store, writes=False)
        cs = await s.propose(
            Proposal(
                first_name="Jane",
                headline="Lecturer",
                new_entries=[EntryRequest("education", SCHOOL)],
                skills_add=["Python"],
            )
        )
        out = await s.rehearse(cs["changeSetId"])
        assert out["allFilled"] is True
        assert [r["status"] for r in out["results"]] == [
            "FILLED_NOT_SAVED",
            "FILLED_NOT_SAVED",
            "FILLED_NOT_SAVED",
            "NOT_REHEARSED",
        ]
        assert ed.writes == [] and ed.entries == {} and ed.first_name == "Old"
        assert ed.rehearsals == [
            "headline",
            "name/first",
            "education:Example University",
        ]
        # Still pending: a rehearsal is not an apply.
        assert (await s.preview(cs["changeSetId"]))["status"] == str(
            ChangeSetStatus.PENDING_APPROVAL
        )

    async def test_a_rehearsal_reports_a_form_that_would_not_take_a_value(self, store):
        ed = FakeEditor(
            fail={
                "create_publication": ProfileEditError(
                    ProfileEditErrorCode.LINKEDIN_SAVE_FAILED
                )
            }
        )
        s = service(ed, store)
        cs = await s.propose(entries(("publication", PAPER)))
        out = await s.rehearse(cs["changeSetId"])
        assert out["allFilled"] is False
        assert out["results"][0]["error"] == "LINKEDIN_SAVE_FAILED"

    async def test_only_a_pending_change_set_is_rehearsed(self, store):
        ed = FakeEditor()
        s = service(ed, store)
        cs = await s.propose(Proposal(last_name="Smith"))
        s.discard(cs["changeSetId"])
        with pytest.raises(ProfileEditError) as e:
            await s.rehearse(cs["changeSetId"])
        assert e.value.code is ProfileEditErrorCode.CHANGE_SET_NOT_PENDING


class TestSavedRichText:
    APPROVED = "Before:\n• one\n• two\n\nAfter."

    def test_a_line_break_read_back_as_a_paragraph_break_is_the_same_text(self):
        from linkedin_mcp_server.profile_edit.model import same_saved_text

        observed = "Before:\n\n• one\n\n• two\n\nAfter."
        assert same_saved_text(observed, self.APPROVED)

    def test_different_words_are_still_a_mismatch(self):
        from linkedin_mcp_server.profile_edit.model import same_saved_text

        assert not same_saved_text(
            "Before:\n\n• one\n\n• three\n\nAfter.", self.APPROVED
        )
        assert not same_saved_text("Before: • one • two After.", self.APPROVED)

    async def test_about_saved_with_paragraph_breaks_is_verified(self, store):
        ed = FakeEditor(mangle={"about": "Before:\n\n• one\n\n• two\n\nAfter."})
        s = service(ed, store)
        cs = await s.propose(Proposal(about=self.APPROVED))
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"


class TestOrganizations:
    OFFICIAL = "Example University, Exampleland"

    def editor(self) -> FakeEditor:
        return FakeEditor(
            organizations={
                ("position", "1"): "Example University",
                ("education", "2"): "Example University",
            }
        )

    async def test_entries_are_linked_to_the_official_page(self, store):
        from linkedin_mcp_server.profile_edit.service import OrganizationEdit

        ed = self.editor()
        s = service(ed, store)
        cs = await s.propose(
            Proposal(
                organizations=[
                    OrganizationEdit("position", "1", self.OFFICIAL),
                    OrganizationEdit("education", "2", self.OFFICIAL),
                ]
            )
        )
        assert [c["before"] for c in cs["changes"]] == ["Example University"] * 2
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "APPLIED"
        assert set(ed.organizations.values()) == {self.OFFICIAL}

    async def test_a_name_linkedin_does_not_suggest_is_not_saved(self, store):
        from linkedin_mcp_server.profile_edit.service import OrganizationEdit

        ed = self.editor()
        s = service(ed, store)
        cs = await s.propose(
            Proposal(organizations=[OrganizationEdit("position", "1", "Example Uni")])
        )
        out = await s.apply(cs["changeSetId"], confirm=True)
        assert out["status"] == "FAILED"
        assert ed.organizations[("position", "1")] == "Example University"

    async def test_an_organization_changed_meanwhile_is_stale(self, store):
        from linkedin_mcp_server.profile_edit.service import OrganizationEdit

        ed = self.editor()
        s = service(ed, store)
        cs = await s.propose(
            Proposal(organizations=[OrganizationEdit("education", "2", self.OFFICIAL)])
        )
        ed.organizations[("education", "2")] = "Something else"
        with pytest.raises(ProfileEditError) as e:
            await s.apply(cs["changeSetId"], confirm=True)
        assert e.value.code is ProfileEditErrorCode.STALE_CHANGE_SET
        assert ed.writes == []
