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

# Stage 12 moved the last shared-boundary caller off the facade. The owner
# tables remain so any patch that reappears is dated instead of unresolved.
_SHARED_BOUNDARY_STAGES: dict[str, set[int]] = {}


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
    assert {seam["kind"] for seam in current["seams"]} == {"permanent_alias_import"}
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
    # `module_rebind_patch` was the job budget tests replacing the facade's
    # whole `time` module, and all five retired with the job owner. The kind
    # is dropped from the required set rather than from the checker: a rebind
    # that comes back has to be inventoried against the workflow driving it.
    assert not [
        seam for seam in current["seams"] if seam["kind"] == "module_rebind_patch"
    ]
    assert "time" in migration._IMPORTED_MODULE_NAMES
    # `public_patch_object` went the same way with the post search, which held
    # the last six. Same reasoning: the kind leaves the required set, not the
    # checker, so a facade delegate patched again in place of its owner is
    # inventoried rather than unseen.
    assert not [
        seam for seam in current["seams"] if seam["kind"] == "public_patch_object"
    ]
    assert migration._WORKFLOW_OWNERS["extract_page"] == ("capture.SectionCapture", 4)


def test_manifest_has_no_obsolete_production_callers():
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert not [
        seam
        for seam in current["seams"]
        if seam["path"].startswith("linkedin_mcp_server/")
    ]


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
    # Both DOM suites now build MessageSender directly, so owner-local imports,
    # patches and private calls are outside the facade seam inventory.
    assert dom_seams == []
    # The URN read moved to `profile_page.ProfilePageReader` at stage 6, and
    # that reader now receives the owner-local message-target read directly.
    profile_urn_reads = [
        seam for seam in current if seam["target"] == "_extract_profile_urn"
    ]
    assert profile_urn_reads == []
    assert migration._PRIVATE_OWNERS["_extract_profile_urn"] == (
        "profile_page.ProfilePageReader",
        6,
    )
    assert not [
        seam
        for seam in current
        if seam["canonical_owner"].startswith("message_sender.")
    ]

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
    assert boundary == []

    # Neither scroll has a consumer left on the facade now that the job
    # reader imports both, so the patches and the direct reads that sized
    # them are gone together, and so is the issue-report binding. Closed by
    # relocating the reads rather than by dropping `_BOUNDARY_OWNERS`
    # entries: one that comes back has to be dated against its caller instead
    # of failing closed as unknown.
    # `handle_modal_close` and `build_references` joined them with the
    # conversation reader: it closes modals through `ScrapingSession` and
    # builds its references from `link_metadata` directly, so neither name is
    # bound in `scraping.extractor` any more.
    retired = (
        "detect_rate_limit",
        "scroll_to_bottom",
        "scroll_job_sidebar",
        "build_issue_diagnostics",
        "handle_modal_close",
        "build_references",
    )
    assert not [seam for seam in current["seams"] if seam["target"] in retired]
    assert all(name in migration._BOUNDARY_OWNERS for name in retired)

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
    assert stdlib == []
    assert {"asyncio", "time"} <= migration._IMPORTED_MODULE_NAMES
    # The last one was a `get_sidebar_profiles` test reading the facade's
    # logger, and it moved to the person owner at stage 6. Closed by
    # relocating the read rather than by dropping `logger` from
    # `_CONTEXTUAL_MODULE_NAMES`: the next such patch has to be inventoried
    # against the workflow that drives it, not fail closed as unknown.
    assert logger_patches == []
    assert "logger" in migration._CONTEXTUAL_MODULE_NAMES


