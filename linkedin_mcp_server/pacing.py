"""Human-paced action budgeting for bulk work spread over days.

LinkedIn restricts accounts on *behavioral pattern*, not raw volume alone --
150 profiles viewed steadily across a workday reads differently from 150
viewed in half an hour. Bulk jobs therefore cannot run as one long loop; they
have to be a sequence of small bunches, paced apart, stopped overnight and at
weekends, and resumable across process restarts.

This module holds the scheduling arithmetic for that, deliberately free of any
browser or MCP dependency so it can be reasoned about and tested directly.
Every function takes ``now`` explicitly rather than reading the clock, so the
tests do not sleep.

The model mirrors what the established LinkedIn automation tools converged on:

* A **rolling 24-hour** action budget. Not a midnight reset -- each action
  ages out exactly 24 hours after it happened. A midnight reset lets a job
  spend its whole budget at 23:00 and again at 00:01, which is precisely the
  burst shape that gets flagged.
* **Working hours** (default 09:00-18:00 local, weekends off, lunch skipped),
  because a member who views profiles at 04:00 on a Sunday is not browsing.
* **Randomized** gaps and a jittered daily cap, so the traffic carries no
  fixed period and no suspiciously round daily total.
* A **warm-up ramp**, because the pattern change matters as much as the level:
  an account that has never automated jumping straight to 100 views/day is a
  step function.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 24 * 60 * 60

# Ceiling across all action types, per the limits every major tool publishes.
# Profile views are cheaper than invites, so the view-only default sits below
# it; neither is an official LinkedIn number -- LinkedIn publishes none.
MAX_DAILY_ACTIONS = 150
DEFAULT_DAILY_ACTIONS = 100

# Gap inside a bunch. Short enough that a bunch fits one MCP tool call,
# long enough that page loads are not back-to-back.
DEFAULT_STEP_DELAY = (8.0, 25.0)

# Bunches are spaced to spread the daily budget across the working window,
# then clamped to this range so the spacing stays plausible either way.
MIN_BUNCH_PAUSE = 60.0
MAX_BUNCH_PAUSE = 3600.0


@dataclass(frozen=True)
class Schedule:
    """When automated work is allowed to run, in the operator's local time."""

    work_start: int = 9
    work_end: int = 18
    lunch_start: int | None = 12
    lunch_end: int | None = 13
    # Monday is 0, matching datetime.weekday().
    days_off: tuple[int, ...] = (5, 6)

    def is_open(self, now: datetime) -> bool:
        """True when `now` falls inside the working window."""
        if now.weekday() in self.days_off:
            return False
        if not (self.work_start <= now.hour < self.work_end):
            return False
        return not self._in_lunch(now)

    def _in_lunch(self, now: datetime) -> bool:
        if self.lunch_start is None or self.lunch_end is None:
            return False
        return self.lunch_start <= now.hour < self.lunch_end

    def next_open(self, now: datetime) -> datetime:
        """The first instant at or after `now` when work may run.

        Walks forward in hour steps rather than solving analytically -- the
        window is small and irregular (lunch, weekends), and a loop that is
        obviously correct beats arithmetic that is nearly correct.
        """
        if self.is_open(now):
            return now

        candidate = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        # 14 days is far past any weekend-plus-holiday gap this can produce;
        # if nothing opens by then the schedule is misconfigured.
        for _ in range(24 * 14):
            if self.is_open(candidate):
                return candidate
            candidate += timedelta(hours=1)

        raise ValueError(
            "Schedule never opens -- check work_start/work_end/days_off "
            f"(start={self.work_start}, end={self.work_end}, off={self.days_off})"
        )

    def seconds_until_close(self, now: datetime) -> float:
        """Working seconds left today, lunch excluded. 0 when closed."""
        if not self.is_open(now):
            return 0.0

        close = now.replace(hour=self.work_end, minute=0, second=0, microsecond=0)
        remaining = (close - now).total_seconds()

        # Lunch still ahead today is not usable time.
        if self.lunch_start is not None and self.lunch_end is not None:
            if now.hour < self.lunch_start:
                remaining -= (self.lunch_end - self.lunch_start) * 3600

        return max(remaining, 0.0)


@dataclass
class Ledger:
    """Timestamps of actions performed, as a rolling 24-hour window."""

    actions: list[float] = field(default_factory=list)

    def prune(self, now: datetime) -> None:
        cutoff = now.timestamp() - WINDOW_SECONDS
        self.actions = [t for t in self.actions if t > cutoff]

    def record(self, now: datetime) -> None:
        self.actions.append(now.timestamp())

    def spent(self, now: datetime) -> int:
        self.prune(now)
        return len(self.actions)

    def remaining(self, now: datetime, cap: int) -> int:
        return max(cap - self.spent(now), 0)

    def next_expiry(self, now: datetime) -> float:
        """Seconds until the oldest action ages out of the window.

        This is how long a budget-exhausted job must wait before it regains
        even one unit of headroom.
        """
        self.prune(now)
        if not self.actions:
            return 0.0
        return max(min(self.actions) + WINDOW_SECONDS - now.timestamp(), 0.0)


