"""Resumable, human-paced bulk profile enrichment.

Enriching a large connection list cannot be one long tool call: a network of
several thousand people, viewed at a rate LinkedIn treats as ordinary
browsing, is days of work. So the unit here is one small *bunch* -- a handful
of profiles, loaded with randomized gaps -- after which the tool persists what
it learned, reports how much of today's budget is left, and says when the next
bunch should run. Call it again then. Nothing is lost if the process dies, the
machine sleeps, or a week passes between calls.

The pacing arithmetic lives in ``linkedin_mcp_server.pacing``; this module is
the browser-facing half.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from patchright._impl._errors import TargetClosedError
from patchright.async_api import Error as PatchrightError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import (
    AuthenticationError,
    RateLimitError,
    ScrapingError,
)
from linkedin_mcp_server.dependencies import get_ready_extractor
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    ACCOUNT_BUDGET_JOB,
    Job,
    JobStore,
    bunch_size_max,
    default_daily_actions,
    load_account_budget,
    max_daily_actions,
    next_bunch_delay,
    request_arrived_at,
    step_delay,
)

logger = logging.getLogger(__name__)

# Leave headroom inside the tool timeout so a bunch always returns and
# persists rather than being killed mid-profile.
DEADLINE_FRACTION = 0.75

# What the bunch tells the caller when it was already out of time on arrival:
# nothing ran, so the pause is a tool-call gap, not a between-bunches one.
RETRY_AFTER_QUEUED_OUT = 10.0

# Empty pages in a row before a profile is filed as failed rather than read
# as a rate limit: a deleted or private URL looks exactly like a throttle.
EMPTY_PAGE_STRIKES = 2

_CLOSED_TARGET_MSG = "Target page, context or browser has been closed"


def _browser_gone(e: BaseException) -> bool:
    """Whether ``e`` says the browser itself is gone, not the page asked for."""
    return isinstance(e, TargetClosedError) or (
        isinstance(e, PatchrightError) and _CLOSED_TARGET_MSG in str(e)
    )


class _BrowserGone(ScrapingError):
    """A navigation that loaded nothing because the browser itself is gone.

    Measured: the daemon's Chrome died mid-run, the scrapers filed every
    section as an error, and the bunch loops marked the target done and
    charged the budget for zero LinkedIn traffic. One class for both shapes
    of that incident -- the swallowed one (``sections`` empty, the reasons in
    ``section_errors``) and the re-raised ``TargetClosedError`` -- so a loop
    can relaunch the browser and retry once, and decline to charge either.
    """

    def __init__(
        self, message: str, section_errors: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.section_errors = section_errors or {}


def _closed_target_filed(errors: dict[str, Any]) -> bool:
    """Whether a swallowed section error is the browser being gone.

    ``error_type`` is the exception's class name, so an empty result alone
    does not say why: a navigation ``TimeoutError``, a blank page or an
    auth-walled tab file the same shape, and none of them is a dead browser.
    Treating those as one relaunched a live browser and left the target at
    the head of the queue for every later call to retry.
    """
    return any(
        _CLOSED_TARGET_MSG in (e.get("error_message") or "")
        or e.get("error_type") == "TargetClosedError"
        for e in errors.values()
    )


class _RelaunchFailed(Exception):
    """A relaunch that failed, carried past the per-target handlers.

    ``raise_tool_error`` re-raises an exception it cannot classify as-is, so
    calling it inside the retry path would land that exception in the loop's
    generic handler, which files the target as failed and moves on to scrape
    the next one on the same dead extractor -- until the bunch is exhausted.
    Measured with ``BrowserBusyError``: a 3-profile queue drained into
    ``failed``. The cause is re-raised only once it is outside those handlers.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _EmptyPage(RateLimitError):
    """A profile that came back as an empty shell, filed as a soft rate limit.

    Kept apart from the hard kind (an HTTP 429, a checkpoint or authwall
    challenge raised by the extractor) because only this shape is ambiguous
    with a deleted or private profile and may be struck out; a hard limit
    under sustained pressure must never drain the queue into ``failed``.
    """


def _soft_rate_limit(result: dict[str, Any]) -> str | None:
    """The message of a rate-limit section error, if the scrape filed one.

    The scrapers do not raise for a soft rate limit; they file it under
    ``section_errors`` and return without the section.
    """
    for error in result.get("section_errors", {}).values():
        if error.get("error_type") == "rate_limit":
            return error.get("error_message") or "LinkedIn rate limit detected."
    return None


