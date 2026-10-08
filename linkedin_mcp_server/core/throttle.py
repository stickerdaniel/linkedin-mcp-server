"""What LinkedIn answered with, kept for the length of one tool call.

A call that fails on something `raise_tool_error` does not classify reaches the
client as "Error calling tool", because the server masks unclassified errors.
When LinkedIn refused some of the requests the page made during that call, the
failure is most likely throttling, but nothing read a response status, so the
client was told nothing and the failure read as a parser bug.

So every response's status is recorded by a listener installed on the page at
browser start, and cleared as each tool call starts. The record is evidence
only: it raises nothing and retries nothing, and a call that succeeds is
untouched by it. `raise_tool_error` appends it to a failure that happened
anyway. Classifying a refused navigation as a rate limit is a separate
question, and is left to the navigation code.

Only the status, the URL and a numeric ``Retry-After`` are read. No body is
read and nothing is sent.

Global rather than per-page, like the browser it observes: one process drives
one shared page, and `raise_tool_error` is reached through call paths that
carry no record of their own.

Status 999 sits beside 429 because LinkedIn answers with it for the same
reason under a different name -- its "Request denied" for traffic it decides is
not a person.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakSet

logger = logging.getLogger(__name__)

THROTTLED_STATUSES = frozenset({429, 999})

# How many throttled responses are kept. The count is exact regardless; this
# caps only the sample, and a throttled page can produce hundreds.
_SAMPLE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class ThrottleHit:
    """One response LinkedIn refused to serve."""

    status: int
    url: str
    retry_after: int | None = None


_hits: list[ThrottleHit] = []
_latest: ThrottleHit | None = None
_count: int = 0
_watched: WeakSet[Any] = WeakSet()


def watch_responses(page: Any) -> None:
    """Record the status of every response this page receives."""
    try:
        if page in _watched:
            return
        _watched.add(page)
    except TypeError:
        # A page that cannot be weak-referenced is still worth watching; the
        # only cost of registering twice is a doubled count.
        logger.debug("Page is not weak-referenceable; watching it anyway")
    page.on("response", _record)


def reset_throttle_record() -> None:
    """Forget the previous call's evidence. Called as a tool call starts."""
    global _count, _latest
    _count = 0
    _latest = None
    _hits.clear()


def throttled_count() -> int:
    """How many responses were throttled during this call."""
    return _count


def throttle_evidence() -> str | None:
    """One sentence naming the throttling, or None when there was none."""
    last = _latest
    if last is None:
        return None

    statuses = " and ".join(
        f"HTTP {status}" for status in sorted({h.status for h in _hits})
    )
    asked = (
        f" LinkedIn asked for {last.retry_after} seconds."
        if last.retry_after is not None
        else ""
    )
    return (
        f"LinkedIn answered {statuses} to {_count} "
        f"{'request' if _count == 1 else 'requests'} during this call, most "
        f"recently {_path(last.url)}. LinkedIn was throttling this session, "
        f"so wait before retrying.{asked}"
    )


def _path(url: str) -> str:
    """The path alone: a throttled URL's query carries ids and no diagnosis."""
    try:
        return urlsplit(url).path or url
    except ValueError:
        return url


def _retry_after(response: Any) -> int | None:
    """``Retry-After`` in seconds, when the header carries a plain number.

    The HTTP-date form is left unread: it is rare from LinkedIn, and a date
    parsed against a clock that may be wrong is worse than no answer.
    """
    try:
        raw = response.headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _record(response: Any) -> None:
    """Note one response. Registered on the page; never raises into Playwright."""
    global _count, _latest

    try:
        status = int(response.status)
        if status not in THROTTLED_STATUSES:
            return
        hit = ThrottleHit(
            status=status, url=str(response.url), retry_after=_retry_after(response)
        )
    except Exception:
        logger.debug("Could not read a response status", exc_info=True)
        return

    _count += 1
    _latest = hit
    if len(_hits) < _SAMPLE_LIMIT:
        _hits.append(hit)
