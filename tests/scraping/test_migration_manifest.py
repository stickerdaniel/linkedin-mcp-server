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
SCRAPING_SYNTHETIC = (
    ROOT / "linkedin_mcp_server" / "scraping" / "synthetic_inventory.py"
)

_SHARED_BOUNDARY_STAGES = {
    "detect_rate_limit": {6, 9, 11, 12},
    "handle_modal_close": {6, 9, 11, 12},
    "scroll_to_bottom": {9},
    "scroll_job_sidebar": {9},
    "build_issue_diagnostics": {6, 8, 9},
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
    assert current["extractor_parent"] == ("70e50ada68b9389f8d315df6ab1e56c08f6c985b")
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
        "private_facade_access",
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
        "linkedin_mcp_server/dependencies.py",
        "linkedin_mcp_server/scraping/__init__.py",
        "linkedin_mcp_server/tools/company.py",
        "linkedin_mcp_server/tools/feed.py",
        "linkedin_mcp_server/tools/person.py",
        "linkedin_mcp_server/tools/post.py",
    }
    assert {
        seam["target"]
        for seam in current["seams"]
        if seam["path"].startswith("linkedin_mcp_server/")
    } == {"rate_limited_section_error", "FilterValidationError", "LinkedInExtractor"}
    assert all(
        seam["kind"] == "permanent_alias_import"
        for seam in current["seams"]
        if seam["path"].startswith("linkedin_mcp_server/")
        and seam["target"] != "LinkedInExtractor"
    )


def test_final_messaging_seams_have_only_the_approved_stage_owners():
    current = migration.scan()["seams"]
    browser_free = {
        seam["target"]: (seam["canonical_owner"], seam["migration_stage"])
        for seam in current
        if seam["path"] == "linkedin_mcp_server/tools/messaging.py"
    }
    # The tool imports both browser-free contracts from their owner. Keeping
    # either on the extractor facade would create a Stage 1 seam that can go
    # stale when the facade is decomposed.
    assert browser_free == {}

    message_paths = {
        "tests/test_message_recipient_dom.py",
        "tests/test_send_message_confirmation_dom.py",
    }
    dom_seams = [
        seam
        for seam in current
        if seam["path"] in message_paths
        and seam["target"]
        not in {"LinkedInExtractor", "_navigate_to_page", "_extract_profile_urn"}
    ]
    assert {seam["path"] for seam in dom_seams} == message_paths
    assert all(seam["migration_stage"] == 12 for seam in dom_seams)
    assert all(
        seam["canonical_owner"].startswith("message_sender.") for seam in dom_seams
    )
    profile_urn_reads = [
        seam
        for seam in current
        if seam["path"] == "tests/test_send_message_confirmation_dom.py"
        and seam["target"] == "_extract_profile_urn"
    ]
    assert {
        (seam["kind"], seam["canonical_owner"], seam["migration_stage"])
        for seam in profile_urn_reads
    } == {("private_facade_access", "profile_page.ProfilePageReader", 6)}

    private_targets = {
        seam["target"]
        for seam in current
        if seam["kind"] == "private_patch_object"
        and seam["canonical_owner"] == "message_sender.MessageSender"
    }
    assert {
        "_read_profile_message_target",
        "_wait_for_message_surface",
        "_read_message_composer_state",
        "_focus_verified_message_editor",
        "_write_verified_message",
        "_wait_for_verified_submit",
        "_submit_verified_message",
        "_cleanup_owned_message",
        "_resolve_message_owner",
        "_dispose_message_owner",
        "_prepare_message_confirmation",
        "_message_send_confirmed",
        "_dispose_message_confirmation",
    } <= private_targets

    obsolete = {
        "_MESSAGING_RECIPIENT_PICKER_SELECTOR",
        "_MESSAGING_COMPOSE_FALLBACK_SELECTORS",
        "_MESSAGING_CLOSE_SELECTOR",
        "_select_message_recipient",
        "_compose_page_matches_recipient",
        "_message_text_occurrences",
        "_message_text_visible",
        "_dismiss_message_ui",
    }
    assert not obsolete & {seam["target"] for seam in current}
    assert not obsolete & set(migration._PRIVATE_OWNERS)
    assert not obsolete & set(migration._MODULE_ATTRIBUTE_OWNERS)


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
    # The lowest stage that still holds an unclosed seam, which is the only
    # kind of override that can surface one. Stages at or below the tree's own
    # completed stage are closed by definition, so an override there proves
    # nothing; raise this number as each stage lands.
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "6"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "obsolete at stage 6:" in result.stderr
    assert "public_patch_object" in result.stderr
    assert "string_patch" in result.stderr
    # Every direct read of an extractor module attribute aimed at the feed
    # drain moved with the test that held it, so no override below stage 9 can
    # surface one. A feed test still reaching back through the facade module
    # would show up here.
    assert "module_attribute" not in result.stderr


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


