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
    normalize_job_id,
    normalize_opaque_id,
    normalize_person_identifier,
    normalize_thread_id,
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

    def test_raises_the_dedicated_invalid_reference_type(self):
        # The type is what keeps error_handler from attaching an issue-report
        # template to a caller mistake, so the base class is not enough here.
        with pytest.raises(InvalidReferenceError):
            normalize_person_identifier("https://www.linkedin.com/feed/")
        with pytest.raises(InvalidReferenceError):
            normalize_company_identifier("https://www.linkedin.com/in/williamhgates")

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
        ],
    )
    def test_reduces_a_company_link_to_the_slug(self, value: str):
        assert normalize_company_identifier(value) == "microsoft"

    @pytest.mark.parametrize(
        "value",
        [
            "https://www.linkedin.com/school/rwth-aachen-university/",
            "https://www.linkedin.com/showcase/microsoft",
        ],
    )
    def test_refuses_a_route_it_cannot_build(self, value: str):
        # The slug used to be rebuilt under /company/, which 301-redirects to the
        # organization root. Right for the root, wrong for every section the
        # company scrape appends: /company/<school-slug>/jobs/ redirects to the
        # school root too, and nothing checks where it landed, so root content
        # was recorded under the requested section.
        with pytest.raises(InvalidReferenceError):
            normalize_company_identifier(value)

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


class TestReferencesThisServerEmits:
    """link_metadata renders every reference as a site-relative path, and the
    tool descriptions send callers back through them. A reference handed
    straight back is a caller following the instructions, so refusing one would
    refuse this server's own output."""

    @pytest.mark.parametrize(
        ("reference", "expected"),
        [
            ("/in/williamhgates/", "williamhgates"),
            ("/in/williamhgates", "williamhgates"),
            ("/mwlite/in/williamhgates/", "williamhgates"),
        ],
    )
    def test_person_reference_yields_the_identifier(self, reference, expected):
        assert normalize_person_identifier(reference) == expected

    @pytest.mark.parametrize(
        ("reference", "expected"),
        [("/company/microsoft/", "microsoft")],
    )
    def test_company_reference_yields_the_slug(self, reference, expected):
        assert normalize_company_identifier(reference) == expected

    def test_thread_reference_yields_the_id(self):
        assert normalize_thread_id("/messaging/thread/2-abc123/") == "2-abc123"

    def test_thread_id_still_passes_through(self):
        assert normalize_thread_id("2-abc123") == "2-abc123"

    def test_job_reference_yields_the_id(self):
        assert normalize_job_id("/jobs/view/4252026496/") == "4252026496"

    def test_a_relative_path_of_the_wrong_kind_is_still_refused(self):
        with pytest.raises(InvalidReferenceError):
            normalize_person_identifier("/company/microsoft/")

    def test_dot_segments_survive_neither_form(self):
        # The relative form is a second way into the same builder, so it has to
        # refuse what the bare form refuses.
        for value in ("/in/../../feed/", "/messaging/thread/../../feed/"):
            with pytest.raises(InvalidReferenceError):
                normalize_person_identifier(value)


class TestJobIdIsANumber:
    """Everything here that produces a job id extracts \\d+, and the tool
    documents one. A word navigates to a 404 that costs a page load to learn."""

    def test_a_word_is_refused(self):
        with pytest.raises(InvalidReferenceError):
            normalize_job_id("abc")

    def test_the_number_passes(self):
        assert normalize_job_id("4252026496") == "4252026496"


class TestLoneSurrogate:
    """A lone surrogate survives JSON parsing and every syntax check, then
    raises inside `quote` while the URL is built. The caller would see an
    unexpected tool failure carrying issue-report diagnostics instead of the
    correction this module exists to give."""

    @pytest.mark.parametrize(
        "normalize",
        [normalize_person_identifier, normalize_company_identifier, normalize_job_id],
    )
    def test_it_is_refused_rather_than_crashing_the_url_builder(self, normalize):
        with pytest.raises(InvalidReferenceError):
            normalize("\ud800")