def test_a_shared_boundary_patch_is_dated_once_per_consuming_stage(monkeypatch):
    """One site, one seam per owner that still consumes the binding.

    The live tree no longer spans two stages here: `detect_rate_limit` is down
    to `send_message` alone now that the conversation reader takes the check
    through `ScrapingSession`, and a single-consumer site proves nothing about
    a reader that returned only the earliest owner. Restoring one retired
    consumer is what keeps the mechanism measured — `_callers` has to answer
    with both owners, or the site closes at stage 11 while `send_message`
    still needs it.
    """
    key = ("tests/scraping/policy_scenarios.py", "boundaries", "detect_rate_limit")
    owners = migration._WORKFLOW_OWNERS | migration._PRIVATE_OWNERS
    restored = ("get_conversation", "send_message")
    monkeypatch.setitem(migration._EXPLICIT_CALLER_CONTEXTS, key, restored)
    source = (
        POLICY_SCENARIOS.read_text(encoding="utf-8")
        .replace(
            "from linkedin_mcp_server.scraping import feed as feed_module\n",
            "from linkedin_mcp_server.scraping import extractor as extractor_module\n"
            "from linkedin_mcp_server.scraping import feed as feed_module\n",
        )
        .replace(
            '        patch.object(session_module, "detect_rate_limit", rate_limit),\n',
            '        patch.object(session_module, "detect_rate_limit", rate_limit),\n'
            '        patch.object(extractor_module, "detect_rate_limit", rate_limit),\n',
        )
    )

    publics, privates = migration.extractor_methods()
    seams = migration.scan_source(
        POLICY_SCENARIOS,
        source,
        publics,
        privates,
    )

    assert {owners[name][1] for name in restored} == {11, 12}
    assert {
        seam.migration_stage
        for seam in seams
        if seam.kind == "boundary_patch_object" and seam.target == "detect_rate_limit"
    } == {11, 12}


def test_the_last_public_facade_patch_retired_with_the_post_search():
    """No workflow reaches its collaborator through a facade delegate now.

    The six that remained were the post-search tests patching `extract_page`
    on the facade, and they moved to `PostSearch` with the workflow. Every
    table entry below stays for the reason each retired one does: a public
    patch that reappears has to be dated against the workflow driving it
    rather than fail closed as an unknown target.
    """
    current = json.loads(MANIFEST.read_text(encoding="utf-8"))
    public = [
        seam for seam in current["seams"] if seam["kind"] == "public_patch_object"
    ]

    assert public == []
    assert migration._WORKFLOW_OWNERS["extract_page"] == ("capture.SectionCapture", 4)
    assert migration._WORKFLOW_OWNERS["search_posts"] == ("posts.PostSearch", 10)
    # `search_companies` was the last one before them, and the two traces that mutated it
    # now mutate `CompanyScraper` instead. Its table entry stays for the same
    # reason the others do.
    assert not [seam for seam in public if seam["target"] == "search_companies"]
    assert migration._WORKFLOW_OWNERS["search_companies"] == (
        "company.CompanyScraper",
        8,
    )
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


def test_checker_accepts_the_completed_facade_migration():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--check", "--stage", "14"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "obsolete at stage 14:" not in result.stderr


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
        extractor._navigator = replacement

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
    ] == [("_navigator", "navigation.PageNavigator", 3)]


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


