"""Extractor seam migration inventory contracts."""

from __future__ import annotations

from pathlib import Path

import json
import logging
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import check_scraping_migration_manifest as migration  # noqa: E402

MANIFEST = ROOT / "tests" / "fixtures" / "scraping-policy" / "migration-manifest.json"
CHECKER = ROOT / "scripts" / "check_scraping_migration_manifest.py"
POLICY_SCENARIOS = ROOT / "tests" / "scraping" / "policy_scenarios.py"
PERSON_TESTS = ROOT / "tests" / "scraping" / "test_person.py"
SCRAPING_SYNTHETIC = (
    ROOT / "linkedin_mcp_server" / "scraping" / "synthetic_inventory.py"
)

_SHARED_BOUNDARY_STAGES = {
    "detect_rate_limit": {9, 11, 12},
    "handle_modal_close": {9, 11, 12},
    "scroll_to_bottom": {9},
    "scroll_job_sidebar": {9},
    "build_issue_diagnostics": {8, 9},
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
    # The URN read moved to `profile_page.ProfilePageReader` at stage 6, and
    # the DOM test builds that reader instead of reaching the facade for it.
    # What it still borrows is the top-card read behind it, which is the
    # message sender's and dated accordingly.
    profile_urn_reads = [
        seam for seam in current if seam["target"] == "_extract_profile_urn"
    ]
    assert profile_urn_reads == []
    assert migration._PRIVATE_OWNERS["_extract_profile_urn"] == (
        "profile_page.ProfilePageReader",
        6,
    )
    assert {
        (seam["kind"], seam["canonical_owner"], seam["migration_stage"])
        for seam in current
        if seam["path"] == "tests/test_send_message_confirmation_dom.py"
        and seam["target"] == "_read_profile_message_target"
    } == {
        ("private_facade_access", "message_sender.MessageSender", 12),
        ("private_patch_object", "message_sender.MessageSender", 12),
    }

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
    # The last one was a `get_sidebar_profiles` test reading the facade's
    # logger, and it moved to the person owner at stage 6. Closed by
    # relocating the read rather than by dropping `logger` from
    # `_CONTEXTUAL_MODULE_NAMES`: the next such patch has to be inventoried
    # against the workflow that drives it, not fail closed as unknown.
    assert logger_patches == []
    assert "logger" in migration._CONTEXTUAL_MODULE_NAMES


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
    } == {8, 9, 10}
    # Enumerated rather than checked one target at a time, so a public patch
    # arriving for a workflow nobody expected fails here instead of passing
    # unnoticed.
    assert {seam["target"] for seam in public} == {"extract_page", "search_companies"}
    # Both of the stage-7 targets were `connect_with_person` tests and closed
    # by moving to the owner: it takes its one main-profile read as an injected
    # callable, and it holds no click-by-text helper at all. The table keeps
    # their entries so a patch that reappears is dated rather than unresolved.
    assert not [seam for seam in public if seam["target"] == "scrape_person"]
    assert not [seam for seam in public if seam["target"] == "click_button_by_text"]
    assert migration._WORKFLOW_OWNERS["scrape_person"] == ("person.PersonScraper", 6)
    assert migration._WORKFLOW_OWNERS["click_button_by_text"] == (
        "facade.LinkedInExtractor compatibility method",
        14,
    )


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
        [sys.executable, str(CHECKER), "--check", "--stage", "8"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "obsolete at stage 8:" in result.stderr
    assert "public_patch_object" in result.stderr
    assert "private_patch_object" in result.stderr
    # Every direct read of an extractor module attribute aimed at the feed
    # drain moved with the test that held it, so no override below stage 9 can
    # surface one. A feed test still reaching back through the facade module
    # would show up here.
    assert "module_attribute" not in result.stderr
    # `string_patch` runs the other way: the person workflow held the last one
    # below stage 8, and the three the company workflow drives sit at exactly
    # 8, so raising this override past the connection stage brought them back.
    assert "string_patch" in result.stderr


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
        (2298, "facade.LinkedInExtractor._scroll_seconds", 14),
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
    # Two files now: the profile-page owner test asserts the top-card program
    # it borrows from the facade is the one that ran, which is the same
    # stage-12 read the messaging tests name.
    assert {seam["path"] for seam in private_reads} == {
        "tests/scraping/test_profile_page.py",
        "tests/test_scraping.py",
    }
    assert all(seam["migration_stage"] == 12 for seam in private_reads)
    direct_privates = [
        seam
        for seam in current["seams"]
        if seam["kind"] == "direct_import" and seam["target"].startswith("_")
    ]
    assert {
        (seam["canonical_owner"], seam["migration_stage"]) for seam in direct_privates
    } >= {("job_pages.JOB_IDS_JS", 9)}
    # The two action-signal programs were the stage-7 half of that pair. They
    # moved with their owner and the DOM test imports them from
    # `connection_actions`, which is no seam at all. Closed by relocating the
    # imports rather than by dropping the entries: an import back through the
    # facade has to be dated, not fail closed as unknown.
    assert not [
        seam
        for seam in current["seams"]
        if seam["target"] in {"_ACTION_SIGNALS_JS", "_CLICK_INCOMING_ACCEPT_JS"}
    ]
    assert migration._IMPORT_OWNERS["_ACTION_SIGNALS_JS"] == (
        "connection_actions.ACTION_SIGNALS_JS",
        7,
    )

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

async def test_shape(page, replacement, monkeypatch, arguments, pair, thing, flag):
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
        (
            "extractor._capture._reader.made_up: object = replacement",
            r"extractor\._capture\._reader: unresolved attribute assignment target",
        ),
        (
            "monkeypatch.setattr()",
            r"monkeypatch\.setattr\(\): unresolved setattr arguments",
        ),
        (
            "monkeypatch.setattr(*arguments)",
            r"monkeypatch\.setattr\(\*arguments\): unresolved setattr arguments",
        ),
        (
            "monkeypatch.setattr(**arguments)",
            r"monkeypatch\.setattr\(\*\*arguments\): unresolved setattr arguments",
        ),
        (
            "setattr(*arguments)",
            r"setattr\(\*arguments\): unresolved setattr arguments",
        ),
        (
            "setattr(**arguments)",
            r"setattr\(\*\*arguments\): unresolved setattr arguments",
        ),
        (
            "monkeypatch.setattr(extractor._capture)",
            r"extractor\._capture: unresolved setattr target",
        ),
        (
            "monkeypatch.setattr(target=extractor._capture, value=replacement)",
            r"extractor\._capture: unresolved setattr target",
        ),
    ],
)
def test_unresolvable_replacement_targets_name_their_call_site(statement, message):
    # Each of these replaces a name on something the reader cannot reduce to a
    # collaborator, or hides both ends of the replacement behind a shape it
    # cannot take apart, so nothing proves the patch intercepts the
    # implementation. A chain of `if ...: return` blocks that simply ends
    # answers "not a seam" to exactly that, which is indistinguishable from a
    # foreign object. The `setattr` arity gates were the last two such chains:
    # both the keyword form and a call short of its member name walked past
    # them without a word.
    with pytest.raises(
        migration.UnresolvedSeamError, match=rf"synthetic_inventory\.py:7 .*{message}"
    ):
        _scan_synthetic(_reach_through(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "helper.setattr(*arguments)",
        "helper.setattr(**arguments)",
        "helper.setattr()",
        "self.helper.setattr(*arguments)",
    ],
)
def test_a_foreign_setattr_receiver_keeps_its_dynamic_shape(statement):
    # `setattr` is an ordinary method name and the tree holds about 1873 calls
    # to one. Refusing every dynamic shape ahead of any scoping fails the
    # checker on a receiver that cannot reach the extractor at all, and this
    # guard gates every remaining stage of the decomposition. Nothing is
    # recorded either, because nothing was resolved.
    seams = _scan_synthetic(_reach_through(statement))

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


