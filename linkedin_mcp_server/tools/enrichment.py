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
from datetime import datetime
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError, RateLimitError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.pacing import (
    DEFAULT_DAILY_ACTIONS,
    MAX_DAILY_ACTIONS,
    Job,
    JobStore,
    next_bunch_delay,
    step_delay,
)

logger = logging.getLogger(__name__)

# Leave headroom inside the tool timeout so a bunch always returns and
# persists rather than being killed mid-profile.
DEADLINE_FRACTION = 0.75


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
        daily_cap: Annotated[int, Field(ge=1, le=MAX_DAILY_ACTIONS)] = (
            DEFAULT_DAILY_ACTIONS
        ),
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
            daily_cap: Profile views allowed per rolling 24 hours (1-150,
                default 100). This is a behavioral budget, not a LinkedIn API
                limit -- LinkedIn publishes no official number.
            warmup: Ramp the cap over four weeks (10/day in week 1, 20 in
                week 2, 50 in week 3, full cap after). Leave on unless this
                account already runs automation at volume.
            replace_existing: Overwrite an existing job of the same name
                instead of appending new usernames to it.

        Returns:
            Job summary: queued count, duplicates dropped, effective cap today.
        """
        try:
            cleaned = [_normalize(u) for u in usernames]
            cleaned = [u for u in cleaned if u]
            if not cleaned:
                raise ToolError("No usable usernames were provided.")

            now = datetime.now().astimezone()

            if store.exists(job_name) and not replace_existing:
                job = store.load(job_name)
                known = set(job.pending) | set(job.done) | set(job.failed)
                fresh = [u for u in dict.fromkeys(cleaned) if u not in known]
                job.pending.extend(fresh)
                added = len(fresh)
            else:
                deduped = list(dict.fromkeys(cleaned))
                job = Job(
                    name=job_name,
                    started_on=now.date(),
                    pending=deduped,
                    daily_cap=daily_cap,
                    warmup=warmup,
                )
                added = len(deduped)

            store.save(job)

            return {
                "job": job.name,
                "added": added,
                "duplicates_dropped": len(cleaned) - added,
                "pending": len(job.pending),
                "done": len(job.done),
                "daily_cap_today": job.effective_cap(now),
                "remaining_today": job.remaining_today(now),
                "note": (
                    "Queue saved. Call run_enrichment_bunch to process a bunch; "
                    "it returns when to call it again."
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
        bunch_size: Annotated[int, Field(ge=1, le=25)] = 5,
        sections: str | None = None,
        ignore_schedule: bool = False,
        extractor: Any | None = None,
    ) -> dict[str, Any]:
        """
        Visit the next few profiles in a job, pacing them like a human would.

        Stops early -- persisting everything gathered -- when the bunch is
        done, the rolling 24-hour budget is spent, the working window closes,
        the tool timeout approaches, or LinkedIn signals a rate limit.

        Args:
            job_name: The job created by start_enrichment_job.
            ctx: FastMCP context for progress reporting.
            bunch_size: Profiles to visit in this call (1-25, default 5).
                Larger bunches are denser activity; the default keeps a call
                inside the tool timeout with randomized gaps between loads.
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
        try:
            job = store.load(job_name)
        except FileNotFoundError:
            raise ToolError(
                f"No job named {job_name!r}. Create it with start_enrichment_job."
            ) from None

        now = datetime.now().astimezone()
        rng = random.Random()

        # Schedule gate. Checked before the budget so a closed window reports
        # the real reason rather than "no budget".
        if not ignore_schedule and not job.schedule.is_open(now):
            opens = job.schedule.next_open(now)
            return _status(
                job,
                now,
                stopped="outside_working_hours",
                next_run_after=(opens - now).total_seconds(),
                gathered={},
                detail=f"Working window reopens {opens.isoformat(timespec='minutes')}.",
            )

        if not job.pending:
            return _status(
                job, now, stopped="queue_empty", next_run_after=None, gathered={}
            )

        budget = job.remaining_today(now)
        if budget <= 0:
            return _status(
                job,
                now,
                stopped="daily_budget_spent",
                next_run_after=job.ledger.next_expiry(now),
                gathered={},
                detail=(
                    "The rolling 24-hour budget is spent. It refills gradually "
                    "as individual actions age out, not all at once at midnight."
                ),
            )

        try:
            extractor = extractor or await get_ready_extractor(
                ctx, tool_name="run_enrichment_bunch"
            )
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "run_enrichment_bunch")

        requested_sections = {
            s.strip() for s in (sections or "").split(",") if s.strip()
        }

        planned = min(bunch_size, budget, len(job.pending))
        deadline = asyncio.get_running_loop().time() + tool_timeout * DEADLINE_FRACTION

        gathered: dict[str, Any] = {}
        stopped = "bunch_complete"

        for index in range(planned):
            if asyncio.get_running_loop().time() >= deadline:
                stopped = "tool_deadline"
                break

            username = job.pending[0]
            now = datetime.now().astimezone()

            try:
                result = await extractor.scrape_person(
                    username, requested_sections, callbacks=None
                )
            except RateLimitError as e:
                # Do not consume the queue entry; the profile was never read.
                # Back off hard -- pushing through a rate limit is how a
                # throttle becomes a restriction.
                logger.warning("Rate limited during enrichment bunch: %s", e)
                store.save(job)
                return _status(
                    job,
                    now,
                    stopped="rate_limited",
                    next_run_after=max(job.ledger.next_expiry(now), 3600.0),
                    gathered=gathered,
                    detail=(
                        "LinkedIn signalled a rate limit. Progress is saved. "
                        "Wait at least an hour; if it repeats, stop for the day."
                    ),
                )
            except Exception as e:
                job.pending.pop(0)
                job.failed[username] = str(e)[:200]
                store.save(job)
                logger.info("Enrichment failed for %s: %s", username, e)
                continue

            job.pending.pop(0)
            job.done[username] = result
            gathered[username] = result
            # Every page load counts, including the extra sections.
            for _ in range(1 + len(requested_sections)):
                job.ledger.record(now)
            # Persist per profile: a kill mid-bunch costs one page view, not
            # the whole bunch.
            store.save(job)

            await ctx.report_progress(
                progress=index + 1,
                total=planned,
                message=f"{len(job.done)} done, {len(job.pending)} pending",
            )

            is_last = index == planned - 1
            if not is_last:
                await asyncio.sleep(step_delay(rng=rng))

        now = datetime.now().astimezone()
        remaining = job.remaining_today(now)
        if remaining <= 0:
            stopped = "daily_budget_spent"
            wait = job.ledger.next_expiry(now)
        elif not job.pending:
            stopped = "queue_empty"
            wait = None
        else:
            wait = next_bunch_delay(remaining, bunch_size, now, job.schedule, rng)

        return _status(
            job, now, stopped=stopped, next_run_after=wait, gathered=gathered
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
                return {"jobs": store.list_jobs()}

            job = store.load(job_name)
            now = datetime.now().astimezone()
            summary = _status(job, now, stopped=None, next_run_after=None, gathered={})
            summary["results"] = job.done
            summary["failures"] = job.failed
            return summary
        except FileNotFoundError:
            raise ToolError(f"No job named {job_name!r}.") from None
        except Exception as e:
            raise_tool_error(e, "get_enrichment_status")  # NoReturn


def _status(
    job: Job,
    now: datetime,
    *,
    stopped: str | None,
    next_run_after: float | None,
    gathered: dict[str, Any],
    detail: str | None = None,
) -> dict[str, Any]:
    """Shared progress payload."""
    total = len(job.pending) + len(job.done) + len(job.failed)
    out: dict[str, Any] = {
        "job": job.name,
        "total": total,
        "done": len(job.done),
        "pending": len(job.pending),
        "failed": len(job.failed),
        "spent_last_24h": job.ledger.spent(now),
        "daily_cap_today": job.effective_cap(now),
        "remaining_today": job.remaining_today(now),
        "working_hours_open": job.schedule.is_open(now),
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
    return out


def _human(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"
