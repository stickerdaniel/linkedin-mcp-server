"""Extractor seam migration inventory contracts."""

from __future__ import annotations

from pathlib import Path

import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import check_scraping_migration_manifest as migration  # noqa: E402

MANIFEST = ROOT / "tests" / "fixtures" / "scraping-policy" / "migration-manifest.json"
CHECKER = ROOT / "scripts" / "check_scraping_migration_manifest.py"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_manifest_matches_every_current_extractor_seam():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check"],
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
        "module_rebind_patch",
        "public_patch_object",
        "imported_module_patch",
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


def test_module_boundary_patches_follow_their_callers():
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    boundary = [
        seam for seam in current["seams"] if seam["kind"] == "boundary_patch_object"
    ]

    assert {
        "record_page_trace",
        "detect_auth_barrier",
        "detect_rate_limit",
        "handle_modal_close",
        "scroll_to_bottom",
        "scroll_job_sidebar",
    } <= {seam["target"] for seam in boundary}
    assert {
        seam["migration_stage"]
        for seam in boundary
        if seam["target"] == "detect_rate_limit"
    } == {4}
    assert all(seam["migration_stage"] is not None for seam in boundary)

    stdlib = [
        seam for seam in current["seams"] if seam["kind"] == "imported_module_patch"
    ]
    assert stdlib
    assert all(seam["migration_stage"] is None for seam in stdlib)


def test_public_facade_patches_follow_each_calling_workflow():
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    public = [
        seam for seam in current["seams"] if seam["kind"] == "public_patch_object"
    ]

    assert {
        seam["migration_stage"] for seam in public if seam["target"] == "extract_page"
    } == {6, 7, 8, 9, 10, 11, 12}
    assert {
        seam["migration_stage"] for seam in public if seam["target"] == "scrape_person"
    } == {6, 7}
    assert {
        seam["migration_stage"]
        for seam in public
        if seam["target"] == "click_button_by_text"
    } == {7}


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


def _scan_synthetic(source: str) -> list[migration.Seam]:
    return migration.scan_source(
        ROOT / "tests" / "synthetic_inventory.py",
        source,
        frozenset(
            {
                "extract_page",
                "extract_feed",
                "scrape_person",
                "scrape_company",
                "search_posts",
            }
        ),
        frozenset({"_navigate_to_page"}),
    )


def test_caller_resolution_ignores_test_class_names():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

class ArbitraryRenamedContainer:
    async def renamed_test(self, page):
        extractor = LinkedInExtractor(page)
        with patch.object(extractor, "extract_page"):
            await extractor.scrape_person("person")
            await extractor.scrape_company("company")
            await extractor.search_posts("query")
"""
    )

    assert {
        seam.migration_stage for seam in seams if seam.kind == "public_patch_object"
    } == {
        6,
        8,
        10,
    }


def test_string_boundary_and_module_rebinds_follow_binding_semantics():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as extractor_module
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_bindings(page):
    extractor = LinkedInExtractor(page)
    with patch("linkedin_mcp_server.scraping.extractor.detect_rate_limit"):
        await extractor.search_posts("query")
    with patch.object(extractor_module, "time", object()):
        await extractor.extract_feed()
    with patch.object(extractor_module.time, "monotonic"):
        await extractor.scrape_company("company")
"""
    )
    by_kind = {seam.kind: seam for seam in seams if seam.kind != "module_alias"}

    assert by_kind["string_patch"].migration_stage == 10
    assert by_kind["module_rebind_patch"].migration_stage == 5
    assert by_kind["imported_module_patch"].migration_stage is None


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("unknown_method", "unknown public facade patch"),
        ("extract_page", "public facade patch has no caller workflow"),
    ],
)
def test_unresolved_public_facade_patches_fail(target, message):
    source = f"""
from unittest.mock import patch
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_unknown(page):
    extractor = LinkedInExtractor(page)
    with patch.object(extractor, {target!r}):
        pass
"""

    with pytest.raises(migration.UnresolvedSeamError, match=message):
        _scan_synthetic(source)


def test_unknown_extractor_module_object_patches_fail():
    source = """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as extractor_module

with patch.object(extractor_module.unknown_dependency, "method"):
    pass
"""

    with pytest.raises(
        migration.UnresolvedSeamError, match="unknown extractor module object patch"
    ):
        _scan_synthetic(source)


def test_completed_stage_is_derived_from_modules_then_ast_gates(tmp_path):
    scraping = tmp_path / "scraping"
    scraping.mkdir()

    for expected, modules in migration._STAGE_MODULES.items():
        for module in modules:
            (scraping / module).write_text("", encoding="utf-8")
        if expected == 1:
            assert "job_policy.py" in modules
            (scraping / "job_policy.py").unlink()
            assert migration.completed_stage(tmp_path) == 0
            (scraping / "job_policy.py").write_text("", encoding="utf-8")
        assert migration.completed_stage(tmp_path) == expected

    (scraping / "capture.py").write_text(
        "class CaptureMode:\n    DEFAULT = 'default'\n", encoding="utf-8"
    )
    assert migration.completed_stage(tmp_path) == 12
    for module in ("fields.py", "person.py", "company.py", "jobs.py", "posts.py"):
        (scraping / module).write_text("mode = CaptureMode.DEFAULT\n", encoding="utf-8")
    assert migration.completed_stage(tmp_path) == 13

    methods = "\n".join(
        f"    def {name}(self):\n        pass"
        for name in sorted(migration._TOOL_METHODS)
    )
    (scraping / "extractor.py").write_text(
        f"class LinkedInExtractor:\n    def __init__(self):\n        pass\n{methods}\n",
        encoding="utf-8",
    )
    assert migration.completed_stage(tmp_path) == 14


def test_stage_override_cannot_weaken_tree_derived_gate():
    with pytest.raises(ValueError, match="below tree-derived completed stage 4"):
        migration.effective_stage(4, 3)

    assert migration.effective_stage(4, None) == 4
    assert migration.effective_stage(4, 5) == 5


def test_checker_generates_only_outside_canonical_fixture_tree(tmp_path):
    output = tmp_path / "candidate.json"
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 2

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(MANIFEST)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert (
        "refusing to write generated output inside canonical fixture" in result.stderr
    )