def warmup_cap(base_cap: int, started_on: date, today: date) -> int:
    """Ramp a fresh job up to `base_cap` over four weeks.

    The published warm-up schedules all share this shape: a fortnight of
    visibly low volume, then a climb. The exact numbers matter less than not
    presenting LinkedIn with a step change.
    """
    days = (today - started_on).days
    if days < 0:
        days = 0
    if days < 7:
        return min(10, base_cap)
    if days < 14:
        return min(20, base_cap)
    if days < 21:
        return min(50, base_cap)
    return base_cap


def jittered_cap(cap: int, today: date, salt: str = "") -> int:
    """Shave a stable, per-day random slice off the cap.

    A job that stops at exactly 100 every single day advertises itself. The
    draw is seeded by date so every call within a day agrees -- otherwise the
    effective cap would wobble between calls and the job could overshoot.
    """
    rng = random.Random(f"{salt}:{today.isoformat()}")
    return max(1, int(cap * rng.uniform(0.85, 1.0)))


def next_bunch_delay(
    remaining_budget: int,
    bunch_size: int,
    now: datetime,
    schedule: Schedule,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before running the next bunch.

    Spreads whatever budget is left across the working time left today, so the
    job finishes the day's allowance around closing time instead of racing
    through it by lunch.
    """
    rng = rng or random.Random()

    if remaining_budget <= 0:
        return MAX_BUNCH_PAUSE

    open_seconds = schedule.seconds_until_close(now)
    if open_seconds <= 0:
        return MAX_BUNCH_PAUSE

    bunches_left = max(math.ceil(remaining_budget / max(bunch_size, 1)), 1)
    base = open_seconds / bunches_left
    jittered = base * rng.uniform(0.75, 1.25)
    return max(MIN_BUNCH_PAUSE, min(jittered, MAX_BUNCH_PAUSE))


def step_delay(
    delay_range: tuple[float, float] = DEFAULT_STEP_DELAY,
    rng: random.Random | None = None,
) -> float:
    """A randomized gap between two profile loads inside one bunch."""
    rng = rng or random.Random()
    low, high = delay_range
    return rng.uniform(low, high)


@dataclass
class Job:
    """A resumable bulk job: its queue, its results, and its action ledger."""

    name: str
    started_on: date
    pending: list[str] = field(default_factory=list)
    done: dict[str, Any] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    ledger: Ledger = field(default_factory=Ledger)
    daily_cap: int = DEFAULT_DAILY_ACTIONS
    schedule: Schedule = field(default_factory=Schedule)
    warmup: bool = True

    def effective_cap(self, now: datetime) -> int:
        """Today's cap after the warm-up ramp and the daily jitter."""
        cap = min(self.daily_cap, MAX_DAILY_ACTIONS)
        if self.warmup:
            cap = warmup_cap(cap, self.started_on, now.date())
        return jittered_cap(cap, now.date(), salt=self.name)

    def remaining_today(self, now: datetime) -> int:
        return self.ledger.remaining(now, self.effective_cap(now))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_on": self.started_on.isoformat(),
            "pending": self.pending,
            "done": self.done,
            "failed": self.failed,
            "actions": self.ledger.actions,
            "daily_cap": self.daily_cap,
            "warmup": self.warmup,
            "schedule": {
                "work_start": self.schedule.work_start,
                "work_end": self.schedule.work_end,
                "lunch_start": self.schedule.lunch_start,
                "lunch_end": self.schedule.lunch_end,
                "days_off": list(self.schedule.days_off),
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Job:
        sched_raw = raw.get("schedule") or {}
        schedule = Schedule(
            work_start=sched_raw.get("work_start", 9),
            work_end=sched_raw.get("work_end", 18),
            lunch_start=sched_raw.get("lunch_start", 12),
            lunch_end=sched_raw.get("lunch_end", 13),
            days_off=tuple(sched_raw.get("days_off", (5, 6))),
        )
        return cls(
            name=raw["name"],
            started_on=date.fromisoformat(raw["started_on"]),
            pending=list(raw.get("pending", [])),
            done=dict(raw.get("done", {})),
            failed=dict(raw.get("failed", {})),
            ledger=Ledger(actions=list(raw.get("actions", []))),
            daily_cap=raw.get("daily_cap", DEFAULT_DAILY_ACTIONS),
            schedule=schedule,
            warmup=raw.get("warmup", True),
        )


class JobStore:
    """Reads and writes jobs as JSON, one file per job.

    Persisted after every single profile rather than at the end of a bunch: a
    crash or a kill mid-bunch should cost at most one duplicated page view,
    never the day's progress.
    """

    def __init__(self, root: Path | str = "~/.linkedin-mcp/jobs") -> None:
        self.root = Path(root).expanduser()

    def _path(self, name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError(f"Job name {name!r} has no usable characters")
        return self.root / f"{safe}.json"

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def load(self, name: str) -> Job:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(f"No job named {name!r} at {path}")
        return Job.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, job: Job) -> None:
        path = self._path(job.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write leaves the previous good file
        # rather than a truncated one.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)

    def list_jobs(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))
