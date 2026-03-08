"""Tests for compact LinkedIn reference extraction helpers."""

from linkedin_mcp_server.scraping.link_metadata import (
    RawReference,
    build_references,
    dedupe_references,
)


class TestBuildReferences:
    def test_canonicalizes_and_types_linkedin_urls(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/in/williamhgates?miniProfileUrn=123",
                    "text": "Bill Gates",
                    "heading": "Featured",
                },
                {
                    "href": "https://www.linkedin.com/company/gates-foundation/posts/",
                    "text": "Gates Foundation",
                    "heading": "Experience",
                },
                {
                    "href": "https://www.linkedin.com/pulse/phone-call-saves-lives-bill-gates-yspvc?trackingId=123",
                    "text": "A phone call that saves lives",
                },
            ],
            "main_profile",
        )

        assert references == [
            {
                "kind": "person",
                "url": "/in/williamhgates/",
                "text": "Bill Gates",
                "context": "featured",
            },
            {
                "kind": "company",
                "url": "/company/gates-foundation/",
                "text": "Gates Foundation",
                "context": "experience",
            },
            {
                "kind": "article",
                "url": "/pulse/phone-call-saves-lives-bill-gates-yspvc",
                "text": "A phone call that saves lives",
                "context": "top card",
            },
        ]

    def test_unwraps_redirect_and_drops_junk(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/redir/redirect/?url=https%3A%2F%2Fgatesnot.es%2Ftgn&urlhash=abc",
                    "text": "Gates Notes",
                },
                {
                    "href": "blob:https://www.linkedin.com/123",
                    "text": "Video",
                },
                {
                    "href": "#caret-small",
                    "text": "",
                },
                {
                    "href": "https://www.linkedin.com/help/linkedin/",
                    "text": "Questions?",
                },
            ],
            "posts",
        )

        assert references == [
            {
                "kind": "external",
                "url": "https://gatesnot.es/tgn",
                "text": "Gates Notes",
                "context": "post attachment",
            }
        ]

    def test_prefers_cleaner_duplicate_label(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/newsletters/gates-notes-123/",
                    "text": "View my newsletter",
                    "aria_label": "Gates Notes",
                },
                {
                    "href": "https://www.linkedin.com/newsletters/gates-notes-123/",
                    "text": "Gates Notes Gates Notes",
                },
            ],
            "posts",
        )

        assert references == [
            {
                "kind": "newsletter",
                "url": "/newsletters/gates-notes-123/",
                "text": "Gates Notes",
                "context": "post attachment",
            }
        ]

    def test_prefers_shorter_clean_label_over_merged_visible_text(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/pulse/test-post?trackingId=123",
                    "text": "Gates Notes Gates Notes A phone call that saves lives Bill Gates",
                    "aria_label": "Open article: A phone call that saves lives by Bill Gates • 3 min read",
                }
            ],
            "posts",
        )

        assert references == [
            {
                "kind": "article",
                "url": "/pulse/test-post",
                "text": "A phone call that saves lives",
                "context": "post attachment",
            }
        ]

    def test_deprioritizes_single_character_labels(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/in/williamhgates/",
                    "text": "1",
                    "aria_label": "Bill Gates",
                }
            ],
            "main_profile",
        )

        assert references == [
            {
                "kind": "person",
                "url": "/in/williamhgates/",
                "text": "Bill Gates",
                "context": "top card",
            }
        ]

    def test_drops_social_proof_company_labels(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/company/gates-foundation/",
                    "text": "Falguni & 8 other connections follow this page",
                },
                {
                    "href": "https://www.linkedin.com/company/gates-foundation/",
                    "text": "Gates Foundation",
                },
            ],
            "about",
        )

        assert references == [
            {
                "kind": "company",
                "url": "/company/gates-foundation/",
                "text": "Gates Foundation",
                "context": "top card",
            }
        ]

    def test_caps_results_per_section(self):
        raw: list[RawReference] = [
            {
                "href": f"https://www.linkedin.com/company/test-{idx}/",
                "text": f"Company {idx}",
            }
            for idx in range(20)
        ]

        references = build_references(raw, "about")

        assert len(references) == 12
        assert references[0]["url"] == "/company/test-0/"
        assert references[-1]["url"] == "/company/test-11/"

    def test_uses_search_result_contexts(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/jobs/view/12345/",
                    "text": "Senior Engineer",
                },
                {
                    "href": "https://www.linkedin.com/in/stickerdaniel/",
                    "text": "Daniel Sticker",
                },
            ],
            "search_results",
        )

        assert references == [
            {
                "kind": "job",
                "url": "/jobs/view/12345/",
                "text": "Senior Engineer",
                "context": "job result",
            },
            {
                "kind": "person",
                "url": "/in/stickerdaniel/",
                "text": "Daniel Sticker",
                "context": "search result",
            },
        ]

    def test_does_not_treat_lookalike_domains_as_linkedin(self):
        references = build_references(
            [
                {
                    "href": "https://www.notlinkedin.com/company/fake/about/",
                    "text": "Fake Company",
                }
            ],
            "about",
        )

        assert references == [
            {
                "kind": "external",
                "url": "https://www.notlinkedin.com/company/fake/about/",
                "text": "Fake Company",
                "context": "top card",
            }
        ]

    def test_keeps_company_about_routes(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/company/legalzoom/about/",
                    "text": "LegalZoom",
                }
            ],
            "about",
        )

        assert references == [
            {
                "kind": "company",
                "url": "/company/legalzoom/",
                "text": "LegalZoom",
                "context": "top card",
            }
        ]

    def test_cross_page_dedupe_keeps_better_reference(self):
        references = dedupe_references(
            [
                {
                    "kind": "job",
                    "url": "/jobs/view/123/",
                    "text": "Job",
                },
                {
                    "kind": "job",
                    "url": "/jobs/view/123/",
                    "text": "Senior Software Engineer",
                    "context": "job result",
                },
            ]
        )

        assert references == [
            {
                "kind": "job",
                "url": "/jobs/view/123/",
                "text": "Senior Software Engineer",
                "context": "job result",
            }
        ]
