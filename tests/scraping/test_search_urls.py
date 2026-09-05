"""Tests for the URL grammar of LinkedIn's four search surfaces.

Browser-free by construction: every assertion here is against a string the
builders return, so nothing in this file needs a page, a mock or an event
loop. The expected codes are written out rather than read back from the maps
under test, because a test that derives its expectation from the table it
checks agrees with any table.
"""

from __future__ import annotations

import pytest

from linkedin_mcp_server.scraping.contracts import FilterValidationError
from linkedin_mcp_server.scraping.search_urls import (
    CONTENT_DATE_POSTED_MAP,
    EXPERIENCE_LEVEL_MAP,
    JOB_DATE_POSTED_MAP,
    JOB_TYPE_MAP,
    NETWORK_TOKENS,
    SORT_BY_MAP,
    WORK_TYPE_MAP,
    build_company_search_url,
    build_content_search_url,
    build_job_search_url,
    build_people_search_url,
)

JOBS = "https://www.linkedin.com/jobs/search/?"
PEOPLE = "https://www.linkedin.com/search/results/people/?"
COMPANIES = "https://www.linkedin.com/search/results/companies/?"
CONTENT = "https://www.linkedin.com/search/results/content/?"

# One keyword per encoding hazard: the plus sign LinkedIn would otherwise read
# as a space, non-ASCII that has to survive as UTF-8 percent-escapes, and a
# script with no ASCII in it at all.
ENCODED_KEYWORDS = [
    ("C++", "C%2B%2B"),
    ("Müller", "M%C3%BCller"),
    ("软件工程师", "%E8%BD%AF%E4%BB%B6%E5%B7%A5%E7%A8%8B%E5%B8%88"),
    ("a&b=c", "a%26b%3Dc"),
    ("python developer", "python+developer"),
]


class TestFilterTables:
    """The vocabulary itself, written out once so a silent edit is visible."""

    def test_job_date_posted_codes_are_linkedin_second_windows(self):
        assert JOB_DATE_POSTED_MAP == {
            "past_hour": "r3600",
            "past_24_hours": "r86400",
            "past_week": "r604800",
            "past_month": "r2592000",
        }

    def test_experience_levels_are_linkedin_ordinals(self):
        assert EXPERIENCE_LEVEL_MAP == {
            "internship": "1",
            "entry": "2",
            "associate": "3",
            "mid_senior": "4",
            "director": "5",
            "executive": "6",
        }

    def test_job_types_are_linkedin_initials(self):
        assert JOB_TYPE_MAP == {
            "full_time": "F",
            "part_time": "P",
            "contract": "C",
            "temporary": "T",
            "volunteer": "V",
            "internship": "I",
            "other": "O",
        }

    def test_work_types_are_linkedin_ordinals(self):
        assert WORK_TYPE_MAP == {"on_site": "1", "remote": "2", "hybrid": "3"}

    def test_sort_by_codes(self):
        assert SORT_BY_MAP == {"date": "DD", "relevance": "R"}

    def test_network_tokens_are_the_three_degrees(self):
        assert NETWORK_TOKENS == ("F", "S", "O")

    def test_content_date_posted_accepts_both_spellings(self):
        assert CONTENT_DATE_POSTED_MAP == {
            "past-24h": "past-24h",
            "past_24_hours": "past-24h",
            "past-week": "past-week",
            "past_week": "past-week",
            "past-month": "past-month",
            "past_month": "past-month",
        }

    def test_past_hour_is_the_one_job_window_content_search_has_not(self):
        # Documented asymmetry rather than an oversight: LinkedIn's Posts tab
        # offers Past 24 hours / week / month and nothing shorter. Reading it
        # off both tables keeps the two from drifting into an hour token that
        # LinkedIn would ignore while echoing it back.
        assert set(JOB_DATE_POSTED_MAP) - set(CONTENT_DATE_POSTED_MAP) == {"past_hour"}
        assert "past_hour" in JOB_DATE_POSTED_MAP

    def test_content_search_refuses_the_job_only_hour_window(self):
        with pytest.raises(FilterValidationError, match="Invalid date_posted"):
            build_content_search_url("python", date_posted="past_hour")

    def test_every_content_token_is_one_linkedin_recognizes(self):
        assert set(CONTENT_DATE_POSTED_MAP.values()) == {
            "past-24h",
            "past-week",
            "past-month",
        }