def test_the_ambient_scroll_field_left_no_reach_through_behind():
    """`_scroll_seconds` is gone from the facade, and so are its two readers.

    Both sat inside a `with` block in a job test and reached the facade from
    a nested closure, which is the shape the synthetic case above covers in
    general. The table entry stays for the reason every other retired one
    does: a reach-through that reappears has to be dated at the facade's own
    stage rather than fail closed as an unknown private attribute.
    """
    accesses = [
        seam
        for seam in migration.scan()["seams"]
        if seam["target"] == "_scroll_seconds"
    ]

    assert accesses == []
    assert migration._INSTANCE_ATTRIBUTE_OWNERS["_scroll_seconds"] == (
        "facade.LinkedInExtractor._scroll_seconds",
        14,
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
    # Message tests now import their programs from the owner module, so no
    # private program read remains through the facade.
    assert private_reads == []
    direct_privates = [
        seam
        for seam in current["seams"]
        if seam["kind"] == "direct_import" and seam["target"].startswith("_")
    ]
    # Every private program import now comes from its owner module.
    assert direct_privates == []
    assert not [seam for seam in current["seams"] if seam["target"] == "_JOB_IDS_JS"]
    assert migration._IMPORT_OWNERS["_JOB_IDS_JS"] == ("job_pages.JOB_IDS_JS", 9)
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
        [sys.executable, str(CHECKER), "--check", "--stage", "14"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert migration._MODULE_ATTRIBUTE_OWNERS["_MESSAGING_COMPOSE_SELECTOR"] == (
        "message_sender.MESSAGE_COMPOSE_SELECTOR",
        12,
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


def _owner_test(
    body: str, *, factory: str = "def _scraper(page):\n    return page"
) -> str:
    # The factory has to be here. The exemption is granted against the
    # definition the module actually holds, so a fragment that only calls
    # `_scraper` describes a file whose entry in `_OWNER_FACTORIES` names
    # nothing, which is its own refusal.
    return (
        "\nfrom unittest.mock import patch\n"
        "\nfrom linkedin_mcp_server.scraping.extractor import LinkedInExtractor\n"
        f"\n\n{factory}\n"
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
        ("    for scraper in (LinkedInExtractor(page),):\n        pass", 15),
        ("    with LinkedInExtractor(page) as scraper:\n        pass", 15),
        ("    try:\n        pass\n    except Exception as scraper:\n        pass", 17),
        ("    scraper, other = LinkedInExtractor(page), None", 14),
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


@pytest.mark.parametrize(
    "factory",
    [
        "def _scraper(page):\n    return page\n\n\ndef _scraper(page):\n    return page",
        "def _scraper(page):\n    return page\n\n\n_scraper = LinkedInExtractor",
        "from helpers import _scraper",
        # Decorated: the name ends up bound to whatever came back, and this
        # decorator returns the facade. Nothing about the `def` says so.
        "def replace(function):\n"
        "    return LinkedInExtractor\n"
        "\n\n@replace\n"
        "def _scraper(page):\n    return page",
        # A `global` assignment rebinds the module name from inside a scope
        # the module-level collector never enters.
        "def _scraper(page):\n    return page\n\n\n"
        "def poison():\n    global _scraper\n    _scraper = LinkedInExtractor",
        # A walrus in a signature is evaluated by the scope holding the `def`,
        # so this binds at module level while the collector reads only `poison`.
        "def _scraper(page):\n    return page\n\n\n"
        "def poison(value=(_scraper := LinkedInExtractor)):\n    return value",
        # Lambda defaults are also evaluated by the containing scope; the
        # lambda body is the only part that gets its own scope.
        "def _scraper(page):\n    return page\n\n\n"
        "poison = lambda value=(_scraper := LinkedInExtractor): value",
        # The lambda can itself sit inside another definition-time expression;
        # its defaults still run in that expression's containing scope.
        "def _scraper(page):\n    return page\n\n\n"
        "def poison(\n"
        "    outer=(lambda value=(_scraper := LinkedInExtractor): value)\n"
        "):\n"
        "    return outer",
    ],
)
def test_a_module_level_factory_shadow_names_its_entry(factory):
    # The exemption was granted on the spelling, so whichever `_scraper` a
    # call reaches carried it, including one the module rebound to the facade
    # itself. Which binding a call site reaches is a question about execution
    # order, and a reader that cannot answer it has to refuse rather than pick.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"test_person\.py:\d+ _scraper: "
        r"owner factory is not a single undecorated module-level definition",
    ):
        migration.scan_source(
            PERSON_TESTS,
            _owner_test(
                "    scraper = _scraper(page)\n"
                '    patch.object(scraper._capture, "extract_page", replacement)',
                factory=factory,
            ),
            *migration.extractor_methods(),
        )


def test_a_nested_local_walrus_does_not_rebind_the_module_factory():
    # The default is evaluated in `outer`, not at module level. Without a
    # `global` declaration it shadows only that function's local name, so a
    # sibling owner test still reaches the declared module factory.
    seams = migration.scan_source(
        PERSON_TESTS,
        _owner_test(
            "    scraper = _scraper(page)\n"
            '    patch.object(scraper._capture, "extract_page", replacement)',
            factory="def _scraper(page):\n    return page\n\n\n"
            "def outer():\n"
            "    def inner(value=(_scraper := LinkedInExtractor)):\n"
            "        return value\n"
            "    return inner",
        ),
        *migration.extractor_methods(),
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


def test_a_nested_default_shadows_the_factory_in_its_enclosing_function():
    # The inner function's default runs while `test_owner` defines it. Its
    # walrus therefore binds a local `_scraper` in `test_owner`, and the later
    # call constructs the facade rather than the module-level owner.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"test_person\.py:16 scraper\._capture: "
        r"unresolved patch\.object target",
    ):
        migration.scan_source(
            PERSON_TESTS,
            _owner_test(
                "    def poison(value=(_scraper := LinkedInExtractor)):\n"
                "        return value\n"
                "\n"
                "    scraper = _scraper(page)\n"
                '    patch.object(scraper._capture, "extract_page", replacement)'
            ),
            *migration.extractor_methods(),
        )


def test_a_nested_lambda_default_shadows_its_enclosing_function():
    # Visiting the outer function's default reaches the nested lambda, whose
    # own default is still evaluated in `test_owner`. Stopping at the lambda
    # silently restores the owner exemption for a facade-producing call.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"test_person\.py:18 scraper\._capture: "
        r"unresolved patch\.object target",
    ):
        migration.scan_source(
            PERSON_TESTS,
            _owner_test(
                "    def poison(\n"
                "        outer=(lambda value=(_scraper := LinkedInExtractor): value)\n"
                "    ):\n"
                "        return outer\n"
                "\n"
                "    scraper = _scraper(page)\n"
                '    patch.object(scraper._capture, "extract_page", replacement)'
            ),
            *migration.extractor_methods(),
        )


def test_a_global_declaration_without_a_write_keeps_the_factory():
    # `global` controls name resolution. It does not rebind anything by
    # itself, so the call still reaches the honored module factory.
    seams = migration.scan_source(
        PERSON_TESTS,
        _owner_test(
            "    global _scraper\n"
            "    scraper = _scraper(page)\n"
            '    patch.object(scraper._capture, "extract_page", replacement)'
        ),
        *migration.extractor_methods(),
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


def test_a_nested_factory_shadow_loses_the_exemption_where_it_shadows():
    # A `def _scraper` inside one test is invisible at module level, so the
    # module check cannot see it and the frame chain has to. Measured against
    # the pre-fix checker on the real file: both patches passed with exit 0,
    # which is the inventory losing a facade reach-through in silence.
    with pytest.raises(
        migration.UnresolvedSeamError,
        match=r"test_person\.py:16 scraper\._capture: "
        r"unresolved patch\.object target",
    ):
        migration.scan_source(
            PERSON_TESTS,
            _owner_test(
                "    def _scraper(page):\n"
                "        return LinkedInExtractor(page)\n"
                "\n"
                "    scraper = _scraper(page)\n"
                '    patch.object(scraper._capture, "extract_page", replacement)'
            ),
            *migration.extractor_methods(),
        )


def test_a_sibling_test_keeps_the_exemption_a_nested_shadow_loses():
    # The narrowing is per scope, not per file. A shadow in one test may not
    # cost the other 44 call sites their exemption, or the fix trades one
    # silent pass for a refusal that blocks every later stage.
    seams = migration.scan_source(
        PERSON_TESTS,
        _owner_test(
            "    def _scraper(page):\n"
            "        return LinkedInExtractor(page)\n"
            "\n"
            "    scraper = _scraper(page)\n"
            "\n"
            "\n"
            "async def test_sibling(page, replacement):\n"
            "    scraper = _scraper(page)\n"
            '    patch.object(scraper._capture, "extract_page", replacement)'
        ),
        *migration.extractor_methods(),
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


def test_a_class_branch_copy_keeps_both_factory_views():
    frame = migration._ScopeFrame(
        kind="class",
        module_names=set(),
        class_names=set(),
        instance_names=set(),
        closure_module_names=set(),
        closure_class_names=set(),
        closure_instance_names=set(),
        owner_factories=frozenset({"current_factory"}),
        closure_owner_factories=frozenset({"enclosing_factory"}),
    )

    copied = migration.Scanner._copy_frame(frame)

    assert copied.owner_factories == frozenset({"current_factory"})
    assert copied.closure_owner_factories == frozenset({"enclosing_factory"})


@pytest.mark.parametrize(
    "branch",
    [
        "    if os.environ.get('FLAG'):\n{test}\n    else:\n        pass\n",
        "    try:\n{test}\n    except Exception:\n        pass\n",
    ],
    ids=["if", "try"],
)
def test_a_class_branch_keeps_the_factory_its_class_body_shadows(branch):
    test = (
        "        async def test_owner(self, page, replacement):\n"
        "            scraper = _scraper(page)\n"
        "            patch.object("
        'scraper._capture, "extract_page", replacement)'
    )
    seams = migration.scan_source(
        PERSON_TESTS,
        "\nimport os\n"
        "from unittest.mock import patch\n"
        "\nfrom linkedin_mcp_server.scraping.extractor import LinkedInExtractor\n"
        "\n\ndef _scraper(page):\n    return page\n"
        "\n\nclass TestOwner:\n"
        "    _scraper = object()\n" + branch.format(test=test),
        *migration.extractor_methods(),
    )

    assert not [seam for seam in seams if seam.kind.endswith("_patch_object")]


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
