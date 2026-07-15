"""Adaptive rate limiting and circuit breaking for write-path LinkedIn actions.

Gates ``send_message``/``connect_with_person`` (the highest ban-risk tools)
with a per-action-type token bucket plus a circuit breaker that opens after
repeated real failures and forces a cooldown. Only genuine outcomes should
be fed to :meth:`ActionRateLimiter.record_result` -- a transport failure
already classified by ``drivers/browser.py::_is_transport_failure`` (raised
as ``NetworkError``) says nothing about whether the action itself is
failing, so callers must not record it as a circuit-breaker failure; treat
it as inconclusive and let the exception propagate on its own.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class RateLimitExceededError(Exception):
    """Raised when an action's token bucket has no tokens available."""


class CircuitOpenError(Exception):
    """Raised when an action's circuit breaker is open and rejecting calls."""


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class TokenBucket:
    """Classic token bucket: a burst capacity that refills continuously."""

    capacity: float
    refill_rate_per_second: float
    tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(
            self.capacity, self.tokens + elapsed * self.refill_rate_per_second
        )
        self._last_refill = now

    def try_consume(self, amount: float = 1.0) -> bool:
        self._refill()
        if self.tokens < amount:
            return False
        self.tokens -= amount
        return True


@dataclass
class CircuitBreaker:
    """CLOSED -> OPEN after N consecutive failures -> HALF_OPEN after cooldown.

    HALF_OPEN allows exactly the next call through; a success closes the
    circuit, a failure reopens it (resets the cooldown).
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 300.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def before_call(self) -> None:
        if self.state == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit breaker is open after {self._consecutive_failures} "
                f"consecutive failures; retry after the {self.cooldown_seconds:.0f}s "
                "cooldown."
            )

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()


# Conservative starting defaults for LinkedIn's highest ban-risk actions:
# a small burst allowance, slow refill. core/opsec.py handles the coarser
# daily-volume cap; this module paces short-term burst/retry behavior and
# stops entirely after repeated real failures. Tune based on observed usage.
_DEFAULT_CAPACITY = 3.0
_DEFAULT_REFILL_PER_SECOND = 1.0 / 120.0  # one token every 2 minutes
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 300.0


class ActionRateLimiter:
    """Per-action-type token bucket + circuit breaker, keyed by action name."""

    def __init__(
        self,
        *,
        capacity: float = _DEFAULT_CAPACITY,
        refill_rate_per_second: float = _DEFAULT_REFILL_PER_SECOND,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._capacity = capacity
        self._refill_rate_per_second = refill_rate_per_second
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def _bucket_for(
        self,
        action: str,
        *,
        capacity: float | None = None,
        refill_rate_per_second: float | None = None,
    ) -> TokenBucket:
        bucket = self._buckets.get(action)
        if bucket is None:
            # Per-action override only applies at first creation -- an
            # action's bucket, once created, keeps its own capacity/refill
            # rate for the life of the process. This lets a caller (e.g. a
            # stealth-profile-derived rate for read-path actions) diverge
            # from the shared instance defaults without needing a second
            # ActionRateLimiter instance/singleton.
            bucket = TokenBucket(
                capacity if capacity is not None else self._capacity,
                refill_rate_per_second
                if refill_rate_per_second is not None
                else self._refill_rate_per_second,
            )
            self._buckets[action] = bucket
        return bucket

    def _breaker_for(self, action: str) -> CircuitBreaker:
        breaker = self._breakers.get(action)
        if breaker is None:
            breaker = CircuitBreaker(self._failure_threshold, self._cooldown_seconds)
            self._breakers[action] = breaker
        return breaker

    def check(
        self,
        action: str,
        *,
        capacity: float | None = None,
        refill_rate_per_second: float | None = None,
    ) -> None:
        """Raise if *action* cannot proceed right now.

        Raises :class:`CircuitOpenError` if the breaker is open, or
        :class:`RateLimitExceededError` if the bucket has no tokens.
        Call once immediately before attempting the real write.

        ``capacity``/``refill_rate_per_second`` override this limiter's
        instance defaults for *action*'s bucket, but only take effect the
        first time this action is seen -- see ``_bucket_for``.
        """
        self._breaker_for(action).before_call()
        if not self._bucket_for(
            action, capacity=capacity, refill_rate_per_second=refill_rate_per_second
        ).try_consume():
            raise RateLimitExceededError(
                f"Rate limit exceeded for '{action}'; wait for the bucket to refill."
            )

    def record_result(self, action: str, *, success: bool) -> None:
        """Feed a genuine outcome to the circuit breaker.

        Only call this for a decisive outcome (the action really succeeded
        or really failed) -- skip it for inconclusive/non-actionable
        results (e.g. "already connected") and never call it for a
        transport failure (see module docstring).
        """
        breaker = self._breaker_for(action)
        if success:
            breaker.record_success()
        else:
            breaker.record_failure()


_default_limiter: ActionRateLimiter | None = None


def get_rate_limiter() -> ActionRateLimiter:
    """Return the process-wide rate limiter singleton."""
    global _default_limiter
    if _default_limiter is None:
        _default_limiter = ActionRateLimiter()
    return _default_limiter


def reset_rate_limiter_for_testing() -> None:
    global _default_limiter
    _default_limiter = None
