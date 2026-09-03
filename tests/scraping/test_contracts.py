"""Tests for the section contracts every scraping workflow returns."""

from linkedin_mcp_server.scraping.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
    FilterValidationError,
    rate_limited_section_error,
)


class TestRateLimitedSection:
    def test_the_sentinel_text_is_what_reaches_the_client(self):
        # Pinned as a literal on purpose. Every other assertion in the suite
        # compares a result against this same constant, so it moves with any
        # edit and none of them can see the message a client would read.
        assert RATE_LIMITED_SECTION_TEXT == (
            "[Rate limited] LinkedIn blocked this section. "
            "Try again later or request fewer sections."
        )

    def test_the_reported_error_repeats_the_sentinel_verbatim(self):
        # The tools compare a section's text against the sentinel and then
        # report this error, so the two drifting apart would describe a
        # section the caller never saw.
        assert rate_limited_section_error() == {
            "error_type": "rate_limit",
            "error_message": RATE_LIMITED_SECTION_TEXT,
        }


class TestExtractedSection:
    def test_a_section_without_an_error_carries_none(self):
        section = ExtractedSection(text="Bill Gates", references=[])

        assert section.error is None

    def test_an_error_is_kept_beside_the_text(self):
        section = ExtractedSection(
            text="", references=[], error=rate_limited_section_error()
        )

        assert section.text == ""
        assert section.error == rate_limited_section_error()


class TestFilterValidationError:
    def test_it_is_still_a_value_error(self):
        # Direct extractor callers catch ValueError; the tool wrappers catch
        # this subclass to surface the message past mask_error_details.
        assert issubclass(FilterValidationError, ValueError)
