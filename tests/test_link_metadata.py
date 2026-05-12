"""Tests for compact LinkedIn reference extraction helpers."""

from urllib.parse import quote

from linkedin_mcp_server.scraping.link_metadata import (
    RawReference,
    build_references,
    classify_link,
    dedupe_references,
    normalize_url,
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
                "url": "/pulse/phone-call-saves-lives-bill-gates-yspvc/",
                "text": "A phone call that saves lives",
                "context": "top card",
            },
        ]

    def test_preserves_person_slug_named_details(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/in/details/",
                    "text": "Details Person",
                }
            ],
            "main_profile",
        )

        assert references == [
            {
                "kind": "person",
                "url": "/in/details/",
                "text": "Details Person",
                "context": "top card",
            }
        ]

    def test_drops_person_details_subpage(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/in/williamhgates/details/experience/",
                    "text": "Bill Gates",
                }
            ],
            "main_profile",
        )

        assert references == []

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

    def test_drops_non_http_external_schemes(self):
        references = build_references(
            [
                {
                    "href": "data:text/html,<p>hello</p>",
                    "text": "Inline payload",
                },
                {
                    "href": "ftp://example.com/report.csv",
                    "text": "FTP report",
                },
                {
                    "href": "https://example.com/report.csv",
                    "text": "HTTPS report",
                },
            ],
            "posts",
        )

        assert references == [
            {
                "kind": "external",
                "url": "https://example.com/report.csv",
                "text": "HTTPS report",
                "context": "post attachment",
            }
        ]

    def test_dedupes_external_tracking_variants(self):
        references = build_references(
            [
                {
                    "href": "https://example.com/report?utm_source=linkedin",
                    "text": "Report",
                },
                {
                    "href": "https://example.com/report?utm_source=share",
                    "text": "Detailed annual report",
                },
            ],
            "posts",
        )

        assert references == [
            {
                "kind": "external",
                "url": "https://example.com/report",
                "text": "Detailed annual report",
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

    def test_normalize_url_unwraps_nested_redirects_within_cap(self):
        target = "https://example.com/report"
        nested = "https://www.linkedin.com/redir/redirect/?url=" + quote(
            "https://www.linkedin.com/redir/redirect/?url=" + quote(target, safe=""),
            safe="",
        )

        assert normalize_url(nested) == target

    def test_normalize_url_drops_redirect_chain_beyond_cap(self):
        target = "https://example.com/report"
        href = target
        for _ in range(7):
            href = "https://www.linkedin.com/redir/redirect/?url=" + quote(
                href, safe=""
            )

        assert normalize_url(href) is None

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
                "url": "/pulse/test-post/",
                "text": "A phone call that saves lives",
                "context": "post attachment",
            }
        ]

    def test_rejects_single_character_labels(self):
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

    def test_preserves_words_starting_with_view(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/company/viewpoint-economics/",
                    "text": "Viewpoint Economics",
                }
            ],
            "about",
        )

        assert references == [
            {
                "kind": "company",
                "url": "/company/viewpoint-economics/",
                "text": "Viewpoint Economics",
                "context": "top card",
            }
        ]

    def test_prefers_company_post_context_for_feed_posts(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/feed/update/urn:li:activity:123/",
                    "text": "Original company post",
                    "in_article": True,
                }
            ],
            "posts",
        )

        assert references == [
            {
                "kind": "feed_post",
                "url": "/feed/update/urn:li:activity:123/",
                "text": "Original company post",
                "context": "company post",
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

    def test_drops_nav_and_footer_anchors(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/in/williamhgates/",
                    "text": "Bill Gates",
                    "in_nav": True,
                },
                {
                    "href": "https://www.linkedin.com/company/gates-foundation/",
                    "text": "Gates Foundation",
                    "in_footer": True,
                },
            ],
            "main_profile",
        )

        assert references == []

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

    def test_caps_jobs_section_more_tightly(self):
        raw: list[RawReference] = [
            {
                "href": f"https://www.linkedin.com/jobs/view/{idx}/",
                "text": f"Job {idx}",
            }
            for idx in range(20)
        ]

        references = build_references(raw, "jobs")

        assert len(references) == 8
        assert references[0]["url"] == "/jobs/view/0/"
        assert references[-1]["url"] == "/jobs/view/7/"

    def test_uses_default_cap_for_unknown_section(self):
        raw: list[RawReference] = [
            {
                "href": f"https://www.linkedin.com/company/test-{idx}/",
                "text": f"Company {idx}",
            }
            for idx in range(20)
        ]

        references = build_references(raw, "unknown_section")

        assert len(references) == 12

    def test_prefers_richer_duplicate_text(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/jobs/view/12345/",
                    "text": "Job",
                },
                {
                    "href": "https://www.linkedin.com/jobs/view/12345/",
                    "text": "Senior Software Engineer",
                },
            ],
            "search_results",
        )

        assert references == [
            {
                "kind": "job",
                "url": "/jobs/view/12345/",
                "text": "Senior Software Engineer",
                "context": "job result",
            }
        ]

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

    def test_uses_job_posting_context_for_job_pages(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/company/acme/",
                    "text": "Acme",
                }
            ],
            "job_posting",
        )

        assert references == [
            {
                "kind": "company",
                "url": "/company/acme/",
                "text": "Acme",
                "context": "job posting",
            }
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

    def test_company_urn_single_id_anchor(self):
        """Anthropic-style: single id in the currentCompany list."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5B%2274126343%22%5D"
                    "&origin=COMPANY_PAGE_CANNED_SEARCH",
                    "text": "501-1K employees",
                }
            ],
            "about",
        )

        assert references == [
            {
                "kind": "company_urn",
                "url": "/search/results/people/?currentCompany=%5B%2274126343%22%5D",
                "value": "74126343",
                "context": "top card",
            }
        ]

    def test_company_urn_multi_id_anchor_uses_first_id(self):
        """SAP-style: parent + subsidiaries; the first id is the parent company."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5B%221115%22%2C%222573558%22%2C%222818%22%5D"
                    "&origin=COMPANY_PAGE_CANNED_SEARCH",
                    "text": "143,150 associated members",
                }
            ],
            "about",
        )

        assert references == [
            {
                "kind": "company_urn",
                "url": "/search/results/people/?currentCompany=%5B%221115%22%5D",
                "value": "1115",
                "context": "top card",
            }
        ]

    def test_company_urn_suppresses_anchor_text(self):
        """Anchor text like '10K+ employees' is not user-meaningful for a URN
        reference; callers should key off ``value``."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5B%221115%22%5D",
                    "text": "10K+ employees",
                }
            ],
            "about",
        )

        assert len(references) == 1
        assert references[0]["kind"] == "company_urn"
        assert references[0]["value"] == "1115"
        assert "text" not in references[0]

    def test_company_urn_accepts_unquoted_json_integers(self):
        """Defensive: LinkedIn currently serialises ids as quoted strings,
        but plain JSON integers are also valid and should still classify."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5B1115%5D",
                    "text": "10K+ employees",
                }
            ],
            "about",
        )

        assert len(references) == 1
        assert references[0]["kind"] == "company_urn"
        assert references[0]["value"] == "1115"

    def test_company_urn_lowercase_percent_escapes(self):
        """``parse_qs`` decodes percent-escapes regardless of case, so
        lowercase variants must still classify and extract the same id."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?currentCompany=%5b%221115%22%5d",
                    "text": "10K+ employees",
                }
            ],
            "about",
        )

        assert len(references) == 1
        assert references[0]["kind"] == "company_urn"
        assert references[0]["value"] == "1115"

    def test_plain_people_search_still_dropped(self):
        """A people-search href without ``currentCompany`` is page chrome
        and stays excluded — preserves existing behaviour."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/search/results/people/"
                    "?keywords=engineer",
                    "text": "engineer",
                }
            ],
            "about",
        )

        assert references == []


