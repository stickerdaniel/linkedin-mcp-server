"""Tests for the bulk-job pacing arithmetic.

Every function under test takes ``now`` explicitly, so nothing here sleeps.
"""

from datetime import date, datetime, timedelta

import pytest

from linkedin_mcp_server.pacing import (
    MAX_BUNCH_PAUSE,
    MIN_BUNCH_PAUSE,
    WINDOW_SECONDS,
    Job,
    JobStore,
    Ledger,
    Schedule,
    jittered_cap,
    next_bunch_delay,
    warmup_cap,
)

# 2026-08-05 is a Wednesday; 2026-08-08 a Saturday.
WED_10AM = datetime(2026, 8, 5, 10, 0)
WED_NOON = datetime(2026, 8, 5, 12, 30)
WED_3AM = datetime(2026, 8, 5, 3, 0)
WED_8PM = datetime(2026, 8, 5, 20, 0)
SAT_10AM = datetime(2026, 8, 8, 10, 0)


class TestSchedule:
    def test_open_during_working_hours(self):
        assert Schedule().is_open(WED_10AM)

    def test_closed_overnight(self):
        assert not Schedule().is_open(WED_3AM)
        assert not Schedule().is_open(WED_8PM)

    def test_closed_at_lunch(self):
        assert not Schedule().is_open(WED_NOON)

    def test_closed_at_weekend(self):
        assert not Schedule().is_open(SAT_10AM)

    def test_next_open_returns_now_when_already_open(self):
        assert Schedule().next_open(WED_10AM) == WED_10AM

    def test_next_open_skips_to_morning(self):
        opens = Schedule().next_open(WED_3AM)
        assert opens.hour == 9
        assert opens.date() == WED_3AM.date()

    def test_next_open_skips_lunch(self):
        opens = Schedule().next_open(WED_NOON)
        assert opens.hour == 13

    def test_next_open_skips_the_weekend(self):
        opens = Schedule().next_open(SAT_10AM)
        assert opens.weekday() == 0  # Monday
        assert opens.hour == 9

    def test_next_open_after_close_lands_next_morning(self):
        opens = Schedule().next_open(WED_8PM)
        assert opens.day == WED_8PM.day + 1
        assert opens.hour == 9

    def test_seconds_until_close_excludes_lunch_still_ahead(self):
        # 10:00 -> 18:00 is 8h, minus the 1h lunch not yet taken.
        assert Schedule().seconds_until_close(WED_10AM) == 7 * 3600

    def test_seconds_until_close_keeps_afternoon_whole(self):
        # 14:00 is past lunch, so nothing is deducted.
        afternoon = datetime(2026, 8, 5, 14, 0)
        assert Schedule().seconds_until_close(afternoon) == 4 * 3600

    def test_seconds_until_close_is_zero_when_shut(self):
        assert Schedule().seconds_until_close(SAT_10AM) == 0.0

    def test_a_schedule_that_never_opens_is_rejected(self):
        every_day_off = Schedule(days_off=(0, 1, 2, 3, 4, 5, 6))
        with pytest.raises(ValueError, match="never opens"):
            every_day_off.next_open(WED_10AM)


class TestLedger:
    def test_actions_age_out_after_24h(self):
        ledger = Ledger()
        ledger.record(WED_10AM)
        # One second past the window.
        later = WED_10AM + timedelta(seconds=WINDOW_SECONDS + 1)
        assert ledger.spent(later) == 0

    def test_actions_inside_the_window_still_count(self):
        ledger = Ledger()
        ledger.record(WED_10AM)
        later = WED_10AM + timedelta(hours=23)
        assert ledger.spent(later) == 1

    def test_budget_refills_gradually_not_at_midnight(self):
        """The rolling window is the whole point.

        Ten actions spent over ten minutes do not all free up at 00:00, and
        they do not all free up at once either -- each returns on its own 24h
        anniversary. A midnight-reset budget would let this job spend twice in
        two minutes across the boundary, which is the exact burst shape that
        gets accounts flagged.
        """
        ledger = Ledger()
        for minute in range(10):
            ledger.record(WED_10AM + timedelta(minutes=minute))

        just_before_midnight = datetime(2026, 8, 5, 23, 59)
        assert ledger.remaining(just_before_midnight, cap=10) == 0

        # One minute past the first anniversary: only the two oldest are back.
        assert ledger.remaining(WED_10AM + timedelta(hours=24, minutes=1), 10) == 2

        # Ten minutes past, and the whole batch has aged out.
        assert ledger.remaining(WED_10AM + timedelta(hours=24, minutes=10), 10) == 10

    def test_next_expiry_is_when_the_oldest_ages_out(self):
        ledger = Ledger()
        ledger.record(WED_10AM)
        one_hour_in = WED_10AM + timedelta(hours=1)
        assert ledger.next_expiry(one_hour_in) == pytest.approx(23 * 3600, abs=1)

    def test_next_expiry_is_zero_when_empty(self):
        assert Ledger().next_expiry(WED_10AM) == 0.0


