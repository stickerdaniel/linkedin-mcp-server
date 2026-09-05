"""Tests for the cross-process minimum tool-call interval gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from linkedin_mcp_server.tool_interval import (
    remaining_wait_seconds,
    try_claim_start,
    write_last_start_wall,
)


class TestRemainingWaitSeconds:
    def test_disabled_interval_never_waits(self):
        assert (
            remaining_wait_seconds(interval=0, last_start_wall=100.0, now_wall=100.5)
            == 0.0
        )

    def test_first_call_never_waits(self):
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=None, now_wall=100.0)
            == 0.0
        )

    def test_reports_remaining_time(self):
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=100.0, now_wall=105.0)
            == 10.0
        )

    def test_elapsed_interval_is_ready(self):
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=100.0, now_wall=120.0)
            == 0.0
        )

    def test_future_stamp_beyond_skew_is_ignored(self):
        # More than an hour ahead of wall time: treat as corrupt and reclaim.
        assert (
            remaining_wait_seconds(
                interval=15, last_start_wall=100.0 + 3601.0, now_wall=100.0
            )
            == 0.0
        )

    def test_small_clock_rollback_waits_only_the_skew(self):
        # Stamp is 2s ahead of now. Must not grant immediately, but must not
        # restart a full interval on every retry either — catch up by the skew.
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=102.0, now_wall=100.0)
            == 2.0
        )

    def test_multi_minute_clock_rollback_catches_up_in_interval_steps(self):
        # Ten minutes backwards: each retry advances by at most ``interval``,
        # so the wait budget is not burned on identical full-interval sleeps.
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=700.0, now_wall=100.0)
            == 15.0
        )
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=700.0, now_wall=115.0)
            == 15.0
        )
        assert (
            remaining_wait_seconds(interval=15, last_start_wall=700.0, now_wall=690.0)
            == 10.0
        )


class TestTryClaimStart:
    def test_claims_and_blocks_until_interval(self, tmp_path: Path, monkeypatch):
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        monkeypatch.setattr(
            "linkedin_mcp_server.tool_interval.time.time", lambda: 1000.0
        )

        assert try_claim_start(auth_root, 15.0) == 0.0
        stamp = json.loads((auth_root / "tool-interval.json").read_text())
        assert stamp["last_start_wall"] == 1000.0

        monkeypatch.setattr(
            "linkedin_mcp_server.tool_interval.time.time", lambda: 1005.0
        )
        assert try_claim_start(auth_root, 15.0) == pytest.approx(10.0)

        monkeypatch.setattr(
            "linkedin_mcp_server.tool_interval.time.time", lambda: 1015.0
        )
        assert try_claim_start(auth_root, 15.0) == 0.0

    def test_disabled_interval_skips_files(self, tmp_path: Path):
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        assert try_claim_start(auth_root, 0.0) == 0.0
        assert not (auth_root / "tool-interval.json").exists()

    def test_two_auth_roots_are_independent(self, tmp_path: Path, monkeypatch):
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()
        monkeypatch.setattr("linkedin_mcp_server.tool_interval.time.time", lambda: 50.0)
        assert try_claim_start(one, 10.0) == 0.0
        assert try_claim_start(two, 10.0) == 0.0
        monkeypatch.setattr("linkedin_mcp_server.tool_interval.time.time", lambda: 55.0)
        # Each root tracks its own stamp: both still have 5s remaining.
        assert try_claim_start(one, 10.0) == pytest.approx(5.0)
        assert try_claim_start(two, 10.0) == pytest.approx(5.0)
        # Advancing only one's clock still leaves the other waiting.
        write_last_start_wall(one, 40.0)
        assert try_claim_start(one, 10.0) == 0.0
        assert try_claim_start(two, 10.0) == pytest.approx(5.0)

    def test_force_claim_overwrites_future_stamp(self, tmp_path: Path, monkeypatch):
        from linkedin_mcp_server.tool_interval import force_claim_start

        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        write_last_start_wall(auth_root, 2000.0)
        monkeypatch.setattr(
            "linkedin_mcp_server.tool_interval.time.time", lambda: 1000.0
        )
        force_claim_start(auth_root)
        stamp = json.loads((auth_root / "tool-interval.json").read_text())
        assert stamp["last_start_wall"] == 1000.0