class TestClassifyLink:
    def test_messaging_thread_url(self):
        result = classify_link(
            "https://www.linkedin.com/messaging/thread/2-NjAwMDAyMDEtZWVh/"
        )
        assert result == (
            "conversation",
            "/messaging/thread/2-NjAwMDAyMDEtZWVh/",
        )

    def test_messaging_thread_url_with_query(self):
        result = classify_link(
            "https://www.linkedin.com/messaging/thread/2-abc123/?focusedMsgUrn=xyz"
        )
        assert result == ("conversation", "/messaging/thread/2-abc123/")

    def test_inbox_references_include_threads(self):
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/messaging/thread/2-abc123/",
                    "text": "Tony Chan",
                },
                {
                    "href": "https://www.linkedin.com/messaging/thread/2-def456/",
                    "text": "Paul Jasper",
                },
            ],
            "inbox",
        )
        assert len(references) == 2
        assert references[0]["kind"] == "conversation"
        assert references[0]["url"] == "/messaging/thread/2-abc123/"
        assert references[0]["text"] == "Tony Chan"
        assert references[0]["context"] == "inbox"
        assert references[1]["kind"] == "conversation"
        assert references[1]["url"] == "/messaging/thread/2-def456/"
        assert references[1]["text"] == "Paul Jasper"

    def test_inbox_conversation_without_text_still_captured(self):
        """Conversation references are kept even without a usable text label."""
        references = build_references(
            [
                {
                    "href": "https://www.linkedin.com/messaging/thread/2-xyz/",
                    "text": "",
                },
            ],
            "inbox",
        )
        assert len(references) == 1
        assert references[0]["kind"] == "conversation"
        assert references[0]["url"] == "/messaging/thread/2-xyz/"
