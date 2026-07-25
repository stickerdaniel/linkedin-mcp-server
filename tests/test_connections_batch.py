"""Tests for scrape_contact_batch chunking and inter-chunk delay control flow.

These exercise the batching logic deterministically with no network traffic:
the per-profile network calls are stubbed, and asyncio.sleep is patched to
record requested durations instead of waiting. This isolates chunk boundaries,
inter-chunk pausing, progress reporting, and rate-limit handling.
"""

from unittest.mock import MagicMock, patch

import pytest

from linkedin_mcp_server.core.exceptions import RateLimitError
from linkedin_mcp_server.scraping import extractor as extractor_mod
from linkedin_mcp_server.scraping.extractor import (
    _NAV_DELAY,
    _RATE_LIMITED_MSG,
    ExtractedSection,
    LinkedInExtractor,
    _parse_contact_record,
)


async def _run_batch(
    total,
    chunk_size,
    chunk_delay,
    *,
    rate_limit_at=None,
    soft_at=(),
):
    """Run scrape_contact_batch with stubbed network and recorded sleeps.

    Returns (result, chunk_sleeps, nav_sleeps, progress) where *_sleeps are the
    recorded durations classified by value and progress is the callback log.
    """
    ext = LinkedInExtractor(page=MagicMock())
    usernames = [f"user{i:03d}" for i in range(total)]

    # Signatures mirror the real methods (url, section_name) so a regression
    # back to a missing-argument call would fail this test rather than hide.
    async def fake_extract_page(url, section_name):
        uname = url.rstrip("/").split("/")[-1]
        if rate_limit_at is not None and uname == f"user{rate_limit_at:03d}":
            raise RateLimitError("simulated hard rate limit")
        if uname in soft_at:
            return ExtractedSection(text=_RATE_LIMITED_MSG, references=[])
        return ExtractedSection(
            text=f"First {uname}\nLast\n2nd degree\nHeadline\nLocation",
            references=[],
        )

    async def fake_overlay(url, section_name):
        return ExtractedSection(text="Email\nuser@example.com", references=[])

    sleeps = []

    async def recording_sleep(duration, *args, **kwargs):
        sleeps.append(duration)

    progress = []

    async def progress_cb(done, tot):
        progress.append((done, tot))

    with (
        patch.object(ext, "extract_page", side_effect=fake_extract_page),
        patch.object(ext, "_extract_overlay", side_effect=fake_overlay),
        patch.object(extractor_mod.asyncio, "sleep", recording_sleep),
    ):
        result = await ext.scrape_contact_batch(
            usernames,
            chunk_size=chunk_size,
            chunk_delay=chunk_delay,
            progress_cb=progress_cb,
        )

    chunk_sleeps = [d for d in sleeps if d == chunk_delay]
    nav_sleeps = [d for d in sleeps if d == _NAV_DELAY]
    return result, chunk_sleeps, nav_sleeps, progress


