"""Tests for per-scrape telemetry buffering/batched flush (core/telemetry.py)."""

import json

from linkedin_mcp_server.core.telemetry import (
    ScrapeTelemetry,
    get_telemetry,
    reset_telemetry_for_testing,
)


class TestScrapeTelemetryBuffering:
    def test_record_buffers_without_writing_to_disk(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=20)

        telemetry.record(
            action="scrape_person",
            profile_name="janedoe",
            duration_seconds=1.5,
            success=True,
        )

        assert len(telemetry) == 1
        assert not path.exists()

    def test_auto_flushes_once_threshold_reached(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=3)

        for i in range(3):
            telemetry.record(
                action="scrape_person",
                profile_name=f"user{i}",
                duration_seconds=1.0,
                success=True,
            )

        assert len(telemetry) == 0  # buffer cleared by the auto-flush
        assert path.exists()
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_does_not_flush_below_threshold(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=5)

        for i in range(4):
            telemetry.record(
                action="scrape_person",
                profile_name=f"user{i}",
                duration_seconds=1.0,
                success=True,
            )

        assert len(telemetry) == 4
        assert not path.exists()


class TestScrapeTelemetryFlush:
    def test_flush_writes_one_json_line_per_record(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=100)
        telemetry.record(
            action="scrape_person",
            profile_name="janedoe",
            duration_seconds=2.5,
            success=True,
        )
        telemetry.record(
            action="scrape_person",
            profile_name="johndoe",
            duration_seconds=0.8,
            success=False,
            error="ChallengeError: soft block",
        )

        telemetry.flush()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["profile_name"] == "janedoe"
        assert first["success"] is True
        assert first["error"] is None
        second = json.loads(lines[1])
        assert second["profile_name"] == "johndoe"
        assert second["success"] is False
        assert second["error"] == "ChallengeError: soft block"

    def test_flush_clears_the_buffer(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=100)
        telemetry.record(
            action="scrape_person",
            profile_name="janedoe",
            duration_seconds=1.0,
            success=True,
        )

        telemetry.flush()

        assert len(telemetry) == 0

    def test_flush_appends_across_multiple_calls(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=100)
        telemetry.record(
            action="scrape_person", profile_name="a", duration_seconds=1.0, success=True
        )
        telemetry.flush()
        telemetry.record(
            action="scrape_person", profile_name="b", duration_seconds=1.0, success=True
        )
        telemetry.flush()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_flush_on_empty_buffer_is_a_no_op(self, tmp_path):
        path = tmp_path / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=100)

        telemetry.flush()  # must not raise, must not create the file

        assert not path.exists()

    def test_flush_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "telemetry.jsonl"
        telemetry = ScrapeTelemetry(path=path, flush_every=100)
        telemetry.record(
            action="scrape_person", profile_name="a", duration_seconds=1.0, success=True
        )

        telemetry.flush()

        assert path.exists()

    def test_flush_failure_is_swallowed_not_raised(self, tmp_path):
        """A write failure (e.g. path collides with an existing file, not a
        directory) must never propagate -- telemetry is purely
        observational and must not break a real scrape."""
        blocking_file = tmp_path / "not_a_directory"
        blocking_file.write_text("x")
        bad_path = blocking_file / "telemetry.jsonl"  # parent isn't a dir
        telemetry = ScrapeTelemetry(path=bad_path, flush_every=100)
        telemetry.record(
            action="scrape_person", profile_name="a", duration_seconds=1.0, success=True
        )

        telemetry.flush()  # must not raise

        assert len(telemetry) == 0  # buffer cleared even though the write failed


class TestGetTelemetrySingleton:
    def test_returns_the_same_instance_across_calls(self):
        reset_telemetry_for_testing()
        try:
            assert get_telemetry() is get_telemetry()
        finally:
            reset_telemetry_for_testing()

    def test_reset_creates_a_fresh_instance(self):
        first = get_telemetry()
        reset_telemetry_for_testing()
        second = get_telemetry()
        try:
            assert first is not second
        finally:
            reset_telemetry_for_testing()
