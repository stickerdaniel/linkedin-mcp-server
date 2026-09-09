"""Tests for the routing and reference policy of the job list workflows."""

from linkedin_mcp_server.scraping.job_policy import (
    reconcile_search_references,
    route,
    same_job_search,
)
from linkedin_mcp_server.scraping.link_metadata import (
    Reference,
    _SEARCH_RESULTS_REFERENCE_CAP,
)


def job(job_id: str, text: str | None = None) -> Reference:
    reference: Reference = {"kind": "job", "url": f"/jobs/view/{job_id}/"}
    if text is not None:
        reference["text"] = text
    return reference


def company(name: str) -> Reference:
    return {"kind": "company", "url": f"/company/{name}/"}


class TestReconcileSearchReferences:
    def test_the_rail_decides_which_jobs_the_page_has(self):
        references = reconcile_search_references(
            [job("100", "Kept"), job("999", "Detail pane")], ["100"]
        )

        assert references == [job("100", "Kept")]

    def test_an_id_without_an_anchor_still_becomes_a_reference(self):
        references = reconcile_search_references([job("100", "Kept")], ["100", "200"])

        assert references == [job("100", "Kept"), job("200")]

    def test_a_job_named_twice_is_emitted_once(self):
        references = reconcile_search_references(
            [job("100", "Anchor"), job("100", "Logo")], ["100"]
        )

        assert references == [job("100", "Anchor")]

    def test_ancillary_references_share_what_the_rail_leaves(self):
        ids = [str(index) for index in range(10)]
        left = _SEARCH_RESULTS_REFERENCE_CAP - len(ids)
        references = reconcile_search_references(
            [company(f"c{index}") for index in range(left + 3)], ids
        )

        ancillary = [ref for ref in references if ref["kind"] != "job"]
        assert len(ancillary) == left

    def test_a_rail_past_the_cap_leaves_no_ancillary_allowance(self):
        # Without the floor the remaining allowance goes negative, which is
        # truthy, so an overfull rail would admit every sidebar link instead
        # of none.
        ids = [str(index) for index in range(_SEARCH_RESULTS_REFERENCE_CAP + 1)]
        references = reconcile_search_references([company("acme")], ids)

        assert all(ref["kind"] == "job" for ref in references)
        assert len(references) == len(ids)


class TestRoute:
    def test_a_route_is_the_host_and_the_path(self):
        assert route("https://www.linkedin.com/jobs/search/?keywords=python") == (
            "www.linkedin.com",
            "/jobs/search",
        )

    def test_the_query_linkedin_appends_is_not_part_of_it(self):
        assert route("https://www.linkedin.com/jobs/search?currentJobId=1") == route(
            "https://www.linkedin.com/jobs/search/"
        )


class TestSameJobSearch:
    def test_the_redesign_redirect_is_the_same_search(self):
        assert same_job_search(
            ("www.linkedin.com", "/jobs/search"),
            ("www.linkedin.com", "/jobs/search-results"),
        )

    def test_a_third_route_is_not(self):
        assert not same_job_search(
            ("www.linkedin.com", "/jobs/search"),
            ("www.linkedin.com", "/checkpoint/challenge"),
        )

    def test_the_same_path_on_another_host_is_not(self):
        assert not same_job_search(
            ("www.linkedin.com", "/jobs/search"),
            ("evil.example", "/jobs/search-results"),
        )
