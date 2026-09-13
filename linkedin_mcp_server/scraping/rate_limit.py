"""Rate-limit accounting shared by every service in one scrape.

Two kinds of limit arrive at a scraper. A *soft* one is a page that loaded
with its content gone, which the capture path notices after the fact and may
re-fetch; a *hard* one is an HTTP 429 the navigation path sees directly. Both
are budgeted here, per scrape rather than per section, so a scrape already
being throttled stops asking instead of amplifying its own request volume.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import logging
import re

logger = logging.getLogger(__name__)

# Backoff before retrying a temporarily blocked page. Each retry within one
# scrape waits twice as long as the one before it, jittered.
RATE_LIMIT_RETRY_DELAY = 5.0

# How many soft rate-limit retries one scrape may spend in total. A budget
# lives for exactly one tool call, so this is the whole scrape's allowance
# rather than each section's. It used to be one retry *per section*, which
# meant an eight-section scrape that had started to be throttled sent eight
# extra navigations -- doubling its request volume at the moment LinkedIn was
# asking for less. Two keeps the original benefit for a genuine one-off blip
# while capping the amplification at a constant.
RATE_LIMIT_RETRY_BUDGET = 2

# A hard 429 never reaches `detect_rate_limit`, which reads a page that
# loaded. Measured live: LinkedIn answers a throttled navigation with a 429
# that Chromium refuses to commit, so `page.goto` raises
# `net::ERR_HTTP_RESPONSE_CODE_FAILURE` and the tab shows Chromium's own
# "This page isn't working / HTTP ERROR 429" interstitial instead of a
# document. The net error token is the classifier because it is a Chromium
# constant; the interstitial's prose is browser chrome and is translated, so
# matching it would break the locale-independence rule.
#
# The token alone is NOT a 429. Chromium raises it for any response code the
# navigation stack refuses, 404 and 403 and 5xx included, so treating it as a
# rate limit on its own told a user who mistyped a username to wait five
# minutes and skipped the not-found branch in `error_handler` entirely. It is
# therefore only half the signal: the status has to be corroborated off the
# interstitial before this is called a rate limit, and an uncorroborated
# refusal is re-raised as the navigation error it already was.
HTTP_STATUS_NAV_FAILURE = "ERR_HTTP_RESPONSE_CODE_FAILURE"

# The status on Chromium's own error page, as digits. The words around it are
# translated; the number is not, which is the whole reason to match on it
# rather than on "too many requests". Bounded by a word boundary so a 429 in a
# URL or a timestamp elsewhere on the page cannot stand in for the status.
HTTP_STATUS_ON_INTERSTITIAL = re.compile(r"\b429\b")

# The other shape of the same thing, and the reason `page.goto`'s return value
# is no longer discarded: a 429 that Chromium *does* commit comes back as an
# ordinary response. Measured against a local server answering 429, with and
# without a body, under both `wait_until="domcontentloaded"` and `"commit"`:
# `goto` returns rather than raising, `status` is 429 and `Retry-After`
# survives on `headers`. No `wait_until` change is needed to see it.
HTTP_TOO_MANY_REQUESTS = 429

# Pause before a hard rate limit is reported, doubling per hit within one
# scrape and jittered like every other deliberate pause here. Bounded well
# under the tool timeout on purpose: this cannot wait out a real limit, it
# only stops the next tool call from leaving for it immediately. How long to
# actually wait is carried to the client on `RateLimitError.suggested_wait_time`.
RATE_LIMIT_BACKOFF_DELAY = 5.0
RATE_LIMIT_BACKOFF_MAX = 30.0
# Enough doublings to reach the cap from the base delay, and no more.
RATE_LIMIT_BACKOFF_MAX_DOUBLINGS = 8

# The longest `Retry-After` worth repeating to a client. LinkedIn asking for a
# day off is a real answer, but relaying it unchanged makes the tool look hung;
# the cap keeps the report actionable and the server still refuses to scrape.
RETRY_AFTER_CEILING = 3600


def retry_after_seconds(value: str | None) -> int | None:
    """`Retry-After` in whole seconds, or None when absent or unreadable.

    RFC 6585 allows either a delay in seconds or an HTTP-date, and both are
    accepted here. None is returned rather than a guess: nothing downstream may
    invent a wait LinkedIn did not ask for.

    The result is clamped to `RETRY_AFTER_CEILING`. A header is a request, not
    an instruction, and an hour-long one relayed verbatim reads to the client
    as the server having hung. The clamp is on the number reported, never on
    anything slept on -- nothing here sleeps for `Retry-After`.

    `isascii()` guards the `isdigit()`: superscripts and other Unicode digits
    answer True to `isdigit()` and then raise inside `int()`, which on this
    path would replace a rate-limit report with an unrelated traceback.
    """
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(RETRY_AFTER_CEILING, int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    return min(RETRY_AFTER_CEILING, max(0, seconds))


class RateLimitBudget:
    """Rate-limit accounting for one scrape.

    One budget is built per tool call and shared by every service that
    navigates on its behalf, so both counters span the whole scrape and every
    section in it, which is the point: a per-section budget is what let a
    throttled scrape double its own request volume.
    """

    __slots__ = ("soft_retries_used", "rate_limit_hits")

    def __init__(self) -> None:
        self.soft_retries_used = 0
        self.rate_limit_hits = 0

    async def claim_soft_retry(
        self, url: str, *, sleep: Callable[[float], Awaitable[None]]
    ) -> bool:
        """Take one retry from this scrape's soft rate-limit budget.

        Returns whether the caller may re-navigate. The budget is the
        scrape's, not the section's, so a scrape already being throttled
        stops asking instead of sending one extra navigation per remaining
        section. Each retry waits twice as long as the one before it.

        `sleep` is handed the base delay and owns the jitter: the session's
        pacing boundary, so the randomness stays behind the one seam a test
        or a policy trace neutralises.
        """
        if self.soft_retries_used >= RATE_LIMIT_RETRY_BUDGET:
            logger.warning(
                "Soft rate-limit retry budget (%d) spent, not re-fetching %s",
                RATE_LIMIT_RETRY_BUDGET,
                url,
            )
            return False

        delay = RATE_LIMIT_RETRY_DELAY * 2**self.soft_retries_used
        self.soft_retries_used += 1
        logger.info("Retrying %s after ~%.1fs backoff", url, delay)
        await sleep(delay)
        return True