def test_relative_import_and_private_facade_accesses_are_inventoried():
    seams = _scan_synthetic(
        """
from .extractor import LinkedInExtractor as Facade

async def scenario(page, replacement):
    extractor = Facade(page)
    build_url = Facade._build_job_search_url
    await extractor._goto_with_auth_checks("https://example.test")
    extractor._read_message_composer_state = replacement
""",
        path=SCRAPING_SYNTHETIC,
    )

    direct = [seam for seam in seams if seam.kind == "direct_import"]
    assert [(seam.target, seam.migration_stage) for seam in direct] == [
        ("LinkedInExtractor", 14)
    ]
    accesses = [seam for seam in seams if seam.kind == "private_facade_access"]
    assert {
        (seam.target, seam.canonical_owner, seam.migration_stage) for seam in accesses
    } == {
        ("_build_job_search_url", "search_urls.build_job_search_url", 2),
        ("_goto_with_auth_checks", "navigation.PageNavigator", 3),
        (
            "_read_message_composer_state",
            "message_sender.MessageSender",
            12,
        ),
    }


@pytest.mark.parametrize(
    "access",
    [
        "missing = Facade._unknown_helper",
        "extractor._unknown_helper = replacement",
    ],
)
def test_unknown_private_facade_accesses_fail_closed(access):
    source = f"""
from .extractor import LinkedInExtractor as Facade

async def scenario(page, replacement):
    extractor = Facade(page)
    {access}
"""

    with pytest.raises(
        migration.UnresolvedSeamError, match="unknown private facade access"
    ):
        _scan_synthetic(source, path=SCRAPING_SYNTHETIC)


def test_nested_functions_inherit_only_unshadowed_extractor_bindings():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def outer(page):
    extractor = LinkedInExtractor(page)

    async def inherited():
        extractor._scroll_seconds += 1.0

    async def parameter_shadow(extractor):
        extractor._unknown_helper()

    async def assignment_shadow():
        extractor = object()
        extractor._unknown_helper()
"""
    )

    accesses = [seam for seam in seams if seam.kind == "private_facade_access"]
    assert [
        (seam.target, seam.canonical_owner, seam.migration_stage) for seam in accesses
    ] == [("_scroll_seconds", "facade.LinkedInExtractor._scroll_seconds", 14)]


def test_unknown_private_closure_access_fails_closed():
    source = """
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def outer(page):
    extractor = LinkedInExtractor(page)

    async def inherited():
        extractor._unknown_helper()
"""

    with pytest.raises(
        migration.UnresolvedSeamError, match="unknown private facade access"
    ):
        _scan_synthetic(source)


@pytest.mark.parametrize(
    ("statement", "local_name"),
    [
        ("from . import extractor", "extractor"),
        ("from . import extractor as legacy", "legacy"),
        ("from ..scraping import extractor as legacy", "legacy"),
    ],
)
def test_relative_extractor_module_aliases_are_inventoried(statement, local_name):
    seams = _scan_synthetic(
        f"{statement}\nreader = {local_name}._drain_listener_tasks\n",
        path=SCRAPING_SYNTHETIC,
    )

    assert [(seam.kind, seam.target, seam.migration_stage) for seam in seams] == [
        ("module_alias", local_name, 14),
        ("module_attribute", "_drain_listener_tasks", 5),
    ]


def test_unknown_relative_module_alias_access_fails_closed():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="unknown direct extractor module attribute",
    ):
        _scan_synthetic(
            "from . import extractor as legacy\nreader = legacy._unknown_helper\n",
            path=SCRAPING_SYNTHETIC,
        )


def test_function_definition_expressions_use_the_enclosing_scope():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

def marker(value):
    return value

@marker(legacy._drain_listener_tasks)
def parameter_shadow(
    legacy: legacy._ProfileMessageTarget = legacy._PROFILE_MESSAGE_TARGET_JS,
    callback=(lambda legacy=legacy._message_page_url_is_safe: legacy._unknown),
) -> legacy._ProfileMessageTargetResolution:
    legacy._unknown()

def local_shadow(
    value: legacy._ProfileMessageTarget = legacy._PROFILE_MESSAGE_TARGET_JS,
):
    legacy = object()
    legacy._unknown()
"""
    )

    attributes = [seam.target for seam in seams if seam.kind == "module_attribute"]
    assert attributes == [
        "_drain_listener_tasks",
        "_ProfileMessageTarget",
        "_PROFILE_MESSAGE_TARGET_JS",
        "_message_page_url_is_safe",
        "_ProfileMessageTargetResolution",
        "_ProfileMessageTarget",
        "_PROFILE_MESSAGE_TARGET_JS",
    ]