class TestDotSegmentsAnywhere:
    """A browser resolves dot segments across the whole path before it asks for
    anything, so reading the route and the segment after it answers about a
    different page than the reference names. `/in/alice/../../in/bob` is Bob to
    a browser and was Alice here."""

    @pytest.mark.parametrize(
        ("normalize", "value"),
        [
            (normalize_person_identifier, "/in/alice/../../in/bob"),
            (normalize_person_identifier, "/in/alice/%2e%2e/%2e%2e/in/bob"),
            (normalize_company_identifier, "/company/a/../../company/b"),
            (normalize_job_id, "/jobs/view/123/../../../jobs/view/456"),
            (
                normalize_thread_id,
                "/messaging/thread/2-a/../../../messaging/thread/2-b",
            ),
        ],
    )
    def test_the_whole_path_is_judged(self, normalize, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize(value)

    @pytest.mark.parametrize(
        ("normalize", "value"),
        [
            (normalize_person_identifier, "/in/alice/x\\..\\..\\..\\in\\bob"),
            (normalize_company_identifier, "/company/a/x\\..\\..\\..\\company\\b"),
            (normalize_job_id, "/jobs/view/123/x\\..\\..\\..\\..\\jobs\\view\\456"),
            (
                normalize_thread_id,
                "/messaging/thread/2-a/x\\..\\..\\..\\..\\messaging\\thread\\2-b",
            ),
        ],
    )
    def test_a_backslash_cannot_smuggle_the_dot_segments_past(
        self, normalize, value: str
    ):
        # The URL Standard makes a backslash a path separator for http(s), and
        # `urlparse` does not, so splitting on "/" alone reads a path no browser
        # navigates. Measured with a conforming parser, the first value resolves
        # to /in/bob while this read Alice.
        with pytest.raises(InvalidReferenceError):
            normalize(value)

    def test_a_real_sub_page_still_resolves(self):
        # Only dot segments are refused; the sub-pages a profile URL carries are
        # what made reading past the identifier necessary in the first place.
        assert normalize_person_identifier("/in/alice/recent-activity/all/") == "alice"


class TestTheParserAgreesWithABrowser:
    """Each case was compared against a conforming URL parser before it was
    written down. The rule is one-directional: this may refuse an address a
    browser loads, and it may never resolve to a different page than one."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # A backslash separates paths and nothing else, so the guard that
            # refuses one must stop at the first `?` or `#` or it rejects the
            # tracking parameters every shared LinkedIn link carries.
            ("https://www.linkedin.com/in/alice?trk=foo\\bar", "alice"),
            ("https://www.linkedin.com/in/alice#foo\\bar", "alice"),
            # A network-path reference takes its scheme from the page it sits
            # on, which for a LinkedIn address is always https.
            ("//de.linkedin.com/in/alice", "alice"),
            ("//www.linkedin.com/in/alice/recent-activity/all/", "alice"),
            # An explicitly written default port is the same address, judged
            # against the scheme's own default rather than against 443 alone.
            ("https://www.linkedin.com:443/in/alice", "alice"),
            ("http://www.linkedin.com:80/in/alice", "alice"),
            # A single trailing dot is the fully qualified spelling of the host,
            # and LinkedIn answers on it.
            ("https://www.linkedin.com./in/alice", "alice"),
        ],
    )
    def test_a_form_a_browser_loads_still_resolves(self, value: str, expected: str):
        assert normalize_person_identifier(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            # LinkedIn answers on 443. Reading the path and dropping the port
            # would rebuild an address the caller never named as the live page.
            "https://www.linkedin.com:444/in/williamhgates",
            # Port 443 spoken in cleartext is a request LinkedIn answers 400 for.
            # Taking it for a default would rebuild it as the live HTTPS profile.
            "http://www.linkedin.com:443/in/williamhgates",
            "https://www.linkedin.com:80/in/williamhgates",
            # One trailing dot is a spelling; two are not a host.
            "https://www.linkedin.com../in/williamhgates",
            "https://www.linkedin.com:99999/in/williamhgates",
            "https://www.linkedin.com:foo/in/williamhgates",
            # A browser reads this as a path on linkedin.com; anything splitting
            # on "/" reads it as a different host entirely.
            "https://www.linkedin.com\\evil.example/in/alice",
        ],
    )
    def test_an_address_a_browser_does_not_load_is_refused(self, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize_person_identifier(value)


class TestSluggedJobUrls:
    """LinkedIn serves a job under both `/jobs/view/<id>/` and
    `/jobs/view/<title>-at-<company>-<id>/`, and the slugged one is what an
    address bar holds. Measured live, both 301 to the same destination."""

    def test_the_slug_resolves_to_the_id_the_numeric_form_carries(self):
        slugged = "https://www.linkedin.com/jobs/view/software-engineer-new-grad-at-ixl-learning-1967281839/"
        assert normalize_job_id(slugged) == normalize_job_id("/jobs/view/1967281839/")
        assert normalize_job_id(slugged) == "1967281839"

    def test_a_bare_argument_stays_strictly_numeric(self):
        # Outside a URL the words are not a slug LinkedIn wrote, they are a
        # wrong value, and a page load is what it costs to learn that.
        with pytest.raises(InvalidReferenceError):
            normalize_job_id("software-engineer-1967281839")


class TestNothingTidiesTheReference:
    """Each of these used to be cleaned up into a real target: the decode was
    followed by a strip, the URL parse drops control characters, and empty segments
    were filtered away. LinkedIn answers 404 for the duplicate-segment path, so
    accepting it names a page the reference does not."""

    @pytest.mark.parametrize(
        ("normalize", "value"),
        [
            (normalize_person_identifier, "/in/%20alice/"),
            (normalize_person_identifier, "/in/foo\nbar/"),
            (normalize_person_identifier, "/in/foo\tbar/"),
            (normalize_company_identifier, "/company//microsoft/"),
            (normalize_thread_id, "/messaging/thread/%202-abc/"),
            (normalize_thread_id, "/messaging//thread//2-abc/"),
        ],
    )
    def test_it_is_refused(self, normalize, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize(value)


class TestIdentifierAllowlist:
    """A public identifier is letters, digits, hyphen and underscore. The old
    rule listed forbidden syntax instead, so a value carrying none of it passed
    and spent a page load on a 404."""

    @pytest.mark.parametrize(
        "value", ["foo@example.com", "foo:bar", "foo!bar", "foo.bar"]
    )
    def test_a_value_that_cannot_be_one_is_refused(self, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize_company_identifier(value)

    @pytest.mark.parametrize(
        "value", ["williamhgates", "felix-krueckel", "андрей", "a_b"]
    )
    def test_a_real_one_passes(self, value: str):
        assert normalize_person_identifier(value) == value