class TestWarmup:
    @pytest.mark.parametrize(
        ("day", "expected"),
        [(0, 10), (6, 10), (7, 20), (13, 20), (14, 50), (20, 50), (21, 100)],
    )
    def test_ramp_steps(self, day, expected):
        start = date(2026, 8, 5)
        assert warmup_cap(100, start, start + timedelta(days=day)) == expected

    def test_ramp_never_exceeds_the_configured_cap(self):
        start = date(2026, 8, 5)
        assert warmup_cap(5, start, start + timedelta(days=30)) == 5

    def test_clock_skew_backwards_is_treated_as_day_zero(self):
        start = date(2026, 8, 5)
        assert warmup_cap(100, start, start - timedelta(days=3)) == 10


class TestJitteredCap:
    def test_is_stable_within_a_day(self):
        day = date(2026, 8, 5)
        assert jittered_cap(100, day, "job") == jittered_cap(100, day, "job")

    def test_varies_across_days(self):
        caps = {
            jittered_cap(100, date(2026, 8, 5) + timedelta(days=d), "job")
            for d in range(14)
        }
        assert len(caps) > 1, "a constant daily total is the pattern to avoid"

    def test_stays_within_a_sane_band(self):
        for d in range(60):
            cap = jittered_cap(100, date(2026, 8, 5) + timedelta(days=d), "job")
            assert 85 <= cap <= 100

    def test_never_rounds_down_to_zero(self):
        assert jittered_cap(1, date(2026, 8, 5), "job") >= 1


class TestNextBunchDelay:
    def test_spreads_budget_across_the_remaining_window(self):
        # 7 usable hours, 100 left, bunches of 5 -> 20 bunches -> ~21 min.
        delay = next_bunch_delay(100, 5, WED_10AM, Schedule())
        assert MIN_BUNCH_PAUSE <= delay <= MAX_BUNCH_PAUSE
        assert 15 * 60 <= delay <= 27 * 60

    def test_backs_right_off_when_budget_is_gone(self):
        assert next_bunch_delay(0, 5, WED_10AM, Schedule()) == MAX_BUNCH_PAUSE

    def test_backs_right_off_when_the_window_is_shut(self):
        assert next_bunch_delay(50, 5, SAT_10AM, Schedule()) == MAX_BUNCH_PAUSE

    def test_a_tiny_remaining_budget_still_respects_the_floor(self):
        # One bunch left across 7 hours would otherwise suggest a 7-hour wait;
        # the clamp keeps it bounded.
        delay = next_bunch_delay(1, 5, WED_10AM, Schedule())
        assert delay <= MAX_BUNCH_PAUSE


class TestJobRoundTrip:
    def test_survives_serialization(self):
        job = Job(
            name="egypt-gulf",
            started_on=date(2026, 8, 5),
            pending=["a", "b"],
            done={"c": {"url": "x"}},
            failed={"d": "boom"},
            ledger=Ledger(actions=[1.0, 2.0]),
            daily_cap=80,
            schedule=Schedule(work_start=8, work_end=17, days_off=(6,)),
            warmup=False,
        )
        restored = Job.from_dict(job.to_dict())
        assert restored.to_dict() == job.to_dict()
        assert restored.schedule.work_start == 8
        assert restored.schedule.days_off == (6,)
        assert restored.warmup is False

    def test_effective_cap_applies_warmup_then_jitter(self):
        job = Job(name="j", started_on=date(2026, 8, 5), daily_cap=100)
        # Day 0 of the ramp caps at 10, jitter can only shave it.
        assert 1 <= job.effective_cap(WED_10AM) <= 10

    def test_effective_cap_is_bounded_by_the_global_ceiling(self):
        job = Job(name="j", started_on=date(2020, 1, 1), daily_cap=150, warmup=False)
        assert job.effective_cap(WED_10AM) <= 150


class TestJobStore:
    def test_save_and_load(self, tmp_path):
        store = JobStore(tmp_path / "jobs")
        job = Job(name="egypt-gulf", started_on=date(2026, 8, 5), pending=["a"])
        store.save(job)
        assert store.exists("egypt-gulf")
        assert store.load("egypt-gulf").pending == ["a"]

    def test_missing_job_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            JobStore(tmp_path / "jobs").load("nope")

    def test_list_is_empty_before_anything_is_written(self, tmp_path):
        assert JobStore(tmp_path / "unborn").list_jobs() == []

    def test_job_names_cannot_escape_the_store_directory(self, tmp_path):
        store = JobStore(tmp_path / "jobs")
        job = Job(name="../../etc/passwd", started_on=date(2026, 8, 5))
        store.save(job)
        assert not (tmp_path / "etc").exists()
        assert store.list_jobs() == ["etcpasswd"]

    def test_a_name_with_nothing_usable_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="no usable characters"):
            JobStore(tmp_path / "jobs").load("../..")

    def test_no_tmp_file_is_left_behind(self, tmp_path):
        store = JobStore(tmp_path / "jobs")
        store.save(Job(name="j", started_on=date(2026, 8, 5)))
        assert list((tmp_path / "jobs").glob("*.tmp")) == []
