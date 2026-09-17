"""Tests for feed permalink recognition across DOM anchors and SDUI payloads."""

from pathlib import Path

from linkedin_mcp_server.scraping.feed_payload import (
    append_permalink_references,
    build_feed_references,
    is_permalink_payload_response,
    permalink_paths_from_payload,
)
from linkedin_mcp_server.scraping.link_metadata import Reference


class TestBuildFeedReferences:
    """Tests for build_feed_references SDUI-capture / DOM-anchor merging."""

    def test_sdui_urls_become_relative_feed_post_references(self):
        captured = [
            "https://www.linkedin.com/posts/alice_some-slug-ugcPost-1-xx",
            "https://www.linkedin.com/posts/bob_other-post-share-2-yy",
        ]
        refs = build_feed_references([], captured)
        assert refs == [
            {
                "kind": "feed_post",
                "url": "/posts/alice_some-slug-ugcPost-1-xx",
                "context": "feed",
            },
            {
                "kind": "feed_post",
                "url": "/posts/bob_other-post-share-2-yy",
                "context": "feed",
            },
        ]

    def test_duplicate_sdui_urls_are_deduped(self):
        captured = [
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
        ]
        refs = build_feed_references([], captured)
        assert len(refs) == 1
        assert refs[0]["url"] == "/posts/alice_x-ugcPost-1-xx"

    def test_dom_anchor_feed_update_passes_through(self):
        # DOM anchors that classify_link recognises as feed_post survive
        # the merge alongside SDUI captures.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:1234567890/",
                "text": "View post",
            }
        ]
        refs = build_feed_references(raw_anchors, [])
        assert any(
            r["url"] == "/feed/update/urn:li:activity:1234567890/"
            and r["kind"] == "feed_post"
            for r in refs
        )

    def test_non_posts_paths_in_sdui_capture_are_skipped(self):
        # Defensive: only /posts/<slug> shapes count for SDUI append.
        captured = [
            "https://www.linkedin.com/in/someuser/",
            "https://www.linkedin.com/posts/alice_x-ugcPost-1-xx",
        ]
        refs = build_feed_references([], captured)
        assert [r["url"] for r in refs] == ["/posts/alice_x-ugcPost-1-xx"]

    def test_cap_matches_num_posts_ceiling(self):
        captured = [
            f"https://www.linkedin.com/posts/p{i}-ugcPost-{i}-xx" for i in range(60)
        ]
        refs = build_feed_references([], captured)
        # Cap is 50, mirroring _REFERENCE_CAPS["feed"] / num_posts <= 50.
        assert len(refs) == 50

    def test_non_feed_post_dom_anchors_are_filtered(self):
        # Sidebar profile / company / external anchors must not crowd
        # out SDUI permalinks — references["feed"] is feed_post-only.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/in/sidebar-user/",
                "text": "Sidebar User",
            },
            {
                "href": "https://www.linkedin.com/company/some-corp/",
                "text": "Some Corp",
            },
            {
                "href": "https://example.com/external/",
                "text": "External Link",
            },
        ]
        refs = build_feed_references(raw_anchors, [])
        assert refs == []

    def test_feed_post_dom_anchors_coexist_with_sdui_captures(self):
        # The two sources fold into the same feed_post kind without
        # collapsing across URL shapes pointing at the same post.
        raw_anchors = [
            {
                "href": "https://www.linkedin.com/feed/update/urn:li:activity:111/",
                "text": "View post",
            }
        ]
        captured = ["https://www.linkedin.com/posts/alice_x-ugcPost-1-xx"]
        refs = build_feed_references(raw_anchors, captured)
        urls = [r["url"] for r in refs]
        kinds = {r["kind"] for r in refs}
        assert urls == [
            "/feed/update/urn:li:activity:111/",
            "/posts/alice_x-ugcPost-1-xx",
        ]
        assert kinds == {"feed_post"}


class TestIsPermalinkPayloadResponse:
    def test_voyager_normalized_json_counts(self):
        assert is_permalink_payload_response(
            "https://www.linkedin.com/voyager/api/graphql",
            "application/vnd.linkedin.normalized+json+2.1",
        )

    def test_json_media_type_parameters_are_stripped(self):
        assert is_permalink_payload_response(
            "https://www.linkedin.com/voyager/api/search",
            "application/json; charset=utf-8",
        )

    def test_document_html_counts(self):
        assert is_permalink_payload_response(
            "https://www.linkedin.com/search/results/content/?keywords=python",
            "text/html",
        )

    def test_binary_media_does_not_count(self):
        for media in ("image/png", "text/css", "font/woff2", "video/mp4", ""):
            assert not is_permalink_payload_response(
                "https://www.linkedin.com/voyager/api/graphql", media
            )

    def test_non_linkedin_hosts_never_count(self):
        assert not is_permalink_payload_response(
            "https://evil.example/voyager/api/graphql",
            "application/json",
        )