def _fixture_alias(binding: str, call: str) -> str:
    """A workflow test that patches through a fixture under another name."""

    return f"""
import pytest
from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

async def test_shape(page, arguments{binding}):
    extractor = LinkedInExtractor(page)
    {call}
    await extractor.scrape_person("ada")
"""


@pytest.mark.parametrize(
    ("binding", "call", "line", "receiver"),
    [
        ("", "monkeypatch.setattr(*arguments)", 7, "monkeypatch"),
        (
            "",
            "with pytest.MonkeyPatch.context() as patching:\n"
            "        patching.setattr(*arguments)",
            8,
            "patching",
        ),
        (
            ", patcher: pytest.MonkeyPatch",
            "patcher.setattr(*arguments)",
            7,
            "patcher",
        ),
        ("", "mp = monkeypatch\n    mp.setattr(*arguments)", 8, "mp"),
        (
            "",
            "self.patcher = pytest.MonkeyPatch()\n    self.patcher.setattr(*arguments)",
            8,
            "self.patcher",
        ),
    ],
)
def test_an_unreadable_setattr_through_the_fixture_names_its_call_site(
    binding, call, line, receiver
):
    # The fixture is the one receiver whose `setattr` routinely lands on the
    # extractor, and it is routinely bound to another name: a private context
    # manager, an annotated parameter, a plain alias. Testing the receiver
    # against the literal `monkeypatch` would skip exactly those, so the live
    # bindings at each call site are resolved first.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=rf"synthetic_inventory\.py:{line} "
        rf"{re.escape(receiver)}\.setattr\(\*arguments\): "
        r"unresolved setattr arguments",
    ):
        _scan_synthetic(_fixture_alias(binding, call))


