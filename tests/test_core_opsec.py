"""Tests for the daily-cap/warm-up/dedup opsec gate (core.opsec)."""

from datetime import UTC, datetime, timedelta

import pytest

from linkedin_mcp_server.core.opsec import (
    VIEW_PROFILE_WARMUP_SCHEDULE,
    WARMUP_SCHEDULE,
    DailyCapExceededError,
    DuplicateRecipientError,
    OpsecGate,
    get_opsec_gate,
    reset_opsec_gate_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_opsec_gate_for_testing()
    yield
    reset_opsec_gate_for_testing()


@pytest.fixture
def gate(tmp_path):
    return OpsecGate(db_path=tmp_path / "opsec.db")


class TestWarmup:
    def test_fresh_gate_uses_first_tier_limit(self, gate):
        gate.check("connect_with_person", "user-a")  # does not raise

    def test_daily_cap_blocks_after_first_tier_limit(self, gate):
        first_tier_limit = WARMUP_SCHEDULE[0][1]
        for i in range(first_tier_limit):
            gate.record("connect_with_person", f"user-{i}")
        with pytest.raises(DailyCapExceededError):
            gate.check("connect_with_person", "user-overflow")

    def test_ramp_unlocks_higher_limit_after_days(self, tmp_path):
        gate = OpsecGate(db_path=tmp_path / "opsec.db")
        first_tier_limit = WARMUP_SCHEDULE[0][1]
        second_tier_days, second_tier_limit = WARMUP_SCHEDULE[1]

        # Seed a "first use" timestamp far enough in the past to unlock the
        # second warm-up tier, then fill up to (but not past) that tier's cap.
        conn = gate._connect()
        old_ts = (datetime.now(UTC) - timedelta(days=second_tier_days + 1)).isoformat()
        conn.execute(
            "INSERT INTO actions (action, recipient, created_at) VALUES (?, ?, ?)",
            ("connect_with_person", "seed-user", old_ts),
        )
        conn.commit()
        conn.close()

        assert second_tier_limit > first_tier_limit
        # first_tier_limit is already comfortably below second_tier_limit,
        # so this must not raise even though it's above the first tier's cap.
        for i in range(first_tier_limit):
            gate.check("connect_with_person", f"user-{i}")
            gate.record("connect_with_person", f"user-{i}")


class TestDailyCapIsolatedByAction:
    def test_different_actions_have_independent_caps(self, gate):
        first_tier_limit = WARMUP_SCHEDULE[0][1]
        for i in range(first_tier_limit):
            gate.record("connect_with_person", f"user-{i}")
        gate.check("send_message", "user-a")  # independent action, does not raise


class TestViewProfileWarmup:
    """view_person_profile uses its own, higher-ceiling schedule
    (VIEW_PROFILE_WARMUP_SCHEDULE) -- viewing a profile is materially
    lower-risk than connecting/messaging, so it gets more daily headroom,
    while still sharing the same account-wide warm-up TIMELINE as every
    other action (see opsec.py::_daily_limit)."""

    def test_fresh_gate_uses_view_profile_first_tier_limit(self, gate):
        first_tier_limit = VIEW_PROFILE_WARMUP_SCHEDULE[0][1]
        assert first_tier_limit != WARMUP_SCHEDULE[0][1], (
            "test assumes the two schedules' first tiers differ"
        )
        for i in range(first_tier_limit):
            gate.record("view_person_profile", f"user-{i}")
        with pytest.raises(DailyCapExceededError):
            gate.check_daily_cap("view_person_profile")

    def test_view_profile_and_connect_have_independent_caps(self, gate):
        connect_limit = WARMUP_SCHEDULE[0][1]
        for i in range(connect_limit):
            gate.record("connect_with_person", f"user-{i}")
        gate.check_daily_cap("view_person_profile")  # independent, does not raise

    def test_view_profile_cap_allows_more_than_connect_default(self, gate):
        """The whole point of the bespoke schedule: filling connect's
        first-tier cap must NOT block view_person_profile, since its own
        first-tier ceiling is higher."""
        connect_limit = WARMUP_SCHEDULE[0][1]
        view_limit = VIEW_PROFILE_WARMUP_SCHEDULE[0][1]
        assert view_limit > connect_limit
        for i in range(connect_limit):
            gate.record("view_person_profile", f"user-{i}")
        gate.check_daily_cap("view_person_profile")  # still under its own cap

    def test_warmup_timeline_is_shared_across_actions(self, tmp_path):
        """A fresh view_person_profile history still unlocks the ramped-up
        tier if SOME OTHER action already established "first use" long ago
        -- the warm-up clock is account-wide, not per-action (unlike the
        daily cap ceiling itself, which IS per-action)."""
        gate = OpsecGate(db_path=tmp_path / "opsec.db")
        first_tier_limit = VIEW_PROFILE_WARMUP_SCHEDULE[0][1]
        second_tier_days, second_tier_limit = VIEW_PROFILE_WARMUP_SCHEDULE[1]
        assert second_tier_limit > first_tier_limit

        conn = gate._connect()
        old_ts = (datetime.now(UTC) - timedelta(days=second_tier_days + 1)).isoformat()
        conn.execute(
            "INSERT INTO actions (action, recipient, created_at) VALUES (?, ?, ?)",
            ("connect_with_person", "seed-user", old_ts),
        )
        conn.commit()
        conn.close()

        # Above view_person_profile's OWN first-tier cap, but still under
        # its second tier -- only unlocked because of the shared timeline.
        for i in range(first_tier_limit + 1):
            gate.record("view_person_profile", f"user-{i}")
        gate.check_daily_cap("view_person_profile")  # does not raise


class TestDedup:
    def test_check_raises_for_recently_recorded_recipient(self, gate):
        gate.record("connect_with_person", "jane-doe")
        with pytest.raises(DuplicateRecipientError):
            gate.check("connect_with_person", "jane-doe")

    def test_check_allows_a_different_recipient(self, gate):
        gate.record("connect_with_person", "jane-doe")
        gate.check("connect_with_person", "john-doe")  # does not raise

    def test_dedup_is_scoped_per_action(self, gate):
        gate.record("connect_with_person", "jane-doe")
        gate.check("send_message", "jane-doe")  # different action, does not raise

    def test_dedup_expires_outside_the_window(self, tmp_path):
        gate = OpsecGate(db_path=tmp_path / "opsec.db", dedup_window_days=1)
        conn = gate._connect()
        old_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        conn.execute(
            "INSERT INTO actions (action, recipient, created_at) VALUES (?, ?, ?)",
            ("connect_with_person", "jane-doe", old_ts),
        )
        conn.commit()
        conn.close()

        gate.check("connect_with_person", "jane-doe")  # outside the window now


class TestSingleton:
    @pytest.fixture(autouse=True)
    def _redirect_default_db(self, tmp_path, monkeypatch):
        # get_opsec_gate() uses OpsecGate's default db_path (~/.linkedin-mcp/
        # opsec.db) -- redirect it so the singleton tests never touch the
        # real user home directory.
        monkeypatch.setattr(
            "linkedin_mcp_server.core.opsec._DEFAULT_DB_PATH",
            tmp_path / "opsec.db",
        )

    def test_get_opsec_gate_returns_same_instance(self):
        assert get_opsec_gate() is get_opsec_gate()

    def test_reset_creates_a_fresh_instance(self):
        first = get_opsec_gate()
        reset_opsec_gate_for_testing()
        assert get_opsec_gate() is not first