class TestContactBatchChunking:
    async def test_ragged_final_chunk(self):
        """53 usernames / chunk 5 -> 11 chunks, 10 inter-chunk delays, none after last."""
        result, chunk_sleeps, nav_sleeps, progress = await _run_batch(53, 5, 30.0)

        assert result["total"] == 53
        assert result["failed"] == []
        assert result["rate_limited"] is False
        assert len(result["pages_visited"]) == 106  # 2 navigations per profile
        # 11 chunks => exactly 10 inter-chunk pauses (no pause after the last chunk)
        assert len(chunk_sleeps) == 10
        assert all(d == 30.0 for d in chunk_sleeps)
        assert len(nav_sleeps) == 53
        assert progress == [
            (c, 53) for c in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 53)
        ]

    async def test_exact_multiple(self):
        """50 / chunk 5 -> 10 chunks, 9 inter-chunk delays."""
        result, chunk_sleeps, _, progress = await _run_batch(50, 5, 30.0)

        assert result["total"] == 50
        assert len(chunk_sleeps) == 9
        assert progress[-1] == (50, 50)

    async def test_hard_rate_limit_stops_early(self):
        """A RateLimitError aborts the whole batch immediately."""
        result, chunk_sleeps, _, progress = await _run_batch(
            53, 5, 30.0, rate_limit_at=12
        )

        assert result["rate_limited"] is True
        assert "user012" in result["failed"]
        assert result["total"] == 12  # user000..user011 enriched before the abort
        # Only the first two chunk boundaries are reached before stopping.
        assert len(chunk_sleeps) == 2
        assert progress == [(5, 53), (10, 53)]

    async def test_soft_rate_limit_is_skipped_not_aborted(self):
        """Empty-content (soft) rate limit marks the profile failed and continues."""
        result, chunk_sleeps, _, _ = await _run_batch(
            12, 5, 30.0, soft_at={"user003", "user009"}
        )

        assert result["rate_limited"] is False
        assert set(result["failed"]) == {"user003", "user009"}
        assert result["total"] == 10  # 12 attempted, 2 soft-skipped

    async def test_single_chunk_has_no_delay(self):
        """Fewer than chunk_size usernames -> one chunk, zero inter-chunk delays."""
        result, chunk_sleeps, _, progress = await _run_batch(3, 5, 30.0)

        assert result["total"] == 3
        assert len(chunk_sleeps) == 0
        assert progress == [(3, 3)]

    async def test_invalid_chunk_size_raises(self):
        ext = LinkedInExtractor(page=MagicMock())
        with pytest.raises(ValueError, match="chunk_size must be a positive integer"):
            await ext.scrape_contact_batch(["a", "b"], chunk_size=0)


# Cleaned innerText captured from live profiles (2026-06-17). The leading
# "<First> has a {:badgeType} account" and stray "test" lines are real noise
# the parser must skip; the location line carries a "Contact info" suffix.
_PROFILE_FULL = (
    "Florian has a {:badgeType} account\n"
    "test\n"
    "Florian Farkas \n"
    "  \n"
    "1st degree connection\n"
    "1st\n"
    "Documentary Content | Founder | Berlin\n"
    "FFmediaagency\n"
    "SRH Berlin School of Design and Communication\n"
    "Berlin, Berlin, Germany  Contact info\n"
    "16 connections\n"
)
_OVERLAY_FULL = (
    "Contact Info\n"
    "Florian’s Profile\n"
    "linkedin.com/in/florian-farkas\n"
    "Website\n"
    "ffmediaagency.de/ (Company)\n"
    "Email\n"
    "florian@ffmediaagency.de\n"
    "Connected\n"
    "Jun 17, 2026\n"
)


class TestParseContactRecord:
    def test_full_profile_and_overlay(self):
        r = _parse_contact_record(_PROFILE_FULL, _OVERLAY_FULL)
        assert r["first_name"] == "Florian"
        assert r["last_name"] == "Farkas"
        assert r["headline"] == "Documentary Content | Founder | Berlin"
        assert r["company"] == "FFmediaagency"
        assert r["location"] == "Berlin, Berlin, Germany"
        assert r["email"] == "florian@ffmediaagency.de"
        assert r["website"] == "ffmediaagency.de/"  # type annotation stripped

    def test_overlay_with_birthday_no_email(self):
        overlay = "Contact Info\nFabiola’s Profile\nBirthday\nJune 2\nConnected\nJun 16, 2026\n"
        r = _parse_contact_record("", overlay)
        assert r["birthday"] == "June 2"
        assert r["email"] is None

    def test_email_regex_fallback_without_label(self):
        # No "Email" label line, but an address is present in the blob.
        overlay = (
            "Contact Info\nName’s Profile\nlinkedin.com/in/x\nsomeone@example.com\n"
        )
        r = _parse_contact_record("", overlay)
        assert r["email"] == "someone@example.com"

    def test_missing_degree_marker_still_finds_name(self):
        r = _parse_contact_record("Jane Smith\nSome headline\n", "")
        assert r["first_name"] == "Jane"
        assert r["last_name"] == "Smith"

    def test_single_word_name_has_no_last_name(self):
        r = _parse_contact_record("Cher\n1st degree connection\nSinger\n", "")
        assert r["first_name"] == "Cher"
        assert r["last_name"] is None
