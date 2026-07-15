"""Tests for adaptive rate limiting / circuit breaking (core.rate_limit)."""

from unittest.mock import patch

import pytest

from linkedin_mcp_server.core.rate_limit import (
    ActionRateLimiter,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    RateLimitExceededError,
    TokenBucket,
    get_rate_limiter,
    reset_rate_limiter_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_rate_limiter_for_testing()
    yield
    reset_rate_limiter_for_testing()


class TestTokenBucket:
    def test_starts_full(self):
        bucket = TokenBucket(capacity=3, refill_rate_per_second=1)
        assert bucket.tokens == 3

    def test_consumes_down_to_zero(self):
        bucket = TokenBucket(capacity=2, refill_rate_per_second=0)
        assert bucket.try_consume() is True
        assert bucket.try_consume() is True
        assert bucket.try_consume() is False

    def test_refills_over_time(self):
        bucket = TokenBucket(capacity=1, refill_rate_per_second=10)
        assert bucket.try_consume() is True
        assert bucket.try_consume() is False

        # Simulate 0.5s elapsed -- at 10 tokens/s that's 5 tokens, clamped
        # to the bucket's capacity of 1, so the next consume should succeed.
        bucket._last_refill -= 0.5
        assert bucket.try_consume() is True


class TestCircuitBreaker:
    def test_starts_closed(self):
        breaker = CircuitBreaker()
        assert breaker.state == CircuitState.CLOSED
        breaker.before_call()  # does not raise

    def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker(failure_threshold=2)
        breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        with pytest.raises(CircuitOpenError):
            breaker.before_call()

    def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(failure_threshold=2)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED  # only 1 consecutive now

    def test_half_opens_after_cooldown(self):
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10)
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        opened_at = breaker._opened_at
        assert opened_at is not None  # set by the record_failure() above
        with patch(
            "linkedin_mcp_server.core.rate_limit.time.monotonic",
            return_value=opened_at + 11,
        ):
            assert breaker.state == CircuitState.HALF_OPEN
            breaker.before_call()  # half-open allows the next call through


class TestActionRateLimiter:
    def test_check_passes_within_capacity(self):
        limiter = ActionRateLimiter(capacity=2, refill_rate_per_second=0)
        limiter.check("send_message")
        limiter.check("send_message")

    def test_check_raises_when_bucket_exhausted(self):
        limiter = ActionRateLimiter(capacity=1, refill_rate_per_second=0)
        limiter.check("send_message")
        with pytest.raises(RateLimitExceededError):
            limiter.check("send_message")

    def test_actions_have_independent_buckets(self):
        limiter = ActionRateLimiter(capacity=1, refill_rate_per_second=0)
        limiter.check("send_message")
        limiter.check("connect_with_person")  # independent bucket, does not raise

    def test_check_raises_when_circuit_open(self):
        limiter = ActionRateLimiter(
            capacity=10, refill_rate_per_second=0, failure_threshold=1
        )
        limiter.record_result("send_message", success=False)
        with pytest.raises(CircuitOpenError):
            limiter.check("send_message")

    def test_record_result_success_does_not_raise(self):
        limiter = ActionRateLimiter(capacity=10, refill_rate_per_second=0)
        limiter.record_result("send_message", success=True)
        limiter.check("send_message")  # still closed


class TestSingleton:
    def test_get_rate_limiter_returns_same_instance(self):
        assert get_rate_limiter() is get_rate_limiter()

    def test_reset_creates_a_fresh_instance(self):
        first = get_rate_limiter()
        reset_rate_limiter_for_testing()
        assert get_rate_limiter() is not first
