"""Guest-endpoint job fetching: parsing, concurrency, limits and failures.

HTTP goes through ``httpx2.MockTransport``; nothing here touches LinkedIn.
"""

import asyncio

import httpx2
import pytest

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.linkedin.guest_jobs import (
    MAX_WORKERS,
    fetch_jobs,
    parse_guest_posting,
)

# Trimmed from a real guest posting page; same classes and nesting.
POSTING_HTML = """
<h2 class="top-card-layout__title font-sans topcard__title">Senior Python Developer</h2>
<div class="topcard__flavor-row">
  <span class="topcard__flavor">
    <a class="topcard__org-name-link topcard__flavor--black-link"
       href="https://eg.linkedin.com/company/acme?trk=x">
      Acme &amp; Co
    </a>
  </span>
  <span class="topcard__flavor topcard__flavor--bullet">
    Giza, Egypt
  </span>
</div>
<div class="topcard__flavor-row">
  <span class="posted-time-ago__text topcard__flavor--metadata"> 2 days ago </span>
  <figure><figcaption class="num-applicants__caption"> Over 100 applicants </figcaption></figure>
</div>
<div class="description__text description__text--rich">
  <section class="show-more-less-html" data-max-lines="5">
    <div class="show-more-less-html__markup show-more-less-html__markup--clamp
        relative overflow-hidden">
      <p>We build things.</p>
      <p><strong>Requirements:</strong><br>Python 3.12</p>
      <ul><li>Django</li><li>PostgreSQL</li></ul>
    </div>
  </section>
  <ul class="description__job-criteria-list">
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader"> Seniority level </h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">
        Mid-Senior level
      </span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader"> Employment type </h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">
        Full-time
      </span>
    </li>
  </ul>
</div>
"""

NO_DESCRIPTION_HTML = '<h2 class="top-card-layout__title">Closed job</h2>'


def _transport(handler):
    return httpx2.MockTransport(handler)


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, text=POSTING_HTML)


class TestParsing:
    def test_extracts_every_field(self):
        posting = parse_guest_posting(POSTING_HTML)

        assert posting is not None
        assert posting["title"] == "Senior Python Developer"
        assert posting["company"] == "Acme & Co"
        assert posting["company_url"] == "https://eg.linkedin.com/company/acme"
        assert posting["location"] == "Giza, Egypt"
        assert posting["posted"] == "2 days ago"
        assert posting["applicants"] == "Over 100 applicants"
        assert posting["criteria"] == {
            "seniority_level": "Mid-Senior level",
            "employment_type": "Full-time",
        }

    def test_description_is_plain_text_with_list_items_and_breaks(self):
        posting = parse_guest_posting(POSTING_HTML)

        assert posting is not None
        assert posting["description"] == (
            "We build things.\n\nRequirements:\nPython 3.12\n\n- Django\n- PostgreSQL"
        )

    def test_page_without_a_description_is_none(self):
        assert parse_guest_posting(NO_DESCRIPTION_HTML) is None


class TestWorkerLimit:
    @pytest.mark.parametrize("n_workers", [MAX_WORKERS + 1, 6, 100, 0, -1])
    async def test_out_of_range_is_refused_before_any_request(self, n_workers):
        requests = []

        def handler(request):
            requests.append(request)
            return _ok(request)

        with pytest.raises(ValueError, match=f"between 1 and {MAX_WORKERS}"):
            await fetch_jobs(["1"], n_workers, transport=_transport(handler))

        assert requests == []

    async def test_the_cap_is_five(self):
        assert MAX_WORKERS == 5