class TestBuildJobSearchUrl:
    def test_keywords_only(self):
        assert build_job_search_url("python developer") == (
            f"{JOBS}keywords=python+developer"
        )

    def test_every_parameter_keeps_its_recorded_position(self):
        # Whole-URL equality locks the concatenation order. Substring
        # assertions pass under any permutation.
        assert build_job_search_url(
            "python",
            location="Berlin",
            date_posted="past_week",
            job_type="full_time",
            experience_level="entry,mid_senior",
            work_type="remote",
            easy_apply=True,
            sort_by="date",
        ) == (
            f"{JOBS}keywords=python&location=Berlin&f_TPR=r604800&f_JT=F"
            "&f_E=2,4&f_WT=2&f_EA=true&sortBy=DD"
        )

    @pytest.mark.parametrize("keywords,encoded", ENCODED_KEYWORDS)
    def test_keywords_are_percent_encoded(self, keywords: str, encoded: str):
        assert build_job_search_url(keywords) == f"{JOBS}keywords={encoded}"

    def test_empty_keywords_still_produce_the_parameter(self):
        assert build_job_search_url("") == f"{JOBS}keywords="

    def test_location_is_percent_encoded(self):
        assert build_job_search_url("python", location="New York") == (
            f"{JOBS}keywords=python&location=New+York"
        )

    @pytest.mark.parametrize(
        "date_posted,code",
        [
            ("past_hour", "r3600"),
            ("past_24_hours", "r86400"),
            ("past_week", "r604800"),
            ("past_month", "r2592000"),
        ],
    )
    def test_date_posted_aliases(self, date_posted: str, code: str):
        assert build_job_search_url("python", date_posted=date_posted) == (
            f"{JOBS}keywords=python&f_TPR={code}"
        )

    def test_date_posted_alias_coverage_is_exhaustive(self):
        assert set(JOB_DATE_POSTED_MAP) == {
            "past_hour",
            "past_24_hours",
            "past_week",
            "past_month",
        }

    def test_unknown_date_posted_passes_through(self):
        assert build_job_search_url("python", date_posted="r7200") == (
            f"{JOBS}keywords=python&f_TPR=r7200"
        )

    def test_padded_date_posted_looks_up_trimmed_but_falls_back_untrimmed(self):
        # The lookup strips, the fallback does not. A padded known alias is
        # therefore mapped, while a padded unknown value reaches LinkedIn with
        # its spaces encoded — which is what makes such a filter visible in the
        # url rather than silently ignored.
        assert build_job_search_url("python", date_posted="  past_week ") == (
            f"{JOBS}keywords=python&f_TPR=r604800"
        )
        assert build_job_search_url("python", date_posted=" r7200 ") == (
            f"{JOBS}keywords=python&f_TPR=+r7200+"
        )

    @pytest.mark.parametrize(
        "level,code",
        [
            ("internship", "1"),
            ("entry", "2"),
            ("associate", "3"),
            ("mid_senior", "4"),
            ("director", "5"),
            ("executive", "6"),
        ],
    )
    def test_experience_level_aliases(self, level: str, code: str):
        assert build_job_search_url("python", experience_level=level) == (
            f"{JOBS}keywords=python&f_E={code}"
        )

    @pytest.mark.parametrize(
        "job_type,code",
        [
            ("full_time", "F"),
            ("part_time", "P"),
            ("contract", "C"),
            ("temporary", "T"),
            ("volunteer", "V"),
            ("internship", "I"),
            ("other", "O"),
        ],
    )
    def test_job_type_aliases(self, job_type: str, code: str):
        assert build_job_search_url("python", job_type=job_type) == (
            f"{JOBS}keywords=python&f_JT={code}"
        )

    @pytest.mark.parametrize(
        "work_type,code", [("on_site", "1"), ("remote", "2"), ("hybrid", "3")]
    )
    def test_work_type_aliases(self, work_type: str, code: str):
        assert build_job_search_url("python", work_type=work_type) == (
            f"{JOBS}keywords=python&f_WT={code}"
        )

    @pytest.mark.parametrize("sort_by,code", [("date", "DD"), ("relevance", "R")])
    def test_sort_by_aliases(self, sort_by: str, code: str):
        assert build_job_search_url("python", sort_by=sort_by) == (
            f"{JOBS}keywords=python&sortBy={code}"
        )

    def test_csv_values_are_normalized_element_by_element(self):
        assert build_job_search_url(
            "python",
            job_type="full_time,contract",
            experience_level="entry,director",
            work_type="on_site,hybrid",
        ) == (f"{JOBS}keywords=python&f_JT=F,C&f_E=2,5&f_WT=1,3")

    def test_csv_elements_are_trimmed_before_lookup(self):
        assert build_job_search_url(
            "python", experience_level=" entry , director "
        ) == (f"{JOBS}keywords=python&f_E=2,5")

    def test_unknown_csv_elements_pass_through_beside_known_ones(self):
        # LinkedIn's own codes are accepted verbatim, so a caller who already
        # knows one is not forced through this server's spelling.
        assert build_job_search_url("python", job_type="F,other,X") == (
            f"{JOBS}keywords=python&f_JT=F,O,X"
        )

    def test_unknown_sort_by_passes_through(self):
        assert build_job_search_url("python", sort_by="XX") == (
            f"{JOBS}keywords=python&sortBy=XX"
        )

    def test_easy_apply_true_adds_the_flag(self):
        assert build_job_search_url("python", easy_apply=True) == (
            f"{JOBS}keywords=python&f_EA=true"
        )

    def test_easy_apply_false_omits_the_flag(self):
        assert build_job_search_url("python", easy_apply=False) == (
            f"{JOBS}keywords=python"
        )

    def test_empty_filter_values_are_omitted_entirely(self):
        assert build_job_search_url(
            "python",
            location="",
            date_posted="",
            job_type="",
            experience_level="",
            work_type="",
            sort_by="",
        ) == (f"{JOBS}keywords=python")

    def test_whitespace_filter_values_are_sent_rather_than_dropped(self):
        # Only emptiness is a "no filter" signal here. Whitespace survives to
        # the url, where LinkedIn ignores it: visible in the request rather
        # than quietly discarded on the way out.
        assert build_job_search_url("python", location="  ") == (
            f"{JOBS}keywords=python&location=++"
        )


