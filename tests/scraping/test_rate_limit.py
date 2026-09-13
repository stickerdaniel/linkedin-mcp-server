"""Tests for the per-scrape rate-limit budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock

from linkedin_mcp_server.scraping.rate_limit import (
    RATE_LIMIT_RETRY_BUDGET,
    RATE_LIMIT_RETRY_DELAY,
    RETRY_AFTER_CEILING,
    RateLimitBudget,
    retry_after_seconds,
)


class TestRetryAfter:
    def test_retry_after_reads_seconds_and_http_date(self):
        assert retry_after_seconds("120") == 120
        soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))
        parsed = retry_after_seconds(soon)
        assert parsed is not None and 80 <= parsed <= 90
        # Nothing is invented when the header is absent or unreadable.
        assert retry_after_seconds(None) is None
        assert retry_after_seconds("") is None
        assert retry_after_seconds("whenever") is None

    def test_a_date_already_passed_reads_as_zero_not_negative(self):
        gone = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=90))
        assert retry_after_seconds(gone) == 0

    def test_retry_after_is_capped_and_unicode_digits_do_not_crash(self):
        """Both halves failed as something other than a rate-limit report.

        `"²".isdigit()` is True while `int("²")` raises, so a header
        carrying one replaced the report with an unrelated traceback; and an
        uncapped day-long wait relayed verbatim reads as the server hanging.
        """
        assert retry_after_seconds("²") is None
        assert retry_after_seconds("86400") == RETRY_AFTER_CEILING
        far = format_datetime(datetime.now(timezone.utc) + timedelta(days=2))
        assert retry_after_seconds(far) == RETRY_AFTER_CEILING


class TestRateLimitBudget:
    """One budget per scrape, shared by every section in it."""

    def test_a_fresh_budget_has_spent_nothing(self):
        budget = RateLimitBudget()
        assert budget.soft_retries_used == 0
        assert budget.rate_limit_hits == 0

    async def test_the_budget_is_spent_across_claims_not_per_claim(self):
        """Four throttled sections may spend the two retries and no more."""
        budget = RateLimitBudget()
        sleep = AsyncMock()

        granted = [
            await budget.claim_soft_retry(
                f"https://www.linkedin.com/in/testuser/details/{section}/",
                sleep=sleep,
            )
            for section in ("experience", "education", "skills", "projects")
        ]

        assert granted == [True] * RATE_LIMIT_RETRY_BUDGET + [False] * (
            4 - RATE_LIMIT_RETRY_BUDGET
        )
        assert budget.soft_retries_used == RATE_LIMIT_RETRY_BUDGET
        assert sleep.await_count == RATE_LIMIT_RETRY_BUDGET

    async def test_delay_escalates_within_one_scrape(self):
        """The second retry of a scrape waits twice as long as the first."""
        budget = RateLimitBudget()
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        for _ in range(RATE_LIMIT_RETRY_BUDGET):
            assert await budget.claim_soft_retry(
                "https://www.linkedin.com/in/testuser/", sleep=record
            )

        assert slept == [RATE_LIMIT_RETRY_DELAY, RATE_LIMIT_RETRY_DELAY * 2]

    async def test_a_claim_is_paced_before_it_is_granted(self):
        """The sleep is the backoff; a claim that returned first would let the
        caller re-navigate into the limit and pay for it afterwards."""
        budget = RateLimitBudget()
        order: list[str] = []

        async def record(seconds: float) -> None:
            order.append("slept")

        granted = await budget.claim_soft_retry(
            "https://www.linkedin.com/in/testuser/", sleep=record
        )
        order.append(f"granted={granted}")

        assert order == ["slept", "granted=True"]