@pytest.mark.parametrize(
    "expression",
    [
        "[legacy._unknown for first in legacy._drain_listener_tasks "
        "for legacy in legacy._drain_listener_tasks]",
        "{legacy._unknown for first in legacy._drain_listener_tasks "
        "for legacy in legacy._drain_listener_tasks}",
        "{legacy._unknown: legacy._also_unknown "
        "for first in legacy._drain_listener_tasks "
        "for legacy in legacy._drain_listener_tasks}",
        "(legacy._unknown for first in legacy._drain_listener_tasks "
        "for legacy in legacy._drain_listener_tasks)",
    ],
)
def test_comprehension_targets_shadow_only_after_their_iterators(expression):
    seams = _scan_synthetic(
        "from linkedin_mcp_server.scraping import extractor as legacy\n"
        f"result = {expression}\n"
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks",
        "_drain_listener_tasks",
    ]


def test_nested_comprehensions_keep_their_implicit_scopes_separate():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy
result = [
    legacy._unknown
    for legacy in [item for item in legacy._drain_listener_tasks]
]
"""
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks"
    ]


def test_module_aliases_follow_nested_global_and_nonlocal_bindings():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

def shadows():
    def by_parameter(legacy):
        legacy._unknown

    def by_assignment():
        legacy = object()
        legacy._unknown

    def by_import():
        import types as legacy
        legacy._unknown

    def by_function():
        def legacy():
            pass
        legacy._unknown

    def by_class():
        class legacy:
            pass
        legacy._unknown

    def module_binding():
        global legacy
        return legacy._drain_listener_tasks

def alias_owner():
    from linkedin_mcp_server.scraping import extractor as nested_alias

    def inherited():
        return nested_alias._drain_listener_tasks

    def enclosing_binding():
        nonlocal nested_alias
        return nested_alias._drain_listener_tasks

nested_alias = object()
nested_alias._unknown
"""
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks",
        "_drain_listener_tasks",
        "_drain_listener_tasks",
    ]


@pytest.mark.parametrize(
    "source",
    [
        """
from linkedin_mcp_server.scraping import extractor as legacy

def marker(value):
    return value

@marker(legacy._unknown)
def scenario():
    pass
""",
        """
from linkedin_mcp_server.scraping import extractor as legacy

def scenario(value=legacy._unknown):
    pass
""",
        """
from linkedin_mcp_server.scraping import extractor as legacy

def scenario(value: legacy._unknown):
    pass
""",
        """
from linkedin_mcp_server.scraping import extractor as legacy
result = [item for item in legacy._unknown]
""",
    ],
)
def test_unknown_module_accesses_in_enclosing_evaluation_fail_closed(source):
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="unknown direct extractor module attribute",
    ):
        _scan_synthetic(source)


def test_class_bodies_apply_alias_bindings_sequentially():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

class AssignmentShadow:
    before = legacy._drain_listener_tasks
    legacy = object()
    after = legacy._unknown

    def method(self):
        return legacy._drain_listener_tasks

class ImportShadow:
    before = legacy._drain_listener_tasks
    import types as legacy
    after = legacy._unknown

class ExceptionShadow:
    before = legacy._drain_listener_tasks
    try:
        raise RuntimeError
    except RuntimeError as legacy:
        inside = legacy._unknown
    after = legacy._drain_listener_tasks

