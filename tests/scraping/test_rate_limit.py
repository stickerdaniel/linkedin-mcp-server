"""Tests for the per-scrape rate-limit budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock

import logging

import pytest

from linkedin_mcp_server.scraping import rate_limit as rate_limit_module
from linkedin_mcp_server.scraping.rate_limit import (
    RATE_LIMIT_BACKOFF_DELAY,
    RATE_LIMIT_BACKOFF_MAX,
    RATE_LIMIT_BACKOFF_MAX_DOUBLINGS,
    RATE_LIMIT_RETRY_BUDGET,
    RATE_LIMIT_RETRY_DELAY,
    RETRY_AFTER_CEILING,
    RateLimitBudget,
    rate_limit_backoff_delay,
    rate_limit_backoff_max,
    rate_limit_backoff_max_doublings,
    rate_limit_retry_budget,
    rate_limit_retry_delay,
    retry_after_ceiling,
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

    def test_retry_after_ceiling_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("RETRY_AFTER_CEILING_SECONDS", "60")
        assert retry_after_ceiling() == 60
        assert retry_after_seconds("120") == 60
        assert retry_after_seconds("30") == 30


class TestEnvironmentReaders:
    """Each pacing constant is a default an environment variable replaces.

    Read at call time, so `monkeypatch.setenv` inside the test is enough; no
    module reload. One test per variable, each asserting a value the default
    cannot produce.
    """

    def test_defaults_stand_when_nothing_is_set(self, monkeypatch):
        for key in (
            "RATE_LIMIT_RETRY_DELAY_SECONDS",
            "RATE_LIMIT_RETRY_BUDGET",
            "RATE_LIMIT_BACKOFF_DELAY_SECONDS",
            "RATE_LIMIT_BACKOFF_MAX_SECONDS",
            "RATE_LIMIT_BACKOFF_MAX_DOUBLINGS",
            "RETRY_AFTER_CEILING_SECONDS",
        ):
            monkeypatch.delenv(key, raising=False)

        assert rate_limit_retry_delay() == RATE_LIMIT_RETRY_DELAY
        assert rate_limit_retry_budget() == RATE_LIMIT_RETRY_BUDGET
        assert rate_limit_backoff_delay() == RATE_LIMIT_BACKOFF_DELAY
        assert rate_limit_backoff_max() == RATE_LIMIT_BACKOFF_MAX
        assert rate_limit_backoff_max_doublings() == RATE_LIMIT_BACKOFF_MAX_DOUBLINGS
        assert retry_after_ceiling() == RETRY_AFTER_CEILING

    def test_retry_delay_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_RETRY_DELAY_SECONDS", "0.25")
        assert rate_limit_retry_delay() == 0.25

    def test_retry_budget_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_RETRY_BUDGET", "0")
        assert rate_limit_retry_budget() == 0

    def test_backoff_delay_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_BACKOFF_DELAY_SECONDS", "0.5")
        assert rate_limit_backoff_delay() == 0.5

    def test_backoff_max_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_BACKOFF_MAX_SECONDS", "1")
        assert rate_limit_backoff_max() == 1.0

    def test_backoff_max_doublings_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("RATE_LIMIT_BACKOFF_MAX_DOUBLINGS", "0")
        assert rate_limit_backoff_max_doublings() == 0

    def test_garbage_falls_back_to_the_default_with_a_warning(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("RATE_LIMIT_BACKOFF_DELAY_SECONDS", "soon")
        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.limits"):
            assert rate_limit_backoff_delay() == RATE_LIMIT_BACKOFF_DELAY

        assert any(
            "RATE_LIMIT_BACKOFF_DELAY_SECONDS" in r.getMessage()
            and "'soon'" in r.getMessage()
            for r in caplog.records
        )


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

    async def test_retry_budget_zero_means_no_soft_retry(self, monkeypatch, caplog):
        monkeypatch.setenv("RATE_LIMIT_RETRY_BUDGET", "0")
        budget = RateLimitBudget()
        sleep = AsyncMock()

        with caplog.at_level(logging.WARNING, logger=rate_limit_module.__name__):
            granted = await budget.claim_soft_retry(
                "https://www.linkedin.com/in/testuser/details/experience/",
                sleep=sleep,
            )

        assert granted is False
        sleep.assert_not_awaited()
        assert budget.soft_retries_used == 0
        assert any(
            "budget (0) spent" in r.getMessage()
            and "/details/experience/" in r.getMessage()
            for r in caplog.records
        )

    async def test_retry_delay_from_the_environment_reaches_the_sleep(
        self, monkeypatch
    ):
        monkeypatch.setenv("RATE_LIMIT_RETRY_DELAY_SECONDS", "0.25")
        budget = RateLimitBudget()
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        await budget.claim_soft_retry(
            "https://www.linkedin.com/in/testuser/", sleep=record
        )

        assert slept == [0.25]
        assert RATE_LIMIT_RETRY_DELAY not in slept

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


@pytest.mark.parametrize(
    "budget_size, expected",
    [
        ("1", [True, False]),
        ("3", [True, True, True]),
    ],
)
async def test_the_environment_sizes_the_budget(monkeypatch, budget_size, expected):
    monkeypatch.setenv("RATE_LIMIT_RETRY_BUDGET", budget_size)
    budget = RateLimitBudget()
    sleep = AsyncMock()

    granted = [
        await budget.claim_soft_retry("https://www.linkedin.com/in/x/", sleep=sleep)
        for _ in expected
    ]

    assert granted == expected