@pytest.mark.parametrize(
    "binding",
    [
        "patch, other = monkeypatch, helper",
        "[patch, other] = [monkeypatch, helper]",
        "(first, [patch, other]) = (value, [monkeypatch, helper])",
        "other, patch = helper, monkeypatch",
    ],
)
def test_destructuring_preserves_the_corresponding_patcher_authority(binding):
    # Mutating assignment handling back to one authority bit for the whole RHS
    # either drops `patch` or blesses its unrelated sibling. The call is the
    # observable authority check, not merely a count of names visited.
    _assert_authoritative_receiver(binding)


def test_exact_destructuring_keeps_an_unrelated_target_non_authoritative():
    source = _receiver_flow("patch, other = helper, monkeypatch", receiver="patch")

    assert _scan_synthetic(source) == []


def test_unpairable_starred_destructuring_keeps_possible_authority():
    _assert_authoritative_receiver("patch, *other = (monkeypatch, helper)")


def test_unpairable_destructuring_without_authority_stays_non_authoritative():
    source = _receiver_flow("patch, *other = (helper, value)")

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize(
    "statements",
    [
        "slots[0] = monkeypatch\n    slots[0] = helper",
        "slots[0] = monkeypatch\n    alias = slots",
        "slots[0] = monkeypatch\n    slots = value",
        "slots[0], slots[1] = helper, monkeypatch",
    ],
)
def test_subscript_receivers_fail_closed_without_textual_authority(statements):
    receiver = "alias[0]" if "alias = slots" in statements else "slots[0]"
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=rf"{re.escape(receiver)}\.setattr\(\*arguments\): "
        r"unresolved setattr arguments",
    ):
        _scan_synthetic(_receiver_flow(statements, receiver=receiver))


def test_subscript_receiver_in_a_closure_fails_closed():
    source = """
async def test_outer(monkeypatch, helper, arguments, slots):
    slots[0] = helper

    async def test_inner():
        slots[0].setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"slots\[0\]\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


def test_a_context_alias_does_not_leak_into_a_sibling_scope():
    source = """
import pytest

async def test_first():
    with pytest.MonkeyPatch.context() as patch:
        pass

async def test_second(arguments):
    patch.setattr(*arguments)
"""

    assert _scan_synthetic(source) == []


def test_a_rebound_monkeypatch_alias_loses_receiver_authority():
    source = """
async def test_shape(monkeypatch, helper, arguments):
    patch = monkeypatch
    patch = helper
    patch.setattr(*arguments)
"""

    assert _scan_synthetic(source) == []


def test_a_nested_scope_can_shadow_an_outer_monkeypatch_alias():
    source = """
async def test_outer(monkeypatch, helper, arguments):
    patch = monkeypatch

    async def test_inner(patch=helper):
        patch.setattr(*arguments)
"""

    assert _scan_synthetic(source) == []


def test_a_nested_scope_inherits_a_live_monkeypatch_alias():
    source = """
async def test_outer(monkeypatch, arguments):
    patch = monkeypatch

    async def test_inner():
        patch.setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