class TestConcurrency:
    async def test_in_flight_requests_never_exceed_n_workers(self):
        in_flight = peak = 0

        async def handler(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return httpx2.Response(200, text=POSTING_HTML)

        ids = [str(1000 + i) for i in range(12)]
        result = await fetch_jobs(ids, 5, transport=_transport(handler))

        assert result["summary"] == {"requested": 12, "ok": 12, "failed": 0}
        assert peak == 5

    async def test_one_worker_is_sequential(self):
        in_flight = peak = 0

        async def handler(request):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx2.Response(200, text=POSTING_HTML)

        await fetch_jobs(["1", "2", "3", "4"], 1, transport=_transport(handler))

        assert peak == 1

    async def test_results_follow_the_order_of_the_ids(self):
        async def handler(request):
            job_id = request.url.path.rsplit("/", 1)[-1]
            # The first id answers last.
            await asyncio.sleep(0.05 if job_id == "1" else 0)
            return httpx2.Response(200, text=POSTING_HTML)

        result = await fetch_jobs(["1", "2", "3"], 3, transport=_transport(handler))

        assert [job["job_id"] for job in result["jobs"]] == ["1", "2", "3"]

    async def test_duplicate_ids_are_fetched_once(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return _ok(request)

        result = await fetch_jobs(
            ["7", "7", "https://www.linkedin.com/jobs/view/7/"],
            transport=_transport(handler),
        )

        assert len(seen) == 1
        assert result["summary"]["requested"] == 1

    async def test_progress_callback_reports_every_completion(self):
        updates = []

        async def report(completed, total):
            updates.append((completed, total))

        await fetch_jobs(
            ["1", "2", "3"],
            2,
            transport=_transport(_ok),
            on_progress=report,
        )

        assert sorted(updates) == [(1, 3), (2, 3), (3, 3)]


class TestFailures:
    async def test_each_failure_is_reported_and_the_others_still_succeed(self):
        def handler(request):
            job_id = request.url.path.rsplit("/", 1)[-1]
            if job_id == "404":
                return httpx2.Response(404)
            if job_id == "500":
                return httpx2.Response(500)
            if job_id == "closed":
                return httpx2.Response(200, text=NO_DESCRIPTION_HTML)
            if job_id == "boom":
                raise httpx2.ConnectError("no route")
            return httpx2.Response(200, text=POSTING_HTML)

        result = await fetch_jobs(
            ["1", "404", "500", "2", "3", "4"], 1, transport=_transport(handler)
        )
        by_id = {job["job_id"]: job for job in result["jobs"]}

        assert by_id["1"]["status"] == "ok"
        assert by_id["404"]["error"] == "not_found"
        assert by_id["500"]["error"] == "http_500"
        assert result["summary"] == {"requested": 6, "ok": 4, "failed": 2}

    async def test_a_page_without_a_description_is_description_missing(self):
        result = await fetch_jobs(
            ["9"],
            transport=_transport(
                lambda r: httpx2.Response(200, text=NO_DESCRIPTION_HTML)
            ),
        )

        assert result["jobs"][0]["error"] == "description_missing"

    async def test_a_network_error_is_request_failed_not_a_raise(self):
        def handler(request):
            raise httpx2.ConnectTimeout("slow")

        result = await fetch_jobs(["9"], transport=_transport(handler))

        assert result["jobs"][0]["error"] == "request_failed"
        assert result["summary"]["failed"] == 1

    @pytest.mark.parametrize("status", [429, 999])
    async def test_a_rate_limit_stops_jobs_that_have_not_started(self, status):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx2.Response(status)

        result = await fetch_jobs(
            ["1", "2", "3", "4"], 1, transport=_transport(handler)
        )
        errors = [job["error"] for job in result["jobs"]]

        assert errors == [
            "rate_limited",
            "skipped_rate_limited",
            "skipped_rate_limited",
            "skipped_rate_limited",
        ]
        assert len(seen) == 1

    async def test_a_malformed_id_fails_the_call_before_any_request(self):
        requests = []

        def handler(request):
            requests.append(request)
            return _ok(request)

        with pytest.raises(InvalidReferenceError):
            await fetch_jobs(["1", "/feed/"], transport=_transport(handler))

        assert requests == []

    async def test_an_empty_list_is_refused(self):
        with pytest.raises(ValueError, match="at least one"):
            await fetch_jobs([], transport=_transport(_ok))
