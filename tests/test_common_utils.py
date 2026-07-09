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
    def test_rejects_absolute_path_outside_export_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        target = tmp_path / "outside" / "job.json"

        with pytest.raises(
            ValueError, match="inside the LinkedIn MCP export directory"
        ):
            apply_output_mode(_sample_result(), str(target), "file")

        assert not target.exists()

    def test_display_returns_full_result_and_writes_nothing(self, tmp_path):
        result = _sample_result()
        target = tmp_path / "out.json"

        returned = apply_output_mode(result, str(target), "display")

        assert returned is result
        assert not target.exists()

    def test_file_writes_json_and_returns_confirmation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _sample_result()
        target = tmp_path / ".linkedin-mcp" / "exports" / "job.json"

        returned = apply_output_mode(result, "job.json", "file")

        # Confirmation carries section names without changing the standard
        # mapping type of the optional `sections` response field.
        assert returned["saved_path"] == str(target)
        assert returned["url"] == result["url"]
        assert returned["job_ids"] == result["job_ids"]
        assert returned["section_names"] == ["job_posting"]
        assert "sections" not in returned

        on_disk = json.loads(target.read_text())
        assert on_disk == result

    def test_file_writes_text_for_non_json_extension(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _sample_result()
        target = tmp_path / ".linkedin-mcp" / "exports" / "job.md"

        apply_output_mode(result, "job.md", "file")

        body = target.read_text()
        assert "URL: https://www.linkedin.com/jobs/view/12345/" in body
        assert "## job_posting" in body
        assert "Software Engineer" in body
        assert "JOB_IDS: 12345, 67890" in body

    def test_both_writes_file_and_returns_full_result_with_path(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _sample_result()
        target = tmp_path / ".linkedin-mcp" / "exports" / "job.json"

        returned = apply_output_mode(result, "job.json", "both")

        assert returned == {**result, "saved_path": str(target)}
        assert "saved_path" not in result
        assert json.loads(target.read_text()) == result

    def test_file_mode_requires_output_path(self):
        with pytest.raises(ValueError, match="output_path is required"):
            apply_output_mode(_sample_result(), None, "file")

    def test_accepts_absolute_path_inside_export_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _sample_result()
        written = tmp_path / ".linkedin-mcp" / "exports" / "saved-job.json"

        returned = apply_output_mode(result, str(written), "file")

        assert written.exists()
        assert returned["saved_path"] == str(written.resolve())

    def test_rejects_parent_traversal_from_export_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        escaped = tmp_path / ".linkedin-mcp" / "outside.json"

        with pytest.raises(
            ValueError, match="inside the LinkedIn MCP export directory"
        ):
            apply_output_mode(_sample_result(), "../outside.json", "file")

        assert not escaped.exists()

    def test_rejects_symlink_escape_from_export_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        export_root = tmp_path / ".linkedin-mcp" / "exports"
        outside = tmp_path / "outside"
        export_root.mkdir(parents=True)
        outside.mkdir()
        (export_root / "escape").symlink_to(outside, target_is_directory=True)

        with pytest.raises(
            ValueError, match="inside the LinkedIn MCP export directory"
        ):
            apply_output_mode(_sample_result(), "escape/job.json", "file")

        assert not (outside / "job.json").exists()

    def test_rejects_symlinked_export_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        linkedin_dir = tmp_path / ".linkedin-mcp"
        outside = tmp_path / "outside"
        linkedin_dir.mkdir()
        outside.mkdir()
        (linkedin_dir / "exports").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="export directory must not be a symlink"):
            apply_output_mode(_sample_result(), "job.json", "file")

        assert not (outside / "job.json").exists()