def _refuse_reserved(job_name: str) -> None:
    """The shared budget record lives in the job store but is not a job.

    Every tool that takes a job name goes through here: creating it would
    overwrite the account's pacing state, and loading it would hand the
    private action history back as if it were a user's results.
    """
    if job_name == ACCOUNT_BUDGET_JOB:
        raise ToolError(f"{job_name!r} is a reserved internal name; choose another.")


def _normalize(username: str) -> str:
    """Reduce a profile URL or handle to a bare username."""
    cleaned = username.strip().rstrip("/")
    if "/in/" in cleaned:
        cleaned = cleaned.split("/in/", 1)[1]
    return cleaned.split("?")[0].split("/")[0]


def register_enrichment_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the paced bulk-enrichment tools."""

    store = JobStore()

    @mcp.tool(
        timeout=tool_timeout,
        title="Start Enrichment Job",
        annotations={"readOnlyHint": False, "openWorldHint": False},
        tags={"person", "bulk"},
    )
    async def start_enrichment_job(
        job_name: str,
        usernames: list[str],
        daily_cap: Annotated[int, Field(ge=1)] | None = None,
        warmup: bool = True,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """
        Create a resumable bulk enrichment job from a list of profiles.

        This only writes the queue to disk -- it visits nothing. Call
        run_enrichment_bunch to start working through it.

        Args:
            job_name: Short identifier, e.g. "egypt-gulf". Reused to resume.
            usernames: LinkedIn usernames or profile URLs. Deduplicated;
                already-completed people are skipped when a job is resumed.
            daily_cap: Profile views allowed per rolling 24 hours (default
                100, ceiling 150; DAILY_ACTIONS_DEFAULT / DAILY_ACTIONS_MAX
                move both). Anything above the ceiling is clamped to it. This
                is a behavioral budget, not a LinkedIn API limit -- LinkedIn
                publishes no official number.
            warmup: Ramp the cap over four weeks (10/day in week 1, 20 in
                week 2, 50 in week 3, full cap after). Leave on unless this
                account already runs automation at volume.
            replace_existing: Overwrite an existing job of the same name
                instead of appending new usernames to it.

        Returns:
            Job summary: queued count, duplicates dropped, effective cap today.
        """
        try:
            _refuse_reserved(job_name)
            cleaned = [_normalize(u) for u in usernames]
            cleaned = [u for u in cleaned if u]
            if not cleaned:
                raise ToolError("No usable usernames were provided.")

            now = datetime.now().astimezone()

            if daily_cap is None:
                daily_cap = default_daily_actions()
            ceiling = max_daily_actions()
            if daily_cap > ceiling:
                logger.info(
                    "Clamping daily_cap=%d to the ceiling %d", daily_cap, ceiling
                )
                daily_cap = ceiling

            # daily_cap and warmup configure the one shared account budget, not
            # this queue -- the cap is account-wide, so several jobs cannot each
            # spend a full allowance. Last writer wins if they disagree.
            budget = load_account_budget(store, now, daily_cap=daily_cap, warmup=warmup)

            if store.exists(job_name) and not replace_existing:
                job = store.load(job_name)
                known = set(job.pending) | set(job.done) | set(job.failed)
                fresh = [u for u in dict.fromkeys(cleaned) if u not in known]
                job.pending.extend(fresh)
                added = len(fresh)
            else:
                deduped = list(dict.fromkeys(cleaned))
                job = Job(name=job_name, started_on=now.date(), pending=deduped)
                added = len(deduped)

            store.save(job)

            return {
                "job": job.name,
                "added": added,
                "duplicates_dropped": len(cleaned) - added,
                "pending": len(job.pending),
                "done": len(job.done),
                "account_daily_cap_today": budget.effective_cap(now),
                "account_remaining_today": budget.remaining_today(now),
                "note": (
                    "Queue saved. daily_cap/warmup set the shared account "
                    "budget used by all enrichment. Call run_enrichment_bunch "
                    "to process a bunch; it returns when to call it again."
                ),
            }
        except ToolError:
            raise
        except Exception as e:
            raise_tool_error(e, "start_enrichment_job")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Run Enrichment Bunch",
        annotations={"readOnlyHint": False, "openWorldHint": True},
        tags={"person", "bulk", "scraping"},
        exclude_args=["extractor"],
    )
    async def run_enrichment_bunch(
        job_name: str,
        ctx: Context,
        bunch_size: Annotated[int, Field(ge=1)] = 5,
        sections: str | None = None,
        ignore_schedule: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Visit the next few profiles in a job, pacing them like a human would.

        Stops early -- persisting everything gathered -- when the bunch is
        done, the rolling 24-hour budget is spent, the working window closes,
        the tool timeout approaches, LinkedIn signals a rate limit, or the
        browser has gone away and a relaunch did not bring it back
        (browser_unavailable: the profile stays pending and nothing is
        charged).

        Args:
            job_name: The job created by start_enrichment_job.
            ctx: FastMCP context for progress reporting.
            bunch_size: Profiles to visit in this call (default 5, ceiling
                25 unless BUNCH_SIZE_MAX moves it; more is clamped). Larger
                bunches are denser activity; the default keeps a call inside
                the tool timeout with randomized gaps between loads.
            sections: Comma-separated extra profile sections, as in
                get_person_profile. Each extra section is another page load
                and counts separately against the budget, so leave this unset
                unless the main profile really is not enough.
            ignore_schedule: Run outside working hours. Off by default --
                activity at 03:00 local is one of the cheaper signals to
                notice. Use only for a one-off catch-up.

        Returns:
            Progress, the profiles gathered this bunch, and next_run_after --
            the number of seconds to wait before calling again.
        """
        _refuse_reserved(job_name)
        try:
            job = store.load(job_name)
        except FileNotFoundError:
            raise ToolError(
                f"No job named {job_name!r}. Create it with start_enrichment_job."
            ) from None

        ceiling = bunch_size_max()
        if bunch_size > ceiling:
            logger.info("Clamping bunch_size=%d to the ceiling %d", bunch_size, ceiling)
            bunch_size = ceiling

        now = datetime.now().astimezone()
        rng = random.Random()
        budget = load_account_budget(store, now)

        # Schedule gate. Checked before the budget so a closed window reports
        # the real reason rather than "no budget".
        if not ignore_schedule and not budget.schedule.is_open(now):
            opens = budget.schedule.next_open(now)
            return _status(
                job,
                budget,
                now,
                stopped="outside_working_hours",
                next_run_after=(opens - now).total_seconds(),
                gathered={},
                detail=f"Working window reopens {opens.isoformat(timespec='minutes')}.",
            )

        if not job.pending:
            return _status(
                job,
                budget,
                now,
                stopped="queue_empty",
                next_run_after=None,
                gathered={},
            )

        requested_sections = {
            s.strip() for s in (sections or "").split(",") if s.strip()
        }
        # Each profile costs one page load for the main profile plus one per
        # extra section, so the budget is measured in those units -- not in
        # profiles. Planning in profiles (the old bug) overspent the cap by
        # len(sections)x.
        cost = 1 + len(requested_sections)

        remaining = budget.remaining_today(now)
        if remaining < cost:
            return _status(
                job,
                budget,
                now,
                stopped="daily_budget_spent",
                next_run_after=budget.ledger.next_expiry(now),
                gathered={},
                detail=(
                    "The shared account budget can't afford another profile "
                    f"({remaining} left, {cost} needed). It refills gradually "
                    "as individual actions age out, not all at once at midnight."
                ),
            )

        # From arrival at the middleware, not from here: the frontend proxy
        # gives up tool_timeout + 30 s (210 s at the defaults) after it sent
        # the call, and a call queued behind another session's spends that
        # waiting before its own timeout starts. Measured: a bunch let
        # through after the proxy had gone ran to completion for nobody.
        arrived = request_arrived_at.get()
        deadline = (
            time.monotonic() if arrived is None else arrived
        ) + tool_timeout * DEADLINE_FRACTION
        if time.monotonic() >= deadline:
            return _status(
                job,
                budget,
                now,
                stopped="tool_deadline",
                next_run_after=RETRY_AFTER_QUEUED_OUT,
                gathered={},
                detail=(
                    "Queued behind other calls for longer than the tool "
                    "deadline; nothing was loaded and nothing was charged. "
                    "Call again."
                ),
            )

        extractor = extractor or await get_ready_extractor(
            ctx, tool_name="run_enrichment_bunch"
        )

        # Never plan more profiles than the budget can pay for at `cost` each.
        planned = min(bunch_size, remaining // cost, len(job.pending))

        gathered: dict[str, Any] = {}
        stopped = "bunch_complete"
        # The username struck out by the previous visit of this call, if any:
        # the next visit emptying too says the whole session is throttled.
        struck_this_call: str | None = None

        async def _scrape(username: str) -> dict[str, Any]:
            """One profile visit, with both shapes of a dead browser raised
            as ``_BrowserGone`` and a soft rate limit raised as such."""
            try:
                result = await extractor.scrape_person(
                    username, requested_sections, callbacks=None
                )
            except Exception as e:
                if _browser_gone(e):
                    raise _BrowserGone(str(e)) from e
                raise
            if not result.get("sections"):
                # With nothing loaded, a filed rate limit is the whole answer,
                # and it is a rate limit, not a dead browser.
                if limit := _soft_rate_limit(result):
                    raise _EmptyPage(limit)
                errors = result.get("section_errors", {})
                if _closed_target_filed(errors):
                    raise _BrowserGone("every section failed", errors)
            return result

        async def _relaunch(username: str) -> Any:
            # Re-acquiring goes through get_or_create_browser, which relaunches
            # a dead browser; the extractor is bound to the old page, so it is
            # re-created too. The caller retries the same profile once.
            logger.warning(
                "browser gone under %s; relaunching and retrying once", username
            )
            try:
                return await get_ready_extractor(ctx, tool_name="run_enrichment_bunch")
            except Exception as e:
                raise _RelaunchFailed(e) from e

        def _browser_unavailable(e: _BrowserGone) -> dict[str, Any]:
            # A closed browser is not a fact about the profile; nothing was
            # loaded, so nothing is charged, and the profile stays pending.
            logger.warning("Browser unavailable during enrichment bunch: %s", e)
            store.save(job)
            store.save(budget)
            return _status(
                job,
                budget,
                now,
                stopped="browser_unavailable",
                next_run_after=60.0,
                gathered=gathered,
                detail=(
                    "The browser is gone and a relaunch did not bring it back; "
                    "nothing was loaded and nothing was charged. Progress saved."
                ),
                section_errors=e.section_errors,
            )

        for index in range(planned):
            if time.monotonic() >= deadline:
                stopped = "tool_deadline"
                break

            username = job.pending[0]
            now = datetime.now().astimezone()

            try:
                try:
                    result = await _scrape(username)
                except _BrowserGone:
                    # One retry of the same profile: a second failure is the
                    # stop below.
                    extractor = await _relaunch(username)
                    result = await _scrape(username)
            except _RelaunchFailed as e:
                # The profile was never read and stays pending, uncharged.
                store.save(job)
                raise_tool_error(e.cause, "run_enrichment_bunch")  # NoReturn
            except _BrowserGone as e:
                return _browser_unavailable(e)
            except (RateLimitError, AuthenticationError) as e:
                # Neither consumes the queue entry -- the profile was never
                # read. A rate limit means back off; an expired session means
                # the next call re-authenticates via get_ready_extractor. Both
                # save progress and stop rather than draining the queue into
                # `failed` (which an auth error, a sibling of RateLimitError,
                # would otherwise do by falling through to the generic handler).
                is_auth = isinstance(e, AuthenticationError)
                if isinstance(e, _EmptyPage):
                    # The heuristic cannot tell a throttle from a profile that
                    # is gone; both come back as an empty shell. The same
                    # username emptying on consecutive calls is struck out
                    # rather than heading the queue on every call forever --
                    # unless the very next profile in the same call empties
                    # too, which is a session-wide throttle, not a dead URL:
                    # then the strike-out is undone and the call backs off.
                    job.strikes[username] = job.strikes.get(username, 0) + 1
                    if struck_this_call is not None:
                        job.pending.insert(0, struck_this_call)
                        del job.failed[struck_this_call]
                        job.strikes[struck_this_call] = EMPTY_PAGE_STRIKES
                        logger.info(
                            "%s emptied right after %s was struck out; "
                            "re-queued it as a throttle",
                            username,
                            struck_this_call,
                        )
                    elif job.strikes[username] >= EMPTY_PAGE_STRIKES:
                        job.pending.pop(0)
                        del job.strikes[username]
                        job.failed[username] = (
                            f"empty page on {EMPTY_PAGE_STRIKES} consecutive visits "
                            "(heuristic rate limit); profile is probably deleted "
                            "or private"
                        )
                        # The striking visit was a real page load, so it is
                        # charged and paced like any other; the first stays
                        # uncharged.
                        for _ in range(cost):
                            budget.ledger.record(now)
                        store.save(job)
                        store.save(budget)
                        logger.info("Enrichment struck out %s: %s", username, e)
                        struck_this_call = username
                        if index != planned - 1:
                            await asyncio.sleep(step_delay(rng=rng))
                        continue
                logger.warning(
                    "%s during enrichment bunch: %s",
                    "Auth expired" if is_auth else "Rate limited",
                    e,
                )
                if not is_auth and not isinstance(e, _EmptyPage):
                    # A hard 429 was still a load LinkedIn counted, and the
                    # middleware leaves recording to this tool. A first empty
                    # page stays uncharged on purpose (see the strike above).
                    budget.ledger.record(now)
                store.save(job)
                store.save(budget)
                return _status(
                    job,
                    budget,
                    now,
                    stopped="session_expired" if is_auth else "rate_limited",
                    next_run_after=(
                        60.0 if is_auth else max(budget.ledger.next_expiry(now), 3600.0)
                    ),
                    gathered=gathered,
                    detail=(
                        "LinkedIn session expired. Progress saved; the next "
                        "call will prompt re-login."
                        if is_auth
                        else (
                            "LinkedIn signalled a rate limit. Progress saved. "
                            "Wait at least an hour; if it repeats, stop for "
                            "the day."
                        )
                    ),
                )
            except Exception as e:
                job.pending.pop(0)
                job.strikes.pop(username, None)
                job.failed[username] = str(e)[:200]
                store.save(job)
                logger.info("Enrichment failed for %s: %s", username, e)
                struck_this_call = None
                continue

            job.pending.pop(0)
            job.strikes.pop(username, None)
            struck_this_call = None
            job.done[username] = result
            gathered[username] = result
            # Every page load counts against the shared budget, extras included.
            for _ in range(cost):
                budget.ledger.record(now)
            # Persist per profile: a kill mid-bunch costs one page view, not
            # the whole bunch. Budget and queue are saved together.
            store.save(job)
            store.save(budget)

            await ctx.report_progress(
                progress=index + 1,
                total=planned,
                message=f"{len(job.done)} done, {len(job.pending)} pending",
            )

            is_last = index == planned - 1
            if not is_last:
                await asyncio.sleep(step_delay(rng=rng))

        now = datetime.now().astimezone()
        remaining = budget.remaining_today(now)
        if remaining < cost:
            stopped = "daily_budget_spent"
            wait = budget.ledger.next_expiry(now)
        elif not job.pending:
            stopped = "queue_empty"
            wait = None
        else:
            # More queue and more budget remain (bunch_complete or tool_deadline);
            # tell the caller when to come back for the next bunch.
            wait = next_bunch_delay(remaining, bunch_size, now, budget.schedule, rng)

        return _status(
            job, budget, now, stopped=stopped, next_run_after=wait, gathered=gathered
        )

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Enrichment Status",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"person", "bulk"},
    )
    async def get_enrichment_status(job_name: str | None = None) -> dict[str, Any]:
        """
        Report progress on an enrichment job, or list all jobs.

        Args:
            job_name: Job to report on. Omit to list every known job.

        Returns:
            Progress counts, today's remaining budget, and the collected
            results when a single job is named.
        """
        try:
            if job_name is None:
                # Hide the internal shared-budget record; it is not a job.
                return {
                    "jobs": [j for j in store.list_jobs() if j != ACCOUNT_BUDGET_JOB]
                }

            _refuse_reserved(job_name)
            job = store.load(job_name)
            now = datetime.now().astimezone()
            budget = load_account_budget(store, now)
            summary = _status(
                job, budget, now, stopped=None, next_run_after=None, gathered={}
            )
            summary["results"] = job.done
            summary["failures"] = job.failed
            return summary
        except FileNotFoundError:
            raise ToolError(f"No job named {job_name!r}.") from None
        except Exception as e:
            raise_tool_error(e, "get_enrichment_status")  # NoReturn


def _status(
    job: Job,
    budget: Job,
    now: datetime,
    *,
    stopped: str | None,
    next_run_after: float | None,
    gathered: dict[str, Any],
    detail: str | None = None,
    section_errors: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared progress payload.

    Queue counts come from ``job``; the budget metrics come from the shared
    account ``budget`` -- they are two different records now.
    """
    total = len(job.pending) + len(job.done) + len(job.failed)
    out: dict[str, Any] = {
        "job": job.name,
        "total": total,
        "done": len(job.done),
        "pending": len(job.pending),
        "failed": len(job.failed),
        "account_spent_last_24h": budget.ledger.spent(now),
        "account_daily_cap_today": budget.effective_cap(now),
        "account_remaining_today": budget.remaining_today(now),
        "working_hours_open": budget.schedule.is_open(now),
    }
    if stopped is not None:
        out["stopped_because"] = stopped
    if next_run_after is not None:
        out["next_run_after_seconds"] = round(next_run_after)
        out["next_run_after_human"] = _human(next_run_after)
    if gathered:
        out["gathered"] = gathered
    if detail:
        out["detail"] = detail
    if section_errors:
        out["section_errors"] = section_errors
    return out


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"