class MatchShadow:
    before = legacy._drain_listener_tasks
    match object():
        case legacy:
            inside = legacy._unknown
    after = legacy._unknown
"""
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks",
        "_drain_listener_tasks",
        "_drain_listener_tasks",
        "_drain_listener_tasks",
        "_drain_listener_tasks",
        "_drain_listener_tasks",
    ]


def test_exception_target_cleanup_restores_outer_alias_not_class_shadow():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

class ExceptionCleanup:
    legacy = object()
    try:
        raise RuntimeError
    except RuntimeError as legacy:
        inside = legacy._unknown
    after = legacy._drain_listener_tasks
"""
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks"
    ]


def test_unreachable_class_exception_handler_preserves_instance_binding():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

class NormalTry:
    worker = LinkedInExtractor(page)
    try:
        pass
    except RuntimeError as worker:
        pass
    navigate = worker._navigate_to_page
"""
    )

    assert [seam.target for seam in seams if seam.kind == "private_facade_access"] == [
        "_navigate_to_page"
    ]


def test_uncertain_class_exception_handler_makes_binding_ambiguous():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="ambiguous extractor binding after conditional class control flow",
    ):
        _scan_synthetic(
            """
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

class UncertainTry:
    worker = LinkedInExtractor(page)
    try:
        might_fail()
    except RuntimeError as worker:
        pass
    navigate = worker._navigate_to_page
"""
        )


def test_class_try_finally_reconciles_all_paths():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

class ReconciledTry:
    worker = LinkedInExtractor(page)
    try:
        might_fail()
    except RuntimeError as worker:
        pass
    finally:
        worker = LinkedInExtractor(page)
    navigate = worker._navigate_to_page
"""
    )

    assert [seam.target for seam in seams if seam.kind == "private_facade_access"] == [
        "_navigate_to_page"
    ]


def test_class_assignments_track_new_extractor_instances():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

class ClassInstances:
    worker = LinkedInExtractor(page)
    worker._scroll_seconds += 1.0
    annotated: LinkedInExtractor = LinkedInExtractor(page)
    annotated._navigate_to_page
"""
    )

    assert [seam.target for seam in seams if seam.kind == "private_facade_access"] == [
        "_scroll_seconds",
        "_navigate_to_page",
    ]


def test_annotation_only_class_assignment_does_not_shadow_alias():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

class AnnotationOnly:
    legacy: object
    reader = legacy._drain_listener_tasks
"""
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks"
    ]


def test_constant_class_control_flow_applies_bindings_in_order():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

class ConstantBranch:
    if True:
        legacy = object()
    reader = legacy._unknown
"""
    )

    assert not [seam for seam in seams if seam.kind == "module_attribute"]


def test_conditional_class_alias_state_fails_closed_as_ambiguous():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="ambiguous extractor binding after conditional class control flow",
    ):
        _scan_synthetic(
            """
from linkedin_mcp_server.scraping import extractor as legacy

class ConditionalBranch:
    if condition:
        legacy = object()
    reader = legacy._drain_listener_tasks
"""
        )


def test_matching_class_branches_clear_prior_ambiguity():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy

class ReconciledBranch:
    if first_condition:
        legacy = object()
    if second_condition:
        legacy = object()
    else:
        legacy = object()
    reader = legacy._unknown
"""
    )

    assert not [seam for seam in seams if seam.kind == "module_attribute"]


def test_unknown_class_access_before_later_binding_fails_closed():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="unknown direct extractor module attribute",
    ):
        _scan_synthetic(
            """
from linkedin_mcp_server.scraping import extractor as legacy

class ShadowLater:
    before = legacy._unknown
    legacy = object()
"""
        )


def test_global_removes_instance_binding_and_nonlocal_keeps_it():
    seams = _scan_synthetic(
        """
from linkedin_mcp_server.scraping import extractor as legacy
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

module_worker = object()

async def outer(page):
    legacy = LinkedInExtractor(page)
    module_worker = LinkedInExtractor(page)

    async def module_alias():
        global legacy
        return legacy._drain_listener_tasks

    async def ordinary_global():
        global module_worker
        return module_worker._unknown

    async def closure_instance():
        nonlocal legacy
        return legacy._scroll_seconds
"""
    )

    assert [seam.target for seam in seams if seam.kind == "module_attribute"] == [
        "_drain_listener_tasks"
    ]
    assert [seam.target for seam in seams if seam.kind == "private_facade_access"] == [
        "_scroll_seconds"
    ]