class TestBuildPeopleSearchUrl:
    def test_keywords_only(self):
        assert build_people_search_url("recruiter") == f"{PEOPLE}keywords=recruiter"

    def test_every_parameter_keeps_its_recorded_position(self):
        assert build_people_search_url(
            "engineer",
            location="Seattle",
            network=["F"],
            current_company="1115",
        ) == (
            f"{PEOPLE}keywords=engineer&location=Seattle"
            "&network=%5B%22F%22%5D&currentCompany=%5B%221115%22%5D"
        )

    @pytest.mark.parametrize("keywords,encoded", ENCODED_KEYWORDS)
    def test_keywords_are_percent_encoded(self, keywords: str, encoded: str):
        assert build_people_search_url(keywords) == f"{PEOPLE}keywords={encoded}"

    def test_empty_keywords_still_produce_the_parameter(self):
        assert build_people_search_url("") == f"{PEOPLE}keywords="

    @pytest.mark.parametrize(
        "token,encoded",
        [("F", "%5B%22F%22%5D"), ("S", "%5B%22S%22%5D"), ("O", "%5B%22O%22%5D")],
    )
    def test_every_network_token_is_accepted(self, token: str, encoded: str):
        assert build_people_search_url("engineer", network=[token]) == (
            f"{PEOPLE}keywords=engineer&network={encoded}"
        )

    def test_multiple_network_tokens_share_one_json_facet(self):
        assert build_people_search_url("engineer", network=["F", "S"]) == (
            f"{PEOPLE}keywords=engineer&network=%5B%22F%22%2C%22S%22%5D"
        )

    def test_network_token_order_is_the_caller_order(self):
        assert build_people_search_url("engineer", network=["S", "F"]) == (
            f"{PEOPLE}keywords=engineer&network=%5B%22S%22%2C%22F%22%5D"
        )

    @pytest.mark.parametrize("token", ["X", "f", "1st", "", " F"])
    def test_invalid_network_token_is_refused(self, token: str):
        with pytest.raises(FilterValidationError) as error:
            build_people_search_url("engineer", network=[token])

        assert repr(token) in str(error.value)
        assert "['F', 'S', 'O']" in str(error.value)

    def test_every_invalid_token_is_named_at_once(self):
        with pytest.raises(FilterValidationError) as error:
            build_people_search_url("engineer", network=["F", "X", "Y"])

        assert str(error.value) == (
            "Invalid network token(s) ['X', 'Y']; expected any of ['F', 'S', 'O']"
        )

    def test_empty_network_list_is_validated_and_then_omitted(self):
        assert build_people_search_url("engineer", network=[]) == (
            f"{PEOPLE}keywords=engineer"
        )

    def test_absent_network_is_omitted(self):
        assert build_people_search_url("engineer", network=None) == (
            f"{PEOPLE}keywords=engineer"
        )

    def test_numeric_current_company_becomes_a_json_facet(self):
        assert build_people_search_url("engineer", current_company="1115") == (
            f"{PEOPLE}keywords=engineer&currentCompany=%5B%221115%22%5D"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "SAP",
            "1115 ",
            " 1115",
            "11 15",
            "+1115",
            "-1115",
            "1115.0",
            "urn:li:fsd_company:1115",
            # ASCII decimal only: str.isdigit() accepts Arabic-Indic digits,
            # and LinkedIn's URN ids are never written in them.
            "١١١٥",
        ],
    )
    def test_non_numeric_current_company_is_refused(self, value: str):
        with pytest.raises(FilterValidationError) as error:
            build_people_search_url("engineer", current_company=value)

        assert repr(value) in str(error.value)
        assert "numeric LinkedIn company URN id" in str(error.value)

    def test_empty_current_company_is_omitted_rather_than_refused(self):
        assert build_people_search_url("engineer", current_company="") == (
            f"{PEOPLE}keywords=engineer"
        )

    def test_location_is_percent_encoded(self):
        assert build_people_search_url("engineer", location="São Paulo") == (
            f"{PEOPLE}keywords=engineer&location=S%C3%A3o+Paulo"
        )

    def test_network_is_refused_before_a_company_urn_is_looked_at(self):
        # Order matters for the message a caller reads back: both filters are
        # wrong here and the network token is the one reported.
        with pytest.raises(FilterValidationError, match="Invalid network token"):
            build_people_search_url("engineer", network=["X"], current_company="SAP")


