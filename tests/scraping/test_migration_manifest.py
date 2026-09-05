"""Extractor seam migration inventory contracts."""

from __future__ import annotations

from pathlib import Path

import json
import logging
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import check_scraping_migration_manifest as migration  # noqa: E402

MANIFEST = ROOT / "tests" / "fixtures" / "scraping-policy" / "migration-manifest.json"
CHECKER = ROOT / "scripts" / "check_scraping_migration_manifest.py"
POLICY_SCENARIOS = ROOT / "tests" / "scraping" / "policy_scenarios.py"

_SHARED_BOUNDARY_STAGES = {
    "detect_rate_limit": {4, 5, 6, 9, 11, 12},
    "handle_modal_close": {4, 5, 6, 9, 11, 12},
    "scroll_to_bottom": {4, 9},
    "scroll_job_sidebar": {9},
    "build_issue_diagnostics": {4, 5, 6, 8, 9},
}


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
        "module_attribute",
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
    current = migration.scan()
    boundary = [
        seam for seam in current["seams"] if seam["kind"] == "boundary_patch_object"
    ]

    assert {
        target: {
            seam["migration_stage"] for seam in boundary if seam["target"] == target
        }
        for target in _SHARED_BOUNDARY_STAGES
    } == _SHARED_BOUNDARY_STAGES
    assert all(seam["migration_stage"] is not None for seam in boundary)

    direct_attributes = [
        seam
        for seam in current["seams"]
        if seam["kind"] == "module_attribute"
        and seam["target"] in {"scroll_to_bottom", "scroll_job_sidebar"}
    ]
    assert {
        target: {
            seam["migration_stage"]
            for seam in direct_attributes
            if seam["target"] == target
        }
        for target in ("scroll_to_bottom", "scroll_job_sidebar")
    } == {
        target: _SHARED_BOUNDARY_STAGES[target]
        for target in ("scroll_to_bottom", "scroll_job_sidebar")
    }

    imported_patches = [
        seam for seam in current["seams"] if seam["kind"] == "imported_module_patch"
    ]
    stdlib = [
        seam
        for seam in imported_patches
        if seam["target"].split(".", 1)[0] in {"asyncio", "time"}
    ]
    logger_patches = [
        seam for seam in imported_patches if seam["target"].split(".", 1)[0] == "logger"
    ]
    assert stdlib
    assert all(seam["migration_stage"] is None for seam in stdlib)
    assert {seam["migration_stage"] for seam in logger_patches} == {6}
    assert all(
        "person.PersonScraper" in seam["canonical_owner"] for seam in logger_patches
    )


@pytest.mark.parametrize(
    ("target", "expected_stages"),
    [
        (target, stages)
        for target, stages in _SHARED_BOUNDARY_STAGES.items()
        if len(stages) > 1
    ],
)
def test_shared_boundary_patches_retain_later_consumers_after_early_migration(
    monkeypatch, target, expected_stages
):
    key = ("tests/scraping/policy_scenarios.py", "boundaries", target)
    owners = migration._WORKFLOW_OWNERS | migration._PRIVATE_OWNERS
    consumers = migration._EXPLICIT_CALLER_CONTEXTS[key]
    earliest_stage = min(owners[name][1] for name in consumers)
    remaining = tuple(name for name in consumers if owners[name][1] != earliest_stage)
    monkeypatch.setitem(migration._EXPLICIT_CALLER_CONTEXTS, key, remaining)

    publics, privates = migration.extractor_methods()
    seams = migration.scan_source(
        POLICY_SCENARIOS,
        POLICY_SCENARIOS.read_text(encoding="utf-8"),
        publics,
        privates,
    )

    assert {
        seam.migration_stage
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == target
    } == expected_stages - {earliest_stage}
    if target == "scroll_to_bottom":
        assert {
            seam.migration_stage
            for seam in seams
            if seam.kind == "module_attribute" and seam.target == target
        } == expected_stages - {earliest_stage}


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