def test_unknown_global_module_alias_access_fails_closed():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="unknown direct extractor module attribute",
    ):
        _scan_synthetic(
            """
from linkedin_mcp_server.scraping import extractor as legacy
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def outer(page):
    legacy = LinkedInExtractor(page)

    async def module_alias():
        global legacy
        return legacy._unknown
"""
        )


def test_manifest_includes_extractor_access_from_nested_closure():
    accesses = [
        seam
        for seam in migration.scan()["seams"]
        if seam["path"] == "tests/test_scraping.py"
        and seam["target"] == "_scroll_seconds"
    ]

    assert {
        (seam["line"], seam["canonical_owner"], seam["migration_stage"])
        for seam in accesses
    } == {
        (590, "facade.LinkedInExtractor._scroll_seconds", 14),
        (3702, "facade.LinkedInExtractor._scroll_seconds", 14),
    }


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


def test_boundary_callers_ignore_workflow_calls_in_shadowed_scopes():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page, values):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        [
            extractor.search_posts("query")
            for extractor in values
        ]
        callback = lambda extractor: extractor.scrape_company("company")

        async def nested(extractor):
            await extractor.scrape_person("person")

        class Holder:
            extractor = object()
            value = extractor.search_posts("query")

        await extractor.extract_feed()
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {5}


def test_boundary_callers_keep_outer_comprehension_iterator_calls():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        [item for item in extractor.search_posts("query")]
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {10}


def test_boundary_callers_keep_inherited_nested_scope_calls():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        async def nested():
            await extractor.search_posts("query")

        class Holder:
            async def method(self):
                await extractor.scrape_person("person")
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {6, 10}


def test_boundary_callers_keep_annotation_only_class_bindings():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        class Holder:
            extractor: object
            value = extractor.extract_feed()
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {5}


def test_ambiguous_class_workflow_callers_fail_closed():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="ambiguous workflow binding after conditional class control flow",
    ):
        _scan_synthetic(
            """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page, condition):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        class Holder:
            if condition:
                extractor = object()
            value = extractor.extract_feed()
"""
        )


def test_boundary_callers_follow_class_exception_and_match_targets():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        class ExceptionTarget:
            extractor = object()
            try:
                raise RuntimeError
            except RuntimeError as extractor:
                inside = extractor.search_posts("query")
            after = extractor.extract_feed()

        class MatchTarget:
            match object():
                case extractor:
                    inside = extractor.search_posts("query")
            after = extractor.scrape_company("company")
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {5}


def test_boundary_callers_ignore_unreachable_class_exception_handlers():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        class NormalTry:
            extractor = object()
            try:
                pass
            except RuntimeError as extractor:
                pass
            after = extractor.search_posts("query")

        await extractor.extract_feed()
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {5}


def test_uncertain_class_exception_callers_fail_closed():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="ambiguous workflow binding after conditional class control flow",
    ):
        _scan_synthetic(
            """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        class UncertainTry:
            extractor = object()
            try:
                might_fail()
            except RuntimeError as extractor:
                pass
            after = extractor.extract_feed()
"""
        )


def test_boundary_callers_track_class_extractor_assignments():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page):
    with patch.object(legacy_surface, "detect_rate_limit"):
        class ReconciledFinally:
            try:
                might_fail()
            except RuntimeError as worker:
                pass
            finally:
                worker = LinkedInExtractor(page)
            after = worker.extract_feed()

        class AnnotatedInstance:
            worker: LinkedInExtractor = LinkedInExtractor(page)
            after = worker.search_posts("query")

        class LocalAlias:
            from linkedin_mcp_server.scraping.extractor import (
                LinkedInExtractor as LocalExtractor,
            )
            worker = LocalExtractor(page)
            after = worker.scrape_company("company")

        class ShadowedAlias:
            LinkedInExtractor = object()
            worker = LinkedInExtractor(page)
            after = worker.scrape_person("person")

        class AnnotationOnly:
            worker: LinkedInExtractor
            after = worker.scrape_person("person")