@pytest.mark.parametrize(
    "nested",
    [
        "async def inner():\n        patch.setattr(*arguments)",
        "class Holder:\n        def method(self):\n            patch.setattr(*arguments)",
    ],
)
def test_nested_bodies_use_authority_assigned_after_their_definition(nested):
    source = f"""
async def outer(monkeypatch, helper, arguments):
    patch = helper

    {nested}

    patch = monkeypatch
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


@pytest.mark.parametrize(
    "nested",
    [
        "async def inner():\n        patch.setattr(*arguments)",
        "class Holder:\n        def method(self):\n            patch.setattr(*arguments)",
    ],
)
def test_nested_bodies_drop_authority_retired_after_their_definition(nested):
    source = f"""
async def outer(monkeypatch, helper, arguments):
    patch = monkeypatch

    {nested}

    patch = helper
"""

    assert _scan_synthetic(source) == []


def test_a_live_context_alias_remains_authoritative():
    source = """
import pytest

async def test_shape(arguments):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


def test_rebinding_receiver_does_not_hide_extractor_reachability():
    source = _reach_through(
        "patch = monkeypatch\n"
        "    patch = helper\n"
        '    patch.setattr(extractor._capture, "made_up", replacement)'
    )

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"_capture\.made_up: unknown capture\.SectionCapture patch",
    ):
        _scan_synthetic(source)


@pytest.mark.parametrize("receiver", ["monkeypatch", "helper"])
def test_a_readable_setattr_target_survives_a_later_star(receiver):
    source = _reach_through(f"{receiver}.setattr(extractor, *arguments)")

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"synthetic_inventory\.py:7 extractor: unresolved setattr target",
    ):
        _scan_synthetic(source)


def _receiver_flow(statements: str, receiver: str = "patch") -> str:
    return f"""
async def test_shape(monkeypatch, helper, arguments, flag, value):
    {statements}
    {receiver}.setattr(*arguments)
"""


def _assert_authoritative_receiver(statements: str, receiver: str = "patch") -> None:
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=rf"{re.escape(receiver)}\.setattr\(\*arguments\): "
        r"unresolved setattr arguments",
    ):
        _scan_synthetic(_receiver_flow(statements, receiver))


@pytest.mark.parametrize(
    "branches",
    [
        "if flag:\n        patch = monkeypatch\n    else:\n        patch = helper",
        "if flag:\n        patch = helper\n    else:\n        patch = monkeypatch",
    ],
)
def test_if_branches_merge_receiver_authority_in_either_order(branches):
    _assert_authoritative_receiver(f"patch = helper\n    {branches}")