class TestPermalinkPathsFromPayload:
    def test_slug_urls_in_both_escape_forms(self):
        payload = (
            '{"a":"https://www.linkedin.com/posts/alice_hi-ugcPost-1234567890-x",'
            '"b":"https:\\u002f\\u002fwww.linkedin.com\\u002fposts\\u002f'
            'bob_yo-share-9876543210-y"}'
        )
        assert permalink_paths_from_payload(payload) == [
            "/posts/alice_hi-ugcPost-1234567890-x",
            "/posts/bob_yo-share-9876543210-y",
        ]

    def test_post_entity_urns_become_feed_update_paths(self):
        payload = (
            '"urn:li:ugcPost:7505583248597512192" '
            '"urn:li:share:7123456789012345678" '
            '"urn:li:activity:7000000000000000000"'
        )
        assert permalink_paths_from_payload(payload) == [
            "/feed/update/urn:li:ugcPost:7505583248597512192/",
            "/feed/update/urn:li:share:7123456789012345678/",
            "/feed/update/urn:li:activity:7000000000000000000/",
        ]

    def test_non_post_urns_are_not_matched(self):
        payload = (
            '"urn:li:comment:(ugcPost:7505583248597512192,7505593519835721729)" '
            '"urn:li:fsd_profile:ACoAAABCD1234" "urn:li:ugcPost:123"'
        )
        assert permalink_paths_from_payload(payload) == []

    def test_slug_form_precedes_urn_form_and_dedupes(self):
        slug = "https://www.linkedin.com/posts/alice_x-ugcPost-1234567890-z"
        payload = (
            f'{{"urn":"urn:li:ugcPost:1234567890","url":"{slug}","again":"{slug}"}}'
        )
        assert permalink_paths_from_payload(payload) == [
            "/posts/alice_x-ugcPost-1234567890-z",
            "/feed/update/urn:li:ugcPost:1234567890/",
        ]


class TestAppendPermalinkReferences:
    def test_dom_references_stay_in_front_and_are_never_displaced(self):
        dom: list[Reference] = [
            {"kind": "person", "url": "/in/ada/", "text": "Ada"},
            {"kind": "company", "url": "/company/acme/", "text": "Acme"},
        ]
        refs = append_permalink_references(
            dom, ["/feed/update/urn:li:ugcPost:1234567890/"], context="search_results"
        )
        assert refs[:2] == dom
        assert refs[2:] == [
            {
                "kind": "feed_post",
                "url": "/feed/update/urn:li:ugcPost:1234567890/",
                "context": "search_results",
            }
        ]

    def test_paths_already_present_as_dom_urls_are_skipped(self):
        dom: list[Reference] = [
            {"kind": "feed_post", "url": "/feed/update/urn:li:activity:123/"}
        ]
        refs = append_permalink_references(
            dom,
            ["/feed/update/urn:li:activity:123/", "/posts/alice_x-ugcPost-9-xx"],
            context="search_results",
        )
        assert [r["url"] for r in refs] == [
            "/feed/update/urn:li:activity:123/",
            "/posts/alice_x-ugcPost-9-xx",
        ]

    def test_empty_capture_returns_dom_references_unchanged(self):
        dom: list[Reference] = [{"kind": "person", "url": "/in/ada/"}]
        assert append_permalink_references(dom, [], context="search_results") == dom

    def test_appended_permalinks_are_capped(self):
        captured = [
            f"/feed/update/urn:li:ugcPost:{7505583248597512000 + i}/" for i in range(60)
        ]
        refs = append_permalink_references([], captured, context="search_results")
        # Cap mirrors build_feed_references' 50-entry ceiling.
        assert len(refs) == 50
        assert refs[0]["url"] == "/feed/update/urn:li:ugcPost:7505583248597512000/"
        assert refs[-1]["url"] == "/feed/update/urn:li:ugcPost:7505583248597512049/"


CONTENT_SEARCH_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "scraping" / "content_search_document.html"
)
CONTENT_SEARCH_URL = "https://www.linkedin.com/search/results/content/?keywords=sample"


class TestContentSearchDocumentFixture:
    """Guards the live payload shape behind search_posts permalinks.

    The fixture is a sanitized excerpt of a real logged-in content-search
    document, so recognition and extraction run against the genuine field
    names and escaping rather than hand-built snippets. If LinkedIn drops
    postSlugUrl or the permalink URNs from the document, this fails.
    """

    def _fixture_text(self) -> str:
        return CONTENT_SEARCH_FIXTURE.read_text(encoding="utf-8")

    def test_the_real_document_content_type_is_recognized(self):
        assert is_permalink_payload_response(
            CONTENT_SEARCH_URL, "text/html; charset=utf-8"
        )

    def test_the_document_carries_slugged_and_urn_permalinks(self):
        urls = set(permalink_paths_from_payload(self._fixture_text()))
        assert urls == {
            "/posts/sample-topic-ugcPost-7214916299250905089-2_2a",
            "/posts/sample-topic-ugcPost-7424761687905288194-WwYG",
            "/posts/sample-topic-ugcPost-7481465171073236992-U1pG",
            "/feed/update/urn:li:activity:7214916299838103554/",
            "/feed/update/urn:li:activity:7424761688761069570/",
            "/feed/update/urn:li:activity:7481482393992921088/",
        }

    def test_no_member_identifiers_survive_in_permalinks(self):
        urls = permalink_paths_from_payload(self._fixture_text())
        assert all("member" not in url and "ACoA" not in url for url in urls)