"""
    )

    boundary = [
        seam
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    ]
    assert {seam.migration_stage for seam in boundary} == {5, 8, 10}


def test_only_shadowed_boundary_callers_fail_closed():
    with pytest.raises(
        migration.UnresolvedSeamError,
        match="caller workflow could not be resolved",
    ):
        _scan_synthetic(
            """
from unittest.mock import patch
from linkedin_mcp_server.scraping import extractor as legacy_surface
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def scenario(page, values):
    extractor = LinkedInExtractor(page)
    with patch.object(legacy_surface, "detect_rate_limit"):
        [extractor.search_posts("query") for extractor in values]
        callback = lambda extractor: extractor.scrape_company("company")
"""
        )


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
    compose_selector = legacy_surface._MESSAGING_COMPOSE_SELECTOR
    target_program = legacy_surface._PROFILE_MESSAGE_TARGET_JS
    target_type = legacy_surface._ProfileMessageTarget
    resolution_type = legacy_surface._ProfileMessageTargetResolution
    profile_urn = legacy_surface._profile_urn_from_compose_url
    profile_path = legacy_surface._profile_path_from_url
    safe_route = legacy_surface._message_page_url_is_safe
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
    assert {seam.migration_stage for seam in matching("scroll_to_bottom")} == {9}
    assert {seam.migration_stage for seam in matching("scroll_job_sidebar")} == {9}
    assert {seam.migration_stage for seam in matching("_URL_SETTLE_LAG")} == {3}
    assert {seam.canonical_owner for seam in matching("_URL_SETTLE_LAG")} == {
        "navigation.PageNavigator._URL_SETTLE_LAG"
    }
    assert {seam.migration_stage for seam in matching("_URL_SETTLE_QUIET")} == {3}
    assert {seam.canonical_owner for seam in matching("_URL_SETTLE_QUIET")} == {
        "navigation.PageNavigator._URL_SETTLE_QUIET"
    }
    messaging_targets = {
        "_MESSAGING_COMPOSE_SELECTOR",
        "_PROFILE_MESSAGE_TARGET_JS",
        "_ProfileMessageTarget",
        "_ProfileMessageTargetResolution",
        "_profile_urn_from_compose_url",
        "_profile_path_from_url",
        "_message_page_url_is_safe",
    }
    assert all(
        {seam.migration_stage for seam in matching(target)} == {12}
        for target in messaging_targets
    )


def test_direct_private_helper_calls_and_stage_gate_are_inventoried():
    current = migration.scan()
    drains = [
        seam for seam in current["seams"] if seam["target"] == "_drain_listener_tasks"
    ]

    # Closed by relocating the reads, not by dropping the entries that resolve
    # them: a read that reappears has to be inventoried against
    # `feed.FeedScraper` rather than fail closed as an unknown attribute.
    assert drains == []
    assert migration._PRIVATE_OWNERS["_drain_listener_tasks"] == ("feed.FeedScraper", 5)
    assert migration._IMPORT_OWNERS["_drain_listener_tasks"] == (
        "feed.FeedScraper._drain_listener_tasks",
        5,
    )

    private_reads = [
        seam
        for seam in current["seams"]
        if seam["kind"] == "module_attribute" and seam["target"].startswith("_")
    ]
    assert {seam["path"] for seam in private_reads} == {"tests/test_scraping.py"}
    assert all(seam["migration_stage"] == 12 for seam in private_reads)
    direct_privates = [
        seam
        for seam in current["seams"]
        if seam["kind"] == "direct_import" and seam["target"].startswith("_")
    ]
    assert {
        (seam["canonical_owner"], seam["migration_stage"]) for seam in direct_privates
    } >= {
        ("connection_actions.ACTION_SIGNALS_JS", 7),
        ("job_pages.JOB_IDS_JS", 9),
    }

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "12"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert (
        "module_attribute _MESSAGING_COMPOSE_SELECTOR -> "
        "message_sender.MESSAGE_COMPOSE_SELECTOR" in result.stderr
    )


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


def test_collaborator_patches_resolve_through_the_facade_attribute():
    seams = _scan_synthetic(
        """
from unittest.mock import patch
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_reach_through(page, replacement):
    extractor = LinkedInExtractor(page)
    with (
        patch.object(extractor._capture, "_extract_overlay", replacement),
        patch.object(extractor._capture, "extract_page", replacement),
        patch.object(extractor._content, "_extract_root_content", replacement),
    ):
        await extractor.scrape_person("ada")

