"""Extractor seam migration inventory contracts."""

from __future__ import annotations

from pathlib import Path

import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "fixtures" / "scraping-policy" / "migration-manifest.json"
CHECKER = ROOT / "scripts" / "check_scraping_migration_manifest.py"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_manifest_matches_every_current_extractor_seam():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "0"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stderr
    assert current["extractor_parent"] == ("c5e5e5a6b142e910374b7b26558addfd49ed7f84")
    assert current["seams"]
    assert {
        "string_patch",
        "module_alias",
        "direct_import",
        "permanent_alias_import",
        "private_patch_object",
        "boundary_patch_object",
    } <= {seam["kind"] for seam in current["seams"]}
    assert all(seam["canonical_owner"] for seam in current["seams"])
    assert not {
        "owner-local service",
        "extractor compatibility surface",
        "extractor migration owner",
    } & {seam["canonical_owner"] for seam in current["seams"]}
    assert all(
        seam["migration_stage"] is None or seam["migration_stage"] >= 1
        for seam in current["seams"]
    )


def test_manifest_covers_production_callers_not_only_tests():
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    production = {
        seam["path"]
        for seam in current["seams"]
        if seam["path"].startswith("linkedin_mcp_server/")
    }

    assert production == {
        "linkedin_mcp_server/tools/company.py",
        "linkedin_mcp_server/tools/feed.py",
        "linkedin_mcp_server/tools/person.py",
        "linkedin_mcp_server/tools/post.py",
    }
    assert {
        seam["target"]
        for seam in current["seams"]
        if seam["path"].startswith("linkedin_mcp_server/")
    } == {"_RATE_LIMITED_MSG", "rate_limited_section_error", "FilterValidationError"}


def test_module_boundary_patches_carry_their_own_owner():
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    boundary = {
        seam["target"]: seam
        for seam in current["seams"]
        if seam["kind"] == "boundary_patch_object"
    }

    # The trace harness patches these on the extractor module. Each one stops
    # intercepting once its caller moves, so each needs its own stage rather
    # than hiding inside the single generic module-alias entry.
    assert {
        "record_page_trace",
        "detect_auth_barrier",
        "detect_rate_limit",
        "handle_modal_close",
        "scroll_to_bottom",
        "scroll_job_sidebar",
    } <= set(boundary)
    assert boundary["detect_rate_limit"]["migration_stage"] == 3
    assert all(seam["migration_stage"] is not None for seam in boundary.values())

    # A patch reaching a standard-library module through the extractor is
    # unaffected by relocation and carries no stage.
    stdlib = [
        seam for seam in current["seams"] if seam["kind"] == "imported_module_patch"
    ]
    assert stdlib
    assert all(seam["migration_stage"] is None for seam in stdlib)


def test_permanent_aliases_never_go_obsolete():
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    permanent = [
        seam for seam in current["seams"] if seam["kind"] == "permanent_alias_import"
    ]

    assert {seam["target"] for seam in permanent} == {
        "ExtractedSection",
        "FilterValidationError",
        "rate_limited_section_error",
        "strip_linkedin_noise",
        "strip_conversation_chrome",
    }
    assert all(seam["migration_stage"] is None for seam in permanent)

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "15"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert "permanent_alias_import" not in result.stderr


def test_manifest_is_canonical_portable_json():
    raw = MANIFEST.read_bytes()
    value = json.loads(raw.decode("utf-8"))

    assert raw.endswith(b"\n")
    assert raw.decode("utf-8") == canonical_json(value)
    assert str(Path.home()) not in raw.decode("utf-8")


def test_checker_rejects_obsolete_seams_at_their_migration_stage():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "1"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "obsolete at stage 1:" in result.stderr
    assert "direct_import" in result.stderr


def test_checkers_offer_no_fixture_update_mode():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--update"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --update" in result.stderr