def _scan_synthetic(source: str, *, path: Path | None = None) -> list[migration.Seam]:
    return migration.scan_source(
        path or ROOT / "tests" / "synthetic_inventory.py",
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


def test_direct_module_attributes_follow_their_canonical_bindings():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy_surface

async def boundaries(tasks):
    real_drain = legacy_surface._drain_listener_tasks
    real_scroll_body = legacy_surface.scroll_to_bottom
    real_scroll_sidebar = legacy_surface.scroll_job_sidebar
    recipient_selector = legacy_surface._MESSAGING_RECIPIENT_PICKER_SELECTOR
    compose_selectors = legacy_surface._MESSAGING_COMPOSE_FALLBACK_SELECTORS
    close_selector = legacy_surface._MESSAGING_CLOSE_SELECTOR
    settle_lag = legacy_surface._URL_SETTLE_LAG
    settle_quiet = legacy_surface._URL_SETTLE_QUIET
    await real_drain(tasks)
""",
        path=ROOT / "tests" / "scraping" / "policy_scenarios.py",
    )
    attributes = [seam for seam in seams if seam.kind == "module_attribute"]

    def matching(target: str) -> list[migration.Seam]:
        return [seam for seam in attributes if seam.target == target]

    assert {seam.migration_stage for seam in matching("_drain_listener_tasks")} == {5}
    assert {seam.canonical_owner for seam in matching("_drain_listener_tasks")} == {
        "feed.FeedScraper"
    }
    assert {seam.migration_stage for seam in matching("scroll_to_bottom")} == {4, 9}
    assert {seam.migration_stage for seam in matching("scroll_job_sidebar")} == {9}
    assert {seam.migration_stage for seam in matching("_URL_SETTLE_LAG")} == {3}
    assert {seam.canonical_owner for seam in matching("_URL_SETTLE_LAG")} == {
        "navigation.PageNavigator.URL_SETTLE_LAG"
    }
    assert {seam.migration_stage for seam in matching("_URL_SETTLE_QUIET")} == {3}
    assert {seam.canonical_owner for seam in matching("_URL_SETTLE_QUIET")} == {
        "navigation.PageNavigator.URL_SETTLE_QUIET"
    }
    assert {
        seam.migration_stage
        for seam in matching("_MESSAGING_RECIPIENT_PICKER_SELECTOR")
    } == {12}
    assert {
        seam.migration_stage
        for seam in matching("_MESSAGING_COMPOSE_FALLBACK_SELECTORS")
    } == {12}
    assert {seam.migration_stage for seam in matching("_MESSAGING_CLOSE_SELECTOR")} == {
        12
    }


def test_direct_private_helper_calls_and_stage_gate_are_inventoried():
    current = migration.scan()
    drains = [
        seam
        for seam in current["seams"]
        if seam["kind"] == "module_attribute"
        and seam["target"] == "_drain_listener_tasks"
    ]

    assert {seam["path"] for seam in drains} == {
        "tests/scraping/policy_scenarios.py",
        "tests/scraping/test_policy_trace_support.py",
    }
    assert all(seam["migration_stage"] == 5 for seam in drains)

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "5"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "module_attribute _drain_listener_tasks -> feed.FeedScraper" in result.stderr


def test_permanent_alias_module_attributes_keep_identity_compatibility():
    expressions = "\n".join(
        f"same_{name} = legacy_surface.{name}" for name in migration.PERMANENT_ALIASES
    )
    seams = _scan_synthetic(
        "from linkedin_mcp_server.scraping import extractor as legacy_surface\n"
        + expressions
    )
    permanent = [
        seam
        for seam in seams
        if seam.kind == "module_attribute"
        and seam.target in migration.PERMANENT_ALIASES
    ]

    assert {seam.target for seam in permanent} == set(migration.PERMANENT_ALIASES)
    assert all(seam.migration_stage is None for seam in permanent)


def test_logger_patch_forms_follow_the_consuming_workflow():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_sidebar(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface.logger, "debug"):
        await extractor.get_sidebar_profiles("example")
    with patch("linkedin_mcp_server.scraping.extractor.logger.debug"):
        await extractor.get_sidebar_profiles("example")
"""
    )
    logger_patches = [
        seam
        for seam in seams
        if seam.kind == "imported_module_patch" and "logger" in seam.target
    ]

    assert len(logger_patches) == 2
    assert {seam.migration_stage for seam in logger_patches} == {6}
    assert all(
        "person.PersonScraper -> owner-local logger binding" in seam.canonical_owner
        for seam in logger_patches
    )
    assert not any(
        seam.kind == "module_attribute" and seam.target == "logger" for seam in seams
    )
    assert logging.getLogger(
        "linkedin_mcp_server.scraping.extractor"
    ) is not logging.getLogger("linkedin_mcp_server.scraping.person")


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


def test_unknown_direct_extractor_module_attributes_fail_closed():
    source = """
from linkedin_mcp_server.scraping import extractor as legacy_surface

unknown = legacy_surface._unknown_extractor_target
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match="unknown direct extractor module attribute",
    ):
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