async def test_company_reach_through(page, replacement):
    extractor = LinkedInExtractor(page)
    with patch.object(extractor._content, "_extract_root_content", replacement):
        await extractor.scrape_company("acme")
"""
    )

    assert [
        (seam.kind, seam.target, seam.canonical_owner, seam.migration_stage)
        for seam in seams
        if seam.kind.endswith("_patch_object")
    ] == [
        ("private_patch_object", "_extract_overlay", "capture.SectionCapture", 6),
        # `extract_page` is not a dependency of `SectionCapture`, it *is*
        # `SectionCapture`, so the owner names the workflow consuming it the
        # way every other public entry does.
        ("public_patch_object", "extract_page", "person.PersonScraper dependency", 6),
        (
            "private_patch_object",
            "_extract_root_content",
            "content.PageContentReader",
            6,
        ),
        (
            "private_patch_object",
            "_extract_root_content",
            "content.PageContentReader",
            8,
        ),
    ]
    # The reach-through stays on the record next to the patch it carries, and
    # both expire with the workflow this call site drives. The same `_content`
    # attribute therefore closes at 6 in the first test and at 8 in the second:
    # once `scrape_person` owns its own reader, the stub in that test
    # intercepts nothing while the `scrape_company` one is still live. A flat
    # per-attribute maximum answered 11 for both.
    assert [
        (seam.target, seam.canonical_owner, seam.migration_stage)
        for seam in seams
        if seam.kind == "private_facade_access"
    ] == [
        ("_capture", "person.PersonScraper -> facade.LinkedInExtractor._capture", 6),
        ("_capture", "person.PersonScraper -> facade.LinkedInExtractor._capture", 6),
        ("_content", "person.PersonScraper -> facade.LinkedInExtractor._content", 6),
        ("_content", "company.CompanyScraper -> facade.LinkedInExtractor._content", 8),
    ]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("extractor._capture, '_extract_overlayy'", "unknown capture.SectionCapture"),
        ("extractor._content, '_extract_root'", "unknown content.PageContentReader"),
        ("extractor._session, 'check_rate_limit'", "unknown facade collaborator"),
    ],
)
def test_unknown_collaborator_patches_fail_closed(target, message):
    # A misspelled member, or an attribute carrying no collaborator at all,
    # intercepts nothing and reads as a passing test. Falling through without a
    # seam is what let the whole shape go unrecorded, so it has to name its
    # call site instead.
    source = f"""
from unittest.mock import patch
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_typo(page):
    extractor = LinkedInExtractor(page)
    with patch.object({target}):
        pass
"""

    with pytest.raises(
        migration.UnresolvedSeamError, match=rf"synthetic_inventory\.py:7 .*{message}"
    ):
        _scan_synthetic(source)


def _reach_through(statement: str) -> str:
    """A workflow test whose single statement is the shape under test."""

    return f"""
from unittest.mock import patch
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_shape(page, replacement, monkeypatch, arguments, pair, thing):
    extractor = LinkedInExtractor(page)
    {statement}
    await extractor.scrape_person("ada")
