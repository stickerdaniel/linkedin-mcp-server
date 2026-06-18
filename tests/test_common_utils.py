"""Tests for linkedin_mcp_server.common_utils output helpers."""

import json

import pytest

from linkedin_mcp_server.common_utils import apply_output_mode


def _sample_result() -> dict:
    return {
        "url": "https://www.linkedin.com/jobs/view/12345/",
        "sections": {"job_posting": "Software Engineer\nGreat opportunity"},
        "job_ids": ["12345", "67890"],
    }


class TestApplyOutputMode:
    def test_display_returns_full_result_and_writes_nothing(self, tmp_path):
        result = _sample_result()
        target = tmp_path / "out.json"

        returned = apply_output_mode(result, str(target), "display")

        assert returned is result
        assert not target.exists()

    def test_file_writes_json_and_returns_confirmation(self, tmp_path):
        result = _sample_result()
        target = tmp_path / "job.json"

        returned = apply_output_mode(result, str(target), "file")

        # Confirmation carries section names, not the full sections payload.
        assert returned["saved_path"] == str(target)
        assert returned["url"] == result["url"]
        assert returned["job_ids"] == result["job_ids"]
        assert returned["sections"] == ["job_posting"]
        assert returned["sections"] != result["sections"]

        on_disk = json.loads(target.read_text())
        assert on_disk == result

    def test_file_writes_text_for_non_json_extension(self, tmp_path):
        result = _sample_result()
        target = tmp_path / "job.md"

        apply_output_mode(result, str(target), "file")

        body = target.read_text()
        assert "URL: https://www.linkedin.com/jobs/view/12345/" in body
        assert "## job_posting" in body
        assert "Software Engineer" in body
        assert "JOB_IDS: 12345, 67890" in body

    def test_both_writes_file_and_returns_full_result(self, tmp_path):
        result = _sample_result()
        target = tmp_path / "job.json"

        returned = apply_output_mode(result, str(target), "both")

        assert returned is result
        assert json.loads(target.read_text()) == result

    def test_file_mode_requires_output_path(self):
        with pytest.raises(ValueError, match="output_path is required"):
            apply_output_mode(_sample_result(), None, "file")

    def test_expands_user_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _sample_result()

        returned = apply_output_mode(result, "~/saved-job.json", "file")

        written = tmp_path / "saved-job.json"
        assert written.exists()
        assert returned["saved_path"] == str(written)
