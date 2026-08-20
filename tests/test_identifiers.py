"""Tests for the profile/company reference repair applied before URL building.

The shapes below are the ones a caller actually passes. A regression is invisible
in production: a wrong identifier does not raise, it navigates somewhere and the
tool returns that page's text as if it were the requested profile.
"""

from urllib.parse import urlparse

import pytest

from linkedin_mcp_server.core.exceptions import (
    InvalidReferenceError,
    LinkedInScraperException,
)
from linkedin_mcp_server.scraping.identifiers import (
    company_page_url,
    job_view_url,
    messaging_thread_url,
    normalize_company_identifier,
    normalize_opaque_id,
    normalize_person_identifier,
    person_profile_url,
)

PROFILE_ID = "ACoAADdZSNYBiaacxEw6je-wVIKjGkKp-it0gD8"


class TestNormalizePersonIdentifier:
    @pytest.mark.parametrize(
        "value",
        [
            "williamhgates",
            "https://www.linkedin.com/in/williamhgates",
            "https://linkedin.com/in/williamhgates",
            "http://www.linkedin.com/in/williamhgates",
            # No scheme, as a browser address bar shows it.
            "www.linkedin.com/in/williamhgates",
            "linkedin.com/in/williamhgates",
            # The locale subdomain follows the country on the member's profile
            # and serves the profile itself rather than redirecting to www.
            "https://de.linkedin.com/in/williamhgates",
            "https://ca.linkedin.com/in/williamhgates",
            "https://uk.linkedin.com/in/williamhgates",
            "https://fr.linkedin.com/in/williamhgates",
            "https://br.linkedin.com/in/williamhgates",
            "https://jp.linkedin.com/in/williamhgates",
            # Mobile entry points and the mobile-web-lite path wrappers.
            "https://m.linkedin.com/in/williamhgates",
            "https://touch.linkedin.com/in/williamhgates",
            "https://www.linkedin.com/mwlite/in/williamhgates",
            "https://www.linkedin.com/mwlite/profile/in/williamhgates",
            # Trailing slash, share-sheet parameters, hash and sub-pages.
            "https://www.linkedin.com/in/williamhgates/",
            "https://www.linkedin.com/in/williamhgates?originalSubdomain=de",
            "https://www.linkedin.com/in/williamhgates?trk=public_profile",
            "https://www.linkedin.com/in/williamhgates#experience",
            "https://www.linkedin.com/in/williamhgates/recent-activity/all/",
        ],
    )
    def test_reduces_every_served_form_to_the_username(self, value: str):
        assert normalize_person_identifier(value) == "williamhgates"

    def test_is_idempotent(self):
        once = normalize_person_identifier("https://de.linkedin.com/in/williamhgates")
        assert normalize_person_identifier(once) == once

    def test_decodes_a_percent_encoded_non_ascii_username(self):
        value = "https://ru.linkedin.com/in/%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9"
        assert normalize_person_identifier(value) == "андрей"

    def test_decodes_a_bare_percent_encoded_username(self):
        # get_my_profile reads the username out of page.url after the /in/me/
        # redirect, and a browser reports that path encoded. Returning it as-is
        # would let the URL builder escape it again into %25D0, a path that is
        # not the profile.
        assert (
            normalize_person_identifier("%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9")
            == "андрей"
        )

    @pytest.mark.parametrize("value", ["%ZZ", "felix%", "felix%2", "%"])
    def test_refuses_a_malformed_escape(self, value: str):
        # unquote leaves a broken escape untouched instead of raising, so it
        # would otherwise survive into the URL.
        with pytest.raises(LinkedInScraperException):
            normalize_person_identifier(value)

    @pytest.mark.parametrize(
        "value", ["felix%2Ffoo", "felix%20foo", "felix%2E%2E%2Ffeed"]
    )
    def test_refuses_syntax_the_escapes_were_hiding(self, value: str):
        with pytest.raises(LinkedInScraperException):
            normalize_person_identifier(value)

    def test_preserves_case(self):
        # A share link from the app can carry a case-sensitive profile id where
        # the public identifier normally sits.
        assert (
            normalize_person_identifier(f"linkedin.com/in/{PROFILE_ID}") == PROFILE_ID
        )
        assert normalize_person_identifier("WilliamHGates") == "WilliamHGates"

    def test_refuses_a_value_that_would_escape_the_profile_path(self):
        # A browser resolves the dot segments away before the request, so this
        # navigates to /feed/ and returns the feed as if it were a profile.
        with pytest.raises(LinkedInScraperException):
            normalize_person_identifier("williamhgates/../../feed")

    @pytest.mark.parametrize(
        "value",
        [
            # Escaping cannot save these: a period is unreserved, so quote leaves
            # it alone and /in/../ still walks up a level.
            "..",
            ".",
            "https://www.linkedin.com/in/..",
            "https://www.linkedin.com/in/%2e%2e",
            # A second encoding layer would reach `..` on the next pass through
            # the normalizer, which connect_with_person performs.
            "%252e%252e",
            "https://www.linkedin.com/in/%252e%252e",
        ],
    )
    def test_refuses_an_exact_dot_segment(self, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize_person_identifier(value)

    @pytest.mark.parametrize("value", ["me", "ME", "%6d%65"])
    def test_refuses_the_signed_in_alias_in_every_form(self, value: str):
        # LinkedIn resolves /in/me/ to the authenticated member, so a lookup that
        # meant somebody else answers about the operator instead.
        with pytest.raises(InvalidReferenceError, match="get_my_profile"):
            normalize_person_identifier(value)

    @pytest.mark.parametrize(
        "value",
        [
            # unquote leaves %ZZ intact and turns %FF into a replacement
            # character, so tolerating either rewrites the destination rather
            # than refusing the reference.
            "https://www.linkedin.com/in/%ZZ",
            "https://www.linkedin.com/in/%FF",
            "https://www.linkedin.com/in/a%2Fb",
        ],
    )
    def test_refuses_a_malformed_escape_inside_a_full_url(self, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize_person_identifier(value)

    def test_never_collapses_a_link_into_the_signed_in_alias(self):
        # /in/me is LinkedIn's alias for the operator's own profile. Resolving a
        # link into it answers confidently about the wrong person.
        with pytest.raises(LinkedInScraperException):
            normalize_person_identifier("https://www.linkedin.com/in/me")

    @pytest.mark.parametrize(
        "value",
        [
            "https://www.linkedin.com/company/microsoft",
            "https://www.linkedin.com/school/rwth-aachen-university",
            "https://www.linkedin.com/feed/",
            "https://www.linkedin.com/sales/lead/ACwAA,NAME_SEARCH,abcd",
            # The legacy form carries name parts and id fragments, never the
            # public identifier, so no segment of it may be handed over.
            "https://www.linkedin.com/pub/bill-gates/1/2a/3b",
        ],
    )
    def test_refuses_a_linkedin_link_that_is_not_a_personal_profile(self, value: str):
        with pytest.raises(LinkedInScraperException):
            normalize_person_identifier(value)

    @pytest.mark.parametrize(
        "value",
        ["https://lnkd.in/eXaMpLe1", "lnkd.in/eXaMpLe1", "http://www.lnkd.in/eXaMpLe1"],
    )
    def test_refuses_a_short_link_that_only_a_redirect_resolves(self, value: str):
        with pytest.raises(LinkedInScraperException, match="shortened"):
            normalize_person_identifier(value)

    @pytest.mark.parametrize(
        "value",
        [
            # Host-suffix lookalikes must not be read as LinkedIn.
            "https://evil-linkedin.com/in/williamhgates",
            "https://linkedin.com.example.test/in/williamhgates",
            "https://example.com/in/williamhgates",
            "bill gates",
            "in/williamhgates",
            "",
            "   ",
        ],
    )
    def test_refuses_a_value_that_cannot_name_a_person(self, value: str):
        with pytest.raises(LinkedInScraperException):
            normalize_person_identifier(value)


class TestNormalizeCompanyIdentifier:
    @pytest.mark.parametrize(
        "value",
        [
            "microsoft",
            "https://www.linkedin.com/company/microsoft",
            "https://de.linkedin.com/company/microsoft/",
            "https://www.linkedin.com/company/microsoft/posts/",
            "https://www.linkedin.com/showcase/microsoft",
        ],
    )
    def test_reduces_a_company_link_to_the_slug(self, value: str):
        assert normalize_company_identifier(value) == "microsoft"

    def test_accepts_a_school_link(self):
        # /company/<school-slug> 301-redirects to /school/<slug>, so reusing the
        # slug on the company route resolves.
        value = "https://www.linkedin.com/school/rwth-aachen-university/"
        assert normalize_company_identifier(value) == "rwth-aachen-university"

    @pytest.mark.parametrize(
        "value",
        [
            "https://www.linkedin.com/in/williamhgates",
            "https://www.linkedin.com/feed/",
            "microsoft/../../feed",
            "",
        ],
    )
    def test_refuses_a_value_that_cannot_name_a_company(self, value: str):
        with pytest.raises(LinkedInScraperException):
            normalize_company_identifier(value)


class TestNormalizeOpaqueId:
    def test_passes_an_ordinary_id_through(self):
        assert normalize_opaque_id("4021126051", field="job_id") == "4021126051"

    @pytest.mark.parametrize("value", ["../../feed", "..", "a/b", "a b", "%ZZ", ""])
    def test_refuses_anything_that_could_redirect_the_path(self, value: str):
        # job_id="../../feed" builds /jobs/view/../../feed/, which a browser
        # resolves to /feed/ before it asks for anything.
        with pytest.raises(InvalidReferenceError):
            normalize_opaque_id(value, field="job_id")

    def test_names_the_field_it_was_given(self):
        with pytest.raises(InvalidReferenceError, match="thread_id"):
            normalize_opaque_id("../x", field="thread_id")


class TestShortLinkMessage:
    def test_asks_a_company_tool_for_a_company_slug(self):
        # A shared message told every caller to supply a personal profile URL,
        # which fails a second time in the company normalizer.
        with pytest.raises(InvalidReferenceError, match="/company/ slug"):
            normalize_company_identifier("https://lnkd.in/eXaMpLe1")

    def test_asks_a_person_tool_for_a_public_identifier(self):
        with pytest.raises(InvalidReferenceError, match="/in/ public identifier"):
            normalize_person_identifier("https://lnkd.in/eXaMpLe1")


class TestUrlBuilders:
    def test_escapes_the_identifier_as_a_single_path_segment(self):
        url = person_profile_url("андрей", "/")
        assert urlparse(url).path == "/in/%D0%B0%D0%BD%D0%B4%D1%80%D0%B5%D0%B9/"

    def test_a_suffix_is_the_only_way_to_add_path(self):
        # safe="" is the point: nothing the identifier carries may become path.
        assert person_profile_url("a/b") == "https://www.linkedin.com/in/a%2Fb"
        assert company_page_url("a/b") == "https://www.linkedin.com/company/a%2Fb"

    def test_escapes_a_job_and_thread_id_as_one_segment(self):
        assert job_view_url("a b", "/") == "https://www.linkedin.com/jobs/view/a%20b/"
        assert (
            messaging_thread_url("a/b")
            == "https://www.linkedin.com/messaging/thread/a%2Fb"
        )

    def test_builds_the_documented_profile_and_company_urls(self):
        assert (
            person_profile_url("williamhgates")
            == "https://www.linkedin.com/in/williamhgates"
        )
        assert (
            company_page_url("microsoft", "/people/")
            == "https://www.linkedin.com/company/microsoft/people/"
        )