"""


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        (
            'patch.object(self.extractor._capture, "made_up", replacement)',
            r"self\.extractor\._capture: unresolved patch\.object target",
        ),
        (
            'patch.object(extractor._capture._reader, "made_up", replacement)',
            r"extractor\._capture\._reader: unresolved patch\.object target",
        ),
        (
            'patch.object(getattr(extractor, "_capture"), "made_up", replacement)',
            r"getattr\(extractor, '_capture'\): unresolved patch\.object target",
        ),
        (
            "patch.object(*arguments)",
            r"patch\.object\(\*arguments\): unresolved patch\.object arguments",
        ),
        (
            "patch.object(extractor._capture)",
            r"patch\.object\(extractor\._capture\): "
            r"unresolved patch\.object arguments",
        ),
        (
            "patch.object(*pair, replacement)",
            r"\*pair: dynamic patch\.object attribute",
        ),
        (
            'monkeypatch.setattr(extractor._capture._reader, "made_up", replacement)',
            r"extractor\._capture\._reader: unresolved setattr target",
        ),
        (
            "extractor._capture._reader.made_up = replacement",
            r"extractor\._capture\._reader: unresolved attribute assignment target",
        ),
    ],
)
def test_unresolvable_replacement_targets_name_their_call_site(statement, message):
    # Each of these replaces a name on something the reader cannot reduce to a
    # collaborator, so nothing proves the patch intercepts the implementation.
    # A chain of `if ...: return` blocks that simply ends answers "not a seam"
    # to exactly that, which is indistinguishable from a foreign object.
    with pytest.raises(
        migration.UnresolvedSeamError, match=rf"synthetic_inventory\.py:7 .*{message}"
    ):
        _scan_synthetic(_reach_through(statement))


@pytest.mark.parametrize(
    ("statement", "line"),
    [
        ('cap = extractor._capture\n    patch.object(cap, "{name}", replacement)', 8),
        ('patch.object(cap := extractor._capture, "{name}", replacement)', 7),
        (
            "patch.object(target=extractor._capture, "
            'attribute="{name}", new=replacement)',
            7,
        ),
        ('monkeypatch.setattr(extractor._capture, "{name}", replacement)', 7),
        ('setattr(extractor._capture, "{name}", replacement)', 7),
        ("extractor._capture.{name} = replacement", 7),
    ],
)
def test_every_reach_through_shape_lands_on_the_collaborator(statement, line):
    # An alias, a walrus, the keyword form and all three `setattr` spellings
    # reach the same collaborator as `patch.object(extractor._capture, ...)`.
    # A shape that produced no seam also produced no error, so a name that has
    # never existed on `SectionCapture` read as a passing test.
    seams = _scan_synthetic(_reach_through(statement.format(name="_extract_overlay")))

    assert [
        (seam.kind, seam.target, seam.canonical_owner, seam.migration_stage)
        for seam in seams
        if seam.kind.endswith("_patch_object")
    ] == [("private_patch_object", "_extract_overlay", "capture.SectionCapture", 6)]

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=rf"synthetic_inventory\.py:{line} _capture\.made_up: "
        r"unknown capture\.SectionCapture patch",
    ):
        _scan_synthetic(_reach_through(statement.format(name="made_up")))


def test_a_foreign_collaborator_of_its_own_stays_out_of_the_inventory():
    # The refusal has to stop at objects the reader knows nothing about, or it
    # refuses the migration's own target state: `tests/scraping/test_feed.py`
    # builds a `FeedScraper` and stubs `_extract_feed_body` on it, which is the
    # identical shape and is exactly what stage 5 produced. Nothing in the
    # expression names the extractor, so there is no reach-through to record.
    seams = _scan_synthetic(
        _reach_through('patch.object(thing, "_extract_overlay", replacement)')
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


def test_a_renamed_collaborator_class_names_its_call_site(monkeypatch):
    # A rename used to reach `collaborator_methods`' bare `AssertionError`,
    # which leaves the scan with a traceback and no location instead of a
    # diagnostic naming the patch that can no longer be resolved.
    monkeypatch.setitem(
        migration._FACADE_COLLABORATORS,
        "_capture",
        ("capture.RenamedSectionCapture", "facade.LinkedInExtractor._capture"),
    )

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"synthetic_inventory\.py:7 _capture\._extract_overlay: "
        r"RenamedSectionCapture not found in scraping/capture\.py",
    ):
        _scan_synthetic(
            _reach_through(
                'patch.object(extractor._capture, "_extract_overlay", replacement)'
            )
        )


@pytest.mark.parametrize(
    "suffix",
    ["", "\nreader = linkedin_mcp_server.scraping.extractor._drain_listener_tasks"],
)
def test_unaliased_extractor_module_imports_are_rejected(suffix):
    with pytest.raises(migration.UnresolvedSeamError, match="requires an as alias"):
        _scan_synthetic("import linkedin_mcp_server.scraping.extractor" + suffix)


def test_aliased_full_module_import_keeps_its_direct_attribute_inventory():
    seams = _scan_synthetic(
        "import linkedin_mcp_server.scraping.extractor as legacy\n"
        "reader = legacy._drain_listener_tasks\n"
    )
    attributes = [seam for seam in seams if seam.kind == "module_attribute"]
    assert len(attributes) == 1
    assert attributes[0].target == "_drain_listener_tasks"
    assert attributes[0].migration_stage == 5


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