@pytest.mark.parametrize(
    "branches",
    [
        "if flag:\n        patch = monkeypatch\n    else:\n        copy = patch",
        "if flag:\n        copy = patch\n    else:\n        patch = monkeypatch",
    ],
)
def test_if_branches_do_not_share_impossible_alias_states(branches):
    source = _receiver_flow(
        f"patch = helper\n    copy = helper\n    {branches}", "copy"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize(
    "statements",
    [
        "patch = helper\n"
        "    try:\n"
        "        patch = monkeypatch\n"
        "    except Exception:\n"
        "        patch = helper\n"
        "    else:\n"
        "        pass\n"
        "    finally:\n"
        "        pass",
        "patch = helper\n"
        "    try:\n"
        "        patch = helper\n"
        "        value()\n"
        "    except Exception:\n"
        "        patch = monkeypatch\n"
        "    else:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        pass",
    ],
)
def test_try_paths_merge_receiver_authority(statements):
    _assert_authoritative_receiver(statements)


def test_try_finally_rebinding_retires_receiver_authority_on_every_path():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        patch = monkeypatch\n"
        "    except Exception:\n"
        "        patch = monkeypatch\n"
        "    else:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch = helper"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize("suite", ["else", "handler"])
def test_finally_sees_authority_before_else_and_handler_raises(suite):
    if suite == "else":
        statements = (
            "patch = helper\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
            "    else:\n"
            "        patch = monkeypatch\n"
            "        raise RuntimeError\n"
            "    finally:\n"
            "        patch.setattr(*arguments)"
        )
    else:
        statements = (
            "patch = helper\n"
            "    try:\n"
            "        raise RuntimeError\n"
            "    except RuntimeError:\n"
            "        patch = monkeypatch\n"
            "        raise ValueError\n"
            "    finally:\n"
            "        patch.setattr(*arguments)"
        )
    _assert_authoritative_receiver(statements)


@pytest.mark.parametrize("suite", ["else", "handler"])
def test_finally_ignores_definitely_retired_else_and_handler_states(suite):
    if suite == "else":
        statements = (
            "patch = helper\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
            "    else:\n"
            "        patch = helper\n"
            "        raise RuntimeError\n"
            "    finally:\n"
            "        patch.setattr(*arguments)"
        )
    else:
        statements = (
            "patch = helper\n"
            "    try:\n"
            "        raise RuntimeError\n"
            "    except RuntimeError:\n"
            "        patch = helper\n"
            "        raise ValueError\n"
            "    finally:\n"
            "        patch.setattr(*arguments)"
        )
    assert _scan_synthetic(_receiver_flow(statements)) == []


def test_trystar_consumes_a_known_lone_exception_at_the_first_match():
    source = _receiver_flow(
        "patch = helper\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except* RuntimeError:\n"
        "        patch = helper\n"
        "    except* Exception:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_trystar_keeps_group_member_matching_unknown():
    _assert_authoritative_receiver(
        "patch = helper\n"
        "    try:\n"
        '        raise ExceptionGroup("group", [RuntimeError()])\n'
        "    except* RuntimeError:\n"
        "        patch = helper\n"
        "    except* Exception:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_trystar_does_not_match_members_from_the_group_class_identity():
    _assert_authoritative_receiver(
        "patch = helper\n"
        "    try:\n"
        '        raise BaseExceptionGroup("group", [RuntimeError()])\n'
        "    except* BaseExceptionGroup:\n"
        "        patch = helper\n"
        "    except* BaseException:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_trystar_routes_only_remaining_lone_exceptions_to_later_handlers():
    source = _receiver_flow(
        "patch = helper\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except* TypeError:\n"
        "        patch = monkeypatch\n"
        "    except* RuntimeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_trystar_unknown_groups_retain_handled_and_unmatched_paths():
    _assert_authoritative_receiver(
        "patch = helper\n"
        "    try:\n"
        "        raise error\n"
        "    except* RuntimeError:\n"
        "        patch = helper\n"
        "    except* Exception:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_trystar_base_exception_consumes_unknown_ordinary_exceptions():
    source = _receiver_flow(
        "patch = helper\n"
        "    try:\n"
        "        raise error\n"
        "    except* BaseException:\n"
        "        patch = helper\n"
        "    except* Exception:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_trystar_without_any_authoritative_path_stays_non_authoritative():
    source = _receiver_flow(
        "patch = helper\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except* RuntimeError:\n"
        "        patch = helper\n"
        "    except* Exception:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_ordinary_try_handlers_remain_exclusive_after_a_known_match():
    source = _receiver_flow(
        "patch = helper\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    except Exception:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_a_handled_raise_that_completes_does_not_poison_finally():
    # Greptile's reproducer: retaining the original RuntimeError beside the
    # handler result makes finally merge an impossible authoritative state. The
    # mutation that seeds `result.exceptional` directly from the try body makes
    # this fail again.
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize(
    "operation",
    [
        "result = local_before_assignment\n"
        "local_before_assignment = helper\n"
        "patch = helper",
        "result = operation()\npatch = helper",
        "result = consume(local_before_assignment)\n"
        "local_before_assignment = helper\n"
        "patch = helper",
        "left, right = value\npatch = helper",
        "raise RuntimeError(local_before_assignment)\nlocal_before_assignment = helper",
    ],
)
def test_expression_evaluation_prefixes_still_reach_finally(operation):
    _assert_authoritative_receiver(
        "patch = monkeypatch\n"
        "    try:\n"
        f"        {operation.replace(chr(10), chr(10) + '        ')}\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


@pytest.mark.parametrize("handler", ["RuntimeError", "Exception", "BaseException", ""])
def test_matching_handlers_consume_a_known_raise_before_finally(handler):
    clause = f"except {handler}:" if handler else "except:"
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise RuntimeError\n"
        f"    {clause}\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_builtin_exception_keyword_call_retains_its_type_error_prefix():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        '        raise RuntimeError(message="failure")\n'
        "    except TypeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_bound_parameter_in_exception_call_creates_no_impossible_prefix():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise RuntimeError(arguments)\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    except TypeError:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize(
    "binding",
    [
        "message = arguments",
        "import types as message",
        "message = helper\n    message = arguments",
    ],
)
def test_safe_name_bindings_survive_sequential_assignment_and_import(binding):
    source = _receiver_flow(
        f"patch = monkeypatch\n    {binding}\n"
        "    try:\n"
        "        raise RuntimeError(message)\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    except (NameError, TypeError):\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_deleted_safe_name_restores_the_evaluation_prefix():
    _assert_authoritative_receiver(
        "patch = monkeypatch\n"
        "    message = arguments\n"
        "    del message\n"
        "    try:\n"
        "        raise RuntimeError(message)\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_safe_name_branch_merges_by_intersection():
    _assert_authoritative_receiver(
        "patch = monkeypatch\n"
        "    if flag:\n"
        "        message = arguments\n"
        "    try:\n"
        "        raise RuntimeError(message)\n"
        "    except RuntimeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_an_unmatched_known_raise_still_reaches_finally():
    _assert_authoritative_receiver(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise ValueError\n"
        "    except TypeError:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_a_bare_reraise_from_a_handler_still_reaches_finally():
    _assert_authoritative_receiver(
        "patch = helper\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except RuntimeError:\n"
        "        patch = monkeypatch\n"
        "        raise\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_a_later_matching_handler_receives_the_known_raise():
    _assert_authoritative_receiver(
        "patch = helper\n"
        "    try:\n"
        "        raise ValueError\n"
        "    except TypeError:\n"
        "        patch = helper\n"
        "    except Exception:\n"
        "        patch = monkeypatch\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_a_nonmatching_handler_prefix_does_not_survive_a_later_match():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise ValueError\n"
        "    except TypeError:\n"
        "        patch = monkeypatch\n"
        "    except Exception:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_an_unknown_exception_keeps_the_non_exception_branch_alive():
    _assert_authoritative_receiver(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise error\n"
        "    except Exception:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_a_bare_handler_consumes_an_unknown_exception_kind():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise error\n"
        "    except:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


def test_unshadowed_base_exception_consumes_an_unknown_ordinary_exception():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    try:\n"
        "        raise error\n"
        "    except BaseException:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize(
    "declaration, handler",
    [
        ("Exception = custom_exception", "Exception"),
        ("", "errors.Exception"),
    ],
)
def test_uncertain_exception_spelling_does_not_consume_known_raise(
    declaration, handler
):
    prefix = "patch = monkeypatch\n"
    if declaration:
        prefix += f"    {declaration}\n"
    _assert_authoritative_receiver(
        prefix + "    try:\n"
        "        raise RuntimeError\n"
        f"    except {handler}:\n"
        "        patch = helper\n"
        "    finally:\n"
        "        patch.setattr(*arguments)"
    )


def test_class_local_exception_shadow_prevents_builtin_matching():
    source = """
async def test_shape(monkeypatch, helper, arguments):
    class Scope:
        patch = monkeypatch
        Exception = Sibling
        try:
            raise RuntimeError
        except Exception:
            patch = helper
        finally:
            patch.setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


@pytest.mark.parametrize(
    "import_statement",
    [
        "import builtins as bi",
        "if flag:\n        import builtins as bi",
    ],
)
def test_qualified_builtin_exception_identities_stay_unknown(import_statement):
    source = f"""
async def test_shape(monkeypatch, helper, arguments, flag):
    patch = monkeypatch
    {import_statement}
    try:
        raise RuntimeError
    except bi.Exception:
        patch = helper
    finally:
        patch.setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


def test_a_loop_retains_the_zero_iteration_receiver_path():
    _assert_authoritative_receiver(
        "patch = monkeypatch\n    for item in value:\n        patch = helper"
    )


def test_a_loop_revisits_calls_before_a_later_authority_assignment():
    source = """
async def test_shape(monkeypatch, helper, arguments, value):
    patch = helper
    for item in value:
        patch.setattr(*arguments)
        patch = monkeypatch
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


def test_a_loop_else_rebinding_retires_receiver_authority_without_a_break():
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    for item in value:\n"
        "        patch = helper\n"
        "    else:\n"
        "        patch = helper"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize("transfer", ["break", "continue"])
def test_loop_transfers_keep_their_exact_authoritative_source(transfer):
    _assert_authoritative_receiver(
        "patch = helper\n"
        "    for item in value:\n"
        "        if flag:\n"
        "            patch = monkeypatch\n"
        f"            {transfer}\n"
        "            patch = helper\n"
        "        patch = helper"
    )


@pytest.mark.parametrize("transfer", ["break", "continue"])
def test_loop_transfers_keep_definitely_retired_source_states(transfer):
    source = _receiver_flow(
        "patch = helper\n"
        "    for item in value:\n"
        "        if flag:\n"
        "            patch = monkeypatch\n"
        "            patch = helper\n"
        f"            {transfer}\n"
        "            patch = monkeypatch\n"
        "        patch = helper"
    )

    assert _scan_synthetic(source) == []


@pytest.mark.parametrize(
    "cases",
    [
        "case True:\n            patch = monkeypatch\n"
        "        case False:\n            patch = helper",
        "case True:\n            patch = helper\n"
        "        case False:\n            patch = monkeypatch",
    ],
)
def test_match_cases_merge_receiver_authority_in_either_order(cases):
    _assert_authoritative_receiver(f"patch = helper\n    match flag:\n        {cases}")


@pytest.mark.parametrize("catch_all", ["_", "ignored"])
def test_exhaustive_match_rebinding_retires_receiver_authority(catch_all):
    source = _receiver_flow(
        "patch = monkeypatch\n"
        "    match flag:\n"
        "        case True:\n"
        "            patch = helper\n"
        f"        case {catch_all}:\n"
        "            patch = helper"
    )

    assert _scan_synthetic(source) == []


def test_a_conditional_receiver_merge_reaches_a_nested_closure():
    source = """
async def test_outer(monkeypatch, helper, arguments, flag):
    patch = helper
    if flag:
        patch = monkeypatch

    async def test_inner():
        patch.setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


def test_a_class_local_receiver_does_not_become_a_method_closure():
    source = """
import pytest

class Scope:
    patch = pytest.MonkeyPatch()

    def method(self, arguments):
        patch.setattr(*arguments)
"""

    assert _scan_synthetic(source) == []


def test_a_method_still_closes_over_an_enclosing_function_receiver():
    source = """
async def test_outer(monkeypatch, arguments):
    patch = monkeypatch

    class Scope:
        patch = object()

        def method(self):
            patch.setattr(*arguments)
"""

    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"patch\.setattr\(\*arguments\): unresolved setattr arguments",
    ):
        _scan_synthetic(source)


@pytest.mark.parametrize(
    ("statement", "line"),
    [
        ('cap = extractor._capture\n    patch.object(cap, "{name}", replacement)', 8),
        ('patch.object(cap := extractor._capture, "{name}", replacement)', 7),
        (
            '(cap := extractor._capture)\n    patch.object(cap, "{name}", replacement)',
            8,
        ),
        (
            "patch.object(target=extractor._capture, "
            'attribute="{name}", new=replacement)',
            7,
        ),
        ('monkeypatch.setattr(extractor._capture, "{name}", replacement)', 7),
        (
            "monkeypatch.setattr(target=extractor._capture, "
            'name="{name}", value=replacement)',
            7,
        ),
        ('setattr(extractor._capture, "{name}", replacement)', 7),
        ("extractor._capture.{name} = replacement", 7),
        ("extractor._capture.{name}: object = replacement", 7),
    ],
)
def test_every_reach_through_shape_lands_on_the_collaborator(statement, line):
    # An alias, a walrus, both keyword forms, all three `setattr` spellings and
    # an assignment with or without an annotation reach the same collaborator
    # as `patch.object(extractor._capture, ...)`. A shape that produced no seam
    # also produced no error, so a name that has never existed on
    # `SectionCapture` read as a passing test.
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


def test_an_annotation_without_a_value_replaces_nothing():
    # `owner.name: T` declares a type and assigns nothing, so there is no
    # replacement to record and no member to validate. Reading it as a patch
    # would refuse a name the file never claims exists, while the reach-through
    # above it stays on the record either way.
    seams = _scan_synthetic(_reach_through("extractor._capture.made_up: object"))

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]
    assert [seam.target for seam in seams if seam.kind == "private_facade_access"] == [
        "_capture"
    ]


@pytest.mark.parametrize(
    "rebinding",
    [
        "cap = thing",
        "cap, pair = thing",
        "cap += thing",
        "(cap := thing)",
        "for cap in thing:\n        pass",
        "with thing as cap:\n        pass",
    ],
)
def test_a_rebound_alias_stops_naming_the_collaborator(rebinding):
    # `cap` holds the reach-through only until the next binding, whichever
    # spelling makes it. A map collected over the whole function keeps
    # answering `_capture` for every later use, which books a patch on a
    # foreign object as a `SectionCapture` seam and refuses an unknown member
    # of it by name. That is the safer direction of the two, and still wrong:
    # one false failure here blocks every stage behind it.
    seams = _scan_synthetic(
        _reach_through(
            "cap = extractor._capture\n"
            f"    {rebinding}\n"
            '    patch.object(cap, "_extract_overlay", replacement)'
        )
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


def test_an_alias_bound_after_a_foreign_one_still_resolves():
    # The retirement has to follow the order the statements are written in
    # rather than switch the alias off, or the shape that motivated the alias
    # stops resolving along with the stale answer.
    seams = _scan_synthetic(
        _reach_through(
            "cap = thing\n"
            "    cap = extractor._capture\n"
            '    patch.object(cap, "_extract_overlay", replacement)'
        )
    )

    assert [
        (seam.kind, seam.target, seam.canonical_owner, seam.migration_stage)
        for seam in seams
        if seam.kind.endswith("_patch_object")
    ] == [("private_patch_object", "_extract_overlay", "capture.SectionCapture", 6)]


@pytest.mark.parametrize(
    ("statement", "line"),
    [
        (
            "cap = extractor._capture\n"
            "    if flag:\n"
            "        cap = thing\n"
            '    patch.object(cap, "_extract_overlay", replacement)',
            10,
        ),
        (
            "for item in thing:\n"
            '        patch.object(cap, "_extract_overlay", replacement)\n'
            "        cap = extractor._capture",
            8,
        ),
    ],
)
def test_a_conditionally_rebound_alias_names_its_call_site(statement, line):
    # No single answer fits a name that held the collaborator down one path and
    # something else down another, and a loop body is the same question asked
    # about its own previous iteration. Keeping the stale answer or dropping it
    # both invent one, so the call site is named instead, the way every other
    # ambiguous binding in this scanner is.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=rf"synthetic_inventory\.py:{line} cap: "
        r"ambiguous collaborator alias after conditional control flow",
    ):
        _scan_synthetic(_reach_through(statement))


def _owner_test(body: str) -> str:
    return (
        "\nfrom unittest.mock import patch\n"
        "\nfrom linkedin_mcp_server.scraping.extractor import LinkedInExtractor\n"
        "\n\nasync def test_owner(page, replacement):\n"
        f"{body}\n"
    )


def test_an_owner_factory_binding_exempts_only_its_own_wiring():
    # `_scraper` builds the migrated owner, whose instance carries the same
    # `_capture` the facade wires, so the wiring signal has to let this one
    # through. Every remaining stage is written this way, and a refusal here
    # would block all of them.
    seams = migration.scan_source(
        PERSON_TESTS,
        _owner_test(
            "    scraper = _scraper(page)\n"
            '    patch.object(scraper._capture, "extract_page", replacement)'
        ),
        *migration.extractor_methods(),
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


@pytest.mark.parametrize(
    ("rebinding", "line"),
    [
        ("    for scraper in (LinkedInExtractor(page),):\n        pass", 11),
        ("    with LinkedInExtractor(page) as scraper:\n        pass", 11),
        ("    try:\n        pass\n    except Exception as scraper:\n        pass", 13),
        ("    scraper, other = LinkedInExtractor(page), None", 10),
    ],
)
def test_a_rebound_owner_name_loses_its_factory_exemption(rebinding, line):
    # The exemption answers for a name the factory alone binds. A `for`, a
    # `with`, an `except` or an unpack binds it again without ever being a
    # facade binding, so the name signal never sees those shapes and the
    # wiring signal is the only guard over `scraper._capture`. Left exempt,
    # a stage-8 test comparing owner and facade through one loop variable
    # would take its facade reach-through out of the inventory entirely, and
    # stage 12 would retire nothing for it.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=rf"test_person\.py:{line} scraper\._capture: "
        r"unresolved patch\.object target",
    ):
        migration.scan_source(
            PERSON_TESTS,
            _owner_test(
                "    scraper = _scraper(page)\n"
                f"{rebinding}\n"
                '    patch.object(scraper._capture, "extract_page", replacement)'
            ),
            *migration.extractor_methods(),
        )


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