class TestBuildCompanySearchUrl:
    def test_keywords_only(self):
        assert build_company_search_url("engine") == f"{COMPANIES}keywords=engine"

    @pytest.mark.parametrize("keywords,encoded", ENCODED_KEYWORDS)
    def test_keywords_are_percent_encoded(self, keywords: str, encoded: str):
        assert build_company_search_url(keywords) == f"{COMPANIES}keywords={encoded}"

    def test_empty_keywords_still_produce_the_parameter(self):
        assert build_company_search_url("") == f"{COMPANIES}keywords="

    def test_whitespace_keywords_reach_linkedin_encoded(self):
        assert build_company_search_url("   ") == f"{COMPANIES}keywords=+++"


class TestBuildContentSearchUrl:
    def test_keywords_carry_the_faceted_search_origin(self):
        assert build_content_search_url("Buscamos Unity") == (
            f"{CONTENT}keywords=Buscamos+Unity&origin=FACETED_SEARCH"
        )

    def test_every_parameter_keeps_its_recorded_position(self):
        assert build_content_search_url("Buscamos Unity", date_posted="past-week") == (
            f"{CONTENT}keywords=Buscamos+Unity&origin=FACETED_SEARCH"
            "&datePosted=%5B%22past-week%22%5D"
        )

    @pytest.mark.parametrize("keywords,encoded", ENCODED_KEYWORDS)
    def test_keywords_are_percent_encoded(self, keywords: str, encoded: str):
        assert build_content_search_url(keywords) == (
            f"{CONTENT}keywords={encoded}&origin=FACETED_SEARCH"
        )

    def test_empty_keywords_still_produce_the_parameter(self):
        assert build_content_search_url("") == (
            f"{CONTENT}keywords=&origin=FACETED_SEARCH"
        )

    @pytest.mark.parametrize(
        "date_posted,token",
        [
            ("past-24h", "past-24h"),
            ("past_24_hours", "past-24h"),
            ("past-week", "past-week"),
            ("past_week", "past-week"),
            ("past-month", "past-month"),
            ("past_month", "past-month"),
        ],
    )
    def test_date_posted_aliases(self, date_posted: str, token: str):
        assert build_content_search_url("python", date_posted=date_posted) == (
            f"{CONTENT}keywords=python&origin=FACETED_SEARCH"
            f"&datePosted=%5B%22{token}%22%5D"
        )

    def test_date_posted_alias_coverage_is_exhaustive(self):
        assert set(CONTENT_DATE_POSTED_MAP) == {
            "past-24h",
            "past_24_hours",
            "past-week",
            "past_week",
            "past-month",
            "past_month",
        }

    def test_padded_date_posted_is_trimmed_before_lookup(self):
        assert build_content_search_url("python", date_posted="  past_week ") == (
            f"{CONTENT}keywords=python&origin=FACETED_SEARCH"
            "&datePosted=%5B%22past-week%22%5D"
        )

    def test_absent_date_posted_omits_the_facet(self):
        assert "datePosted" not in build_content_search_url("python")

    @pytest.mark.parametrize("date_posted", ["", "   ", "\t\n"])
    def test_blank_date_posted_omits_the_facet(self, date_posted: str):
        # Blank is "no filter", not an invalid token: appending it would send
        # an empty facet LinkedIn ignores while the request reads as filtered.
        assert build_content_search_url("python", date_posted=date_posted) == (
            f"{CONTENT}keywords=python&origin=FACETED_SEARCH"
        )

    @pytest.mark.parametrize(
        "date_posted", ["past-day", "past_hour", "PAST-WEEK", "past week", "r604800"]
    )
    def test_unknown_date_posted_is_refused_rather_than_passed_through(
        self, date_posted: str
    ):
        # The opposite of job search, and deliberately so: LinkedIn echoes an
        # unrecognized content token back in the url and then ignores it, so
        # passing one through returns an unfiltered answer that looks filtered.
        with pytest.raises(FilterValidationError) as error:
            build_content_search_url("python", date_posted=date_posted)

        assert repr(date_posted) in str(error.value)
        assert "past-24h" in str(error.value)
