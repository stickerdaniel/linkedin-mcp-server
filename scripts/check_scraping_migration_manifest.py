#!/usr/bin/env python3
"""Check the extractor patch/import migration inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Any

import argparse
import ast
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
PACKAGE = ROOT / "linkedin_mcp_server"
FIXTURE_ROOT = TESTS / "fixtures" / "scraping-policy"
MANIFEST = FIXTURE_ROOT / "migration-manifest.json"
EXTRACTOR_MODULE = "linkedin_mcp_server.scraping.extractor"

# Public-named contracts that keep a permanent identity alias in
# scraping.extractor. An import through the alias reaches the same object as an
# import from the canonical owner, so it never goes obsolete. A *patch* against
# the alias still does, which is why only direct imports are exempted here.
PERMANENT_ALIASES = {
    "ExtractedSection": "contracts.ExtractedSection",
    "FilterValidationError": "contracts.FilterValidationError",
    "rate_limited_section_error": "contracts.rate_limited_section_error",
    "strip_linkedin_noise": "text.strip_linkedin_noise",
    "strip_conversation_chrome": "text.strip_conversation_chrome",
}

_PRIVATE_OWNERS: dict[str, tuple[str, int]] = {
    "_message_action_result": ("contracts.message_action_result", 1),
    "_build_job_search_url": ("search_urls.build_job_search_url", 2),
    "_build_content_search_url": ("search_urls.build_content_search_url", 2),
    "_normalize_body_marker": ("navigation.PageNavigator", 3),
    "_log_navigation_failure": ("navigation.PageNavigator", 3),
    "_raise_if_auth_barrier": ("navigation.PageNavigator", 3),
    "_goto_with_auth_checks": ("navigation.PageNavigator", 3),
    "_navigate_to_page": ("navigation.PageNavigator", 3),
    "_watching_navigations": ("navigation.PageNavigator", 3),
    "_document_origin": ("navigation.PageNavigator", 3),
    "_settle_navigation": ("navigation.PageNavigator", 3),
    "_extract_loaded_section": ("capture.SectionCapture", 4),
    "_extract_overlay": ("capture.SectionCapture", 4),
    "_extract_overlay_once": ("capture.SectionCapture", 4),
    "_extract_root_content": ("content.PageContentReader", 4),
    "_extract_feed_once": ("feed.FeedScraper", 5),
    "_extract_feed_body": ("feed.FeedScraper", 5),
    "_extract_profile_urn": ("profile_page.ProfilePageReader", 6),
    "_read_action_signals": ("connection_actions.ConnectionActions", 7),
    "_dialog_is_open": ("connection_actions.ConnectionActions", 7),
    "_click_dialog_primary_button": ("connection_actions.ConnectionActions", 7),
    "_get_premium_upsell_message": ("connection_actions.ConnectionActions", 7),
    "_fill_dialog_textarea": ("connection_actions.ConnectionActions", 7),
    "_submit_invite_dialog": ("connection_actions.ConnectionActions", 7),
    "_probe_invite_note_limit": ("connection_actions.ConnectionActions", 7),
    "_open_more_menu": ("connection_actions.ConnectionActions", 7),
    "_click_incoming_accept": ("connection_actions.ConnectionActions", 7),
    "_dismiss_dialog": ("connection_actions.ConnectionActions", 7),
    "_extract_search_page": ("job_pages.JobPageReader", 9),
    "_extract_search_page_once": ("job_pages.JobPageReader", 9),
    "_extract_saved_jobs_page": ("job_pages.JobPageReader", 9),
    "_extract_saved_jobs_page_once": ("job_pages.JobPageReader", 9),
    "_extract_job_ids": ("job_pages.JobPageReader", 9),
    "_get_total_search_pages": ("job_pages.JobPageReader", 9),
    "_get_total_list_pages": ("job_pages.JobPageReader", 9),
    "_wait_for_main_text": ("conversations.ConversationReader", 11),
    "_scroll_main_scrollable_region": ("conversations.ConversationReader", 11),
    "_extract_conversation_thread_refs": ("conversations.ConversationReader", 11),
    "_resolve_conversation_thread_urls": ("conversations.ConversationReader", 11),
    "_open_conversation_by_username": ("conversations.ConversationReader", 11),
    "_strip_select_conversation_prefix": (
        "conversations.strip_select_conversation_prefix",
        11,
    ),
    "_read_profile_display_name": ("profile_page.ProfilePageReader", 6),
    "_read_profile_message_target": ("message_sender.MessageSender", 12),
    "_resolve_message_compose_href": ("message_sender.MessageSender", 12),
    "_wait_for_message_surface": ("message_sender.MessageSender", 12),
    "_wait_for_message_composer": ("message_sender.MessageSender", 12),
    "_resolve_message_compose_box": ("message_sender.MessageSender", 12),
    "_message_target_argument": ("message_sender.MessageSender", 12),
    "_read_message_composer_state": ("message_sender.MessageSender", 12),
    "_focus_verified_message_editor": ("message_sender.MessageSender", 12),
    "_write_verified_message": ("message_sender.MessageSender", 12),
    "_wait_for_verified_submit": ("message_sender.MessageSender", 12),
    "_submit_verified_message": ("message_sender.MessageSender", 12),
    "_cleanup_owned_message": ("message_sender.MessageSender", 12),
    "_resolve_message_owner": ("message_sender.MessageSender", 12),
    "_dispose_message_owner": ("message_sender.MessageSender", 12),
    "_message_confirmation_argument": ("message_sender.MessageSender", 12),
    "_prepare_message_confirmation": ("message_sender.MessageSender", 12),
    "_message_send_confirmed": ("message_sender.MessageSender", 12),
    "_dispose_message_confirmation": ("message_sender.MessageSender", 12),
    "_drain_listener_tasks": ("feed.FeedScraper", 5),
}

# Module-level names patched through scraping.extractor. These aliases remain
# while unmoved workflows still call them, so the seam's stage comes from the
# caller that consumes the binding rather than from the stage that first
# introduces the canonical helper.
_BOUNDARY_OWNERS = {
    "record_page_trace": "navigation.PageNavigator",
    "detect_auth_barrier": "navigation.PageNavigator",
    "detect_auth_barrier_quick": "navigation.PageNavigator",
    "resolve_remember_me_prompt": "navigation.PageNavigator",
    "stabilize_navigation": "navigation.PageNavigator",
    "detect_rate_limit": "session.ScrapingSession.check_rate_limit",
    "handle_modal_close": "session.ScrapingSession.dismiss_modal",
    "scroll_to_bottom": "session.ScrapingSession.scroll_body",
    "scroll_job_sidebar": "session.ScrapingSession.scroll_job_sidebar",
    "build_issue_diagnostics": "error_diagnostics.build_issue_diagnostics",
    "strip_linkedin_noise": "text.strip_linkedin_noise",
    "build_references": "link_metadata.build_references",
}

_IMPORT_OWNERS = {
    "LinkedInExtractor": ("facade.LinkedInExtractor", 14),
    "_ACTION_SIGNALS_JS": ("connection_actions.ACTION_SIGNALS_JS", 7),
    "_CLICK_INCOMING_ACCEPT_JS": ("connection_actions.CLICK_INCOMING_ACCEPT_JS", 7),
    "_JOB_IDS_JS": ("job_pages.JOB_IDS_JS", 9),
    "SEND_INTERRUPTED_WARNING": ("contracts.SEND_INTERRUPTED_WARNING", 1),
    "refuse_an_invalid_message": ("contracts.refuse_an_invalid_message", 1),
    "_drain_listener_tasks": ("feed.FeedScraper._drain_listener_tasks", 5),
    "_PROFILE_MESSAGE_TARGET_JS": ("message_sender.PROFILE_MESSAGE_TARGET_JS", 12),
    "_MESSAGE_COMPOSER_STATE_JS": ("message_sender.MESSAGE_COMPOSER_STATE_JS", 12),
    "_MESSAGE_COMPOSER_OWNER_JS": ("message_sender.MESSAGE_COMPOSER_OWNER_JS", 12),
    "_MESSAGE_CONFIRMATION_PREPARE_JS": (
        "message_sender.MESSAGE_CONFIRMATION_PREPARE_JS",
        12,
    ),
    "_MESSAGE_CONFIRMATION_READY_JS": (
        "message_sender.MESSAGE_CONFIRMATION_READY_JS",
        12,
    ),
    "_MESSAGE_CONFIRMATION_DISPOSE_JS": (
        "message_sender.MESSAGE_CONFIRMATION_DISPOSE_JS",
        12,
    ),
    "_ProfileMessageTarget": ("message_sender.ProfileMessageTarget", 12),
    "_ProfileMessageTargetResolution": (
        "message_sender.ProfileMessageTargetResolution",
        12,
    ),
}

# Facade wiring a test reaches but never owns. These carry no consumer of their
# own, so each one closes with the module that owns it.
_INSTANCE_ATTRIBUTE_OWNERS = {
    "_page": ("facade.LinkedInExtractor._page", 14),
    "_session": ("session.ScrapingSession", 3),
    "_navigator": ("navigation.PageNavigator", 3),
    "_scroll_seconds": ("facade.LinkedInExtractor._scroll_seconds", 14),
}

# `_content` and `_capture` are the facade's own wiring rather than a
# collaborator a test could build for itself: reaching one is how a workflow
# still living on the facade gets its reader stubbed. Each entry names the
# collaborator the reach-through lands on and the facade binding the
# reach-through itself consumes.
#
# Both the reach-through and the patch it carries expire with the workflow the
# *call site* drives, never with a flat per-attribute maximum: the moment
# `scrape_company` owns its own reader, a `_content` stub inside a
# `scrape_company` test intercepts nothing, while a `get_inbox` test reaching
# the same attribute is still live. Measured over every reach-through in the
# tree: all 19 `_capture` sites drive `scrape_person` (6), and the 14 `_content`
# sites split across `scrape_company` (8), `_extract_search_page` and
# `search_jobs` (9) and `get_inbox`, `get_conversation`,
# `search_conversations` (11). A single pin at 11 left the one stage-8 and the
# four stage-9 reach-throughs unflagged for three and two stages; a pin at the
# facade's own stage 14 would leave every one of them unflagged for the rest of
# the migration. The stage therefore comes from `_callers`, exactly as a
# boundary patch's does.
_FACADE_COLLABORATORS = {
    "_content": ("content.PageContentReader", "facade.LinkedInExtractor._content"),
    "_capture": ("capture.SectionCapture", "facade.LinkedInExtractor._capture"),
}

_MODULE_ATTRIBUTE_OWNERS = {
    "_URL_SETTLE_LAG": ("navigation.PageNavigator._URL_SETTLE_LAG", 3),
    "_URL_SETTLE_QUIET": ("navigation.PageNavigator._URL_SETTLE_QUIET", 3),
    "_MESSAGING_COMPOSE_SELECTOR": ("message_sender.MESSAGE_COMPOSE_SELECTOR", 12),
    "_PROFILE_MESSAGE_TARGET_JS": ("message_sender.PROFILE_MESSAGE_TARGET_JS", 12),
    "_ProfileMessageTarget": ("message_sender.ProfileMessageTarget", 12),
    "_ProfileMessageTargetResolution": (
        "message_sender.ProfileMessageTargetResolution",
        12,
    ),
    "_profile_urn_from_compose_url": (
        "message_sender.profile_urn_from_compose_url",
        12,
    ),
    "_profile_path_from_url": ("message_sender.profile_path_from_url", 12),
    "_message_page_url_is_safe": ("message_sender.message_page_url_is_safe", 12),
}

_WORKFLOW_OWNERS: dict[str, tuple[str, int]] = {
    "extract_page": ("capture.SectionCapture", 4),
    "extract_feed": ("feed.FeedScraper", 5),
    "scrape_person": ("person.PersonScraper", 6),
    "get_my_profile": ("person.PersonScraper", 6),
    "get_sidebar_profiles": ("person.PersonScraper", 6),
    "search_people": ("person.PersonScraper", 6),
    "connect_with_person": ("connection_actions.ConnectionActions", 7),
    "scrape_company": ("company.CompanyScraper", 8),
    "get_company_employees": ("company.CompanyScraper", 8),
    "search_companies": ("company.CompanyScraper", 8),
    "scrape_job": ("jobs.JobScraper", 9),
    "search_jobs": ("jobs.JobScraper", 9),
    "get_saved_jobs": ("jobs.JobScraper", 9),
    "search_posts": ("posts.PostSearch", 10),
    "get_inbox": ("conversations.ConversationReader", 11),
    "get_conversation": ("conversations.ConversationReader", 11),
    "search_conversations": ("conversations.ConversationReader", 11),
    "send_message": ("message_sender.MessageSender", 12),
    "get_page_text": ("facade.LinkedInExtractor compatibility method", 14),
    "click_button_by_text": ("facade.LinkedInExtractor compatibility method", 14),
    "_goto_with_auth_checks": ("navigation.PageNavigator", 3),
    "_extract_page_once": ("capture.SectionCapture", 4),
    "_extract_feed_once": ("feed.FeedScraper", 5),
    "_watching_navigations": ("navigation.PageNavigator", 3),
    "_document_origin": ("navigation.PageNavigator", 3),
    "_settle_navigation": ("navigation.PageNavigator", 3),
    "_extract_search_page": ("job_pages.JobPageReader", 9),
    "_extract_saved_jobs_page": ("job_pages.JobPageReader", 9),
    "_open_conversation_by_username": ("conversations.ConversationReader", 11),
}

# These helpers deliberately hold patches for callers selected outside their
# own body. Keying by file and function keeps class renames irrelevant; a
# helper/function rename becomes an unresolved seam instead of silently
# changing its migration stage.
_EXPLICIT_INSTANCE_BINDINGS = {
    ("tests/test_scraping.py", "_patch_to_composer"): ("extractor",),
}

_EXPLICIT_CALLER_CONTEXTS: dict[tuple[str, str, str], tuple[str, ...]] = {
    (
        "tests/test_scraping.py",
        "_calls",
        "extract_page",
    ): (
        "scrape_person",
        "connect_with_person",
        "get_sidebar_profiles",
        "_open_conversation_by_username",
        "send_message",
        "scrape_company",
        "get_company_employees",
        "scrape_job",
        "get_conversation",
    ),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "record_page_trace",
    ): ("_goto_with_auth_checks",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "detect_auth_barrier_quick",
    ): ("_goto_with_auth_checks",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "detect_auth_barrier",
    ): ("_goto_with_auth_checks",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "resolve_remember_me_prompt",
    ): ("_goto_with_auth_checks",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "stabilize_navigation",
    ): ("_goto_with_auth_checks",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "detect_rate_limit",
    ): (
        "get_sidebar_profiles",
        "_extract_search_page_once",
        "_extract_saved_jobs_page_once",
        "_resolve_conversation_thread_urls",
        "_open_conversation_by_username",
        "get_inbox",
        "get_conversation",
        "search_conversations",
        "send_message",
    ),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "handle_modal_close",
    ): (
        "get_sidebar_profiles",
        "_extract_search_page_once",
        "_extract_saved_jobs_page_once",
        "_resolve_conversation_thread_urls",
        "_open_conversation_by_username",
        "get_inbox",
        "get_conversation",
        "search_conversations",
        "send_message",
    ),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "scroll_to_bottom",
    ): ("_extract_saved_jobs_page_once",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "scroll_job_sidebar",
    ): ("_extract_search_page_once",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "build_issue_diagnostics",
    ): (
        "scrape_person",
        "scrape_company",
        "_extract_search_page",
        "search_jobs",
        "_extract_saved_jobs_page",
        "get_saved_jobs",
    ),
    (
        "tests/test_scraping.py",
        "_patch_to_composer",
        "*",
    ): ("send_message",),
}

_STAGE_MODULES: dict[int, tuple[str, ...]] = {
    1: ("contracts.py", "text.py", "feed_payload.py", "job_policy.py"),
    2: ("search_urls.py",),
    3: ("session.py", "navigation.py"),
    4: ("content.py", "capture.py"),
    5: ("feed.py",),
    6: ("profile_page.py", "person.py"),
    7: ("connection_actions.py",),
    8: ("company.py",),
    9: ("job_pages.py", "jobs.py"),
    10: ("posts.py",),
    11: ("conversations.py",),
    12: ("message_sender.py",),
}

_TOOL_METHODS = {
    name
    for name, (_, stage) in _WORKFLOW_OWNERS.items()
    if 4 <= stage <= 12 and not name.startswith("_")
}
_COMPATIBILITY_METHODS = {"get_page_text", "click_button_by_text"}
_IMPORTED_MODULE_NAMES = {"asyncio", "time"}
_CONTEXTUAL_MODULE_NAMES = {"logger"}


@dataclass(frozen=True, slots=True)
class Seam:
    kind: str
    path: str
    line: int
    target: str
    canonical_owner: str
    migration_stage: int | None


class UnresolvedSeamError(ValueError):
    """A seam could not be assigned to a verified migration owner."""


def _resolved_import_module(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = list(path.parent.relative_to(ROOT).parts)
    keep = len(package) - node.level + 1
    if keep < 0:
        return None
    resolved = package[:keep]
    if node.module:
        resolved.extend(node.module.split("."))
    return ".".join(resolved)


def _is_extractor_module_import(path: Path, node: ast.ImportFrom) -> bool:
    return _resolved_import_module(path, node) == EXTRACTOR_MODULE


def _is_scraping_package_import(path: Path, node: ast.ImportFrom) -> bool:
    return _resolved_import_module(path, node) == "linkedin_mcp_server.scraping"


def _annotation_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value
    return None


def _walk_scope(node: ast.AST):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield child
        yield from _walk_scope(child)


class _LocalBindingCollector(ast.NodeVisitor):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.bindings: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.module_aliases: set[str] = set()
        self.class_aliases: set[str] = set()

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bindings.add(node.id)

    def visit_Import(self, node: ast.Import) -> Any:
        self.bindings.update(
            alias.asname or alias.name.split(".", 1)[0] for alias in node.names
        )
        self.module_aliases.update(
            alias.asname
            for alias in node.names
            if alias.name == EXTRACTOR_MODULE and alias.asname
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        self.bindings.update(alias.asname or alias.name for alias in node.names)
        if self.path is None:
            return
        if _is_scraping_package_import(self.path, node):
            self.module_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "extractor"
            )
        if _is_extractor_module_import(self.path, node) or _is_scraping_package_import(
            self.path, node
        ):
            self.class_aliases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "LinkedInExtractor"
            )

    def visit_Global(self, node: ast.Global) -> Any:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> Any:
        self.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> Any:
        if node.name:
            self.bindings.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> Any:
        if node.name:
            self.bindings.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> Any:
        if node.name:
            self.bindings.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> Any:
        if node.rest:
            self.bindings.add(node.rest)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.bindings.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.bindings.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.bindings.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        return None

    def _visit_comprehension(
        self, generators: list[ast.comprehension], values: list[ast.expr]
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        self._visit_comprehension(node.generators, [node.key, node.value])


def _argument_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
    }
    if arguments.vararg:
        names.add(arguments.vararg.arg)
    if arguments.kwarg:
        names.add(arguments.kwarg.arg)
    return names


def _signature_expressions(arguments: ast.arguments) -> list[ast.expr]:
    """Read the expressions a signature evaluates in its enclosing scope."""

    annotated = [
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        *([arguments.vararg] if arguments.vararg else []),
        *([arguments.kwarg] if arguments.kwarg else []),
    ]
    return [
        *(
            argument.annotation
            for argument in annotated
            if argument.annotation is not None
        ),
        *arguments.defaults,
        *(default for default in arguments.kw_defaults if default is not None),
    ]


def _scope_collector(path: Path, statements: list[ast.stmt]) -> _LocalBindingCollector:
    collector = _LocalBindingCollector(path)
    for statement in statements:
        collector.visit(statement)
    return collector


def _function_shadowed_names(
    path: Path, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> set[str]:
    collector = _scope_collector(path, node.body)
    local_bindings = collector.bindings - collector.globals - collector.nonlocals
    return _argument_names(node.args) | local_bindings


def _pattern_names(pattern: ast.pattern) -> set[str]:
    return {
        name
        for item in ast.walk(pattern)
        for name in (
            item.name
            if isinstance(item, (ast.MatchAs, ast.MatchStar))
            else item.rest
            if isinstance(item, ast.MatchMapping)
            else None,
        )
        if name
    }


def _raised_exception_name(statement: ast.stmt) -> str | None:
    if not isinstance(statement, ast.Raise) or statement.exc is None:
        return None
    if isinstance(statement.exc, ast.Name):
        return statement.exc.id
    if isinstance(statement.exc, ast.Call) and isinstance(statement.exc.func, ast.Name):
        return statement.exc.func.id
    return None


def _matching_handler_index(
    handlers: list[ast.ExceptHandler], exception_name: str
) -> int | None:
    for index, handler in enumerate(handlers):
        if handler.type is None:
            return index
        candidates = (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        )
        if any(
            isinstance(candidate, ast.Name) and candidate.id == exception_name
            for candidate in candidates
        ):
            return index
    return None


def _static_try_outcome(
    node: ast.Try | ast.TryStar,
) -> tuple[str, int | None]:
    if all(isinstance(statement, ast.Pass) for statement in node.body):
        return ("normal", None)
    if len(node.body) == 1:
        exception_name = _raised_exception_name(node.body[0])
        if exception_name is not None:
            handler = _matching_handler_index(node.handlers, exception_name)
            if handler is not None:
                return ("handler", handler)
    return ("unknown", None)


def _extractor_assignment_names(statement: ast.stmt, class_names: set[str]) -> set[str]:
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
        return set()
    value = statement.value
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in class_names
    ):
        return set()
    targets = (
        statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    )
    return {target.id for target in targets if isinstance(target, ast.Name)}


@dataclass(slots=True)
class _ScopeFrame:
    kind: str
    module_names: set[str]
    class_names: set[str]
    instance_names: set[str]
    closure_module_names: set[str]
    closure_class_names: set[str]
    closure_instance_names: set[str]
    ambiguous_names: set[str] = field(default_factory=set)
    # Keyed by the identity of the ``ast.Name`` that uses the alias, not by the
    # name: one local can hold the collaborator over part of a function and
    # something else over the rest, and the scanner reaches those uses in an
    # order of its own.
    collaborator_aliases: dict[int, str] = field(default_factory=dict)
    ambiguous_aliases: set[int] = field(default_factory=set)


def _extractor_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    annotation_aliases: set[str],
    body_aliases: set[str],
) -> set[str]:
    bindings = {
        argument.arg
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if _annotation_name(argument.annotation) in annotation_aliases
    }
    if (
        node.args.vararg
        and _annotation_name(node.args.vararg.annotation) in annotation_aliases
    ):
        bindings.add(node.args.vararg.arg)
    if (
        node.args.kwarg
        and _annotation_name(node.args.kwarg.annotation) in annotation_aliases
    ):
        bindings.add(node.args.kwarg.arg)

    for statement in node.body:
        for item in (statement, *_walk_scope(statement)):
            if not isinstance(item, (ast.Assign, ast.AnnAssign)):
                continue
            value = item.value
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in body_aliases
            ):
                continue
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            bindings.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
    return bindings


def _patch_object_arguments(node: ast.Call) -> tuple[ast.expr | None, ast.expr | None]:
    """Read ``patch.object``'s target and attribute across its call shapes.

    ``mock`` accepts both by keyword, and an arity gate counting positional
    arguments alone answers "not a patch" to the keyword form, to ``*args`` and
    to a call missing its attribute. Returning ``None`` for either end tells the
    caller to refuse rather than to skip.
    """

    if any(keyword.arg is None for keyword in node.keywords):
        return (None, None)
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    target = node.args[0] if node.args else keywords.get("target")
    attribute = node.args[1] if len(node.args) > 1 else keywords.get("attribute")
    return (target, attribute)


def _setattr_arguments(node: ast.Call) -> tuple[ast.expr | None, ast.expr | None]:
    """Read ``setattr``'s target and member name across its call shapes.

    ``monkeypatch.setattr`` names ``target``, ``name`` and ``value``, so a gate
    counting positional arguments answers "not a patch" to the keyword form,
    exactly the defect ``_patch_object_arguments`` exists to close. Returning
    ``None`` for the target tells the caller to refuse rather than to skip; a
    missing name is left to the caller because the two-argument
    ``setattr("dotted.path", value)`` form carries its member inside the
    target and never has one. A star after a readable first argument can hide
    the member and value, but it cannot hide which object receives the patch.
    """

    if any(keyword.arg is None for keyword in node.keywords):
        return (None, None)
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    first = node.args[0] if node.args else None
    target = first if first is not None and not isinstance(first, ast.Starred) else None
    if not node.args:
        target = keywords.get("target")
    second = node.args[1] if len(node.args) > 1 else None
    name = (
        second if second is not None and not isinstance(second, ast.Starred) else None
    )
    if len(node.args) < 2:
        name = keywords.get("name")
    return (target, name)


# The fixture's own name, and the class a private context is opened from. A
# binding reading from either holds the same object, which is the authority the
# use-site collector follows.
_MONKEYPATCH_NAMES = frozenset({"monkeypatch", "MonkeyPatch"})


_MonkeyPatchState = tuple[set[str], set[str]]


@dataclass(slots=True)
class _FlowResult:
    normal: list[_MonkeyPatchState] = field(default_factory=list)
    breaks: list[_MonkeyPatchState] = field(default_factory=list)
    continues: list[_MonkeyPatchState] = field(default_factory=list)
    returns: list[_MonkeyPatchState] = field(default_factory=list)
    exceptional: list[_MonkeyPatchState] = field(default_factory=list)

    def extend(self, other: _FlowResult) -> None:
        self.normal.extend(other.normal)
        self.breaks.extend(other.breaks)
        self.continues.extend(other.continues)
        self.returns.extend(other.returns)
        self.exceptional.extend(other.exceptional)


@dataclass(slots=True)
class _ClosureCell:
    aliases: set[str]


@dataclass(slots=True)
class _DeferredFunction:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    cell: _ClosureCell


def _mentions_monkeypatch(expression: ast.expr) -> bool:
    return any(
        (isinstance(item, ast.Name) and item.id in _MONKEYPATCH_NAMES)
        or (isinstance(item, ast.Attribute) and item.attr in _MONKEYPATCH_NAMES)
        for item in ast.walk(expression)
    )


class _MonkeyPatchCallCollector(ast.NodeVisitor):
    """Locate unreadable ``setattr`` calls on a live pytest patcher binding.

    Receiver authority follows lexical scopes and reachable control-flow paths.
    Branches start from the same incoming state and merge by union, so one live
    path retains fail-closed authority while rebinding on every path retires it.
    The closure state is merged alongside the current scope without turning a
    class local into a method closure. Calls with readable targets do not need
    this evidence: extractor reachability scopes those independently.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: set[int] = set()
        self.aliases = {"monkeypatch"}
        self.closure_aliases = set(self.aliases)
        self.scope_kinds = ["module"]
        self.cells = [_ClosureCell(set(self.closure_aliases))]
        self.deferred: list[_DeferredFunction] = []
        self.deferred_ids: set[int] = set()

    @staticmethod
    def _key(expression: ast.expr) -> str:
        if isinstance(expression, (ast.Name, ast.Attribute)):
            return ast.unparse(expression)
        return ""

    @staticmethod
    def _targets(target: ast.expr) -> set[str]:
        if isinstance(target, (ast.Tuple, ast.List)):
            return {
                name
                for item in target.elts
                if (name := _MonkeyPatchCallCollector._key(item))
            }
        name = _MonkeyPatchCallCollector._key(target)
        return {name} if name else set()

    def _is_authority(self, expression: ast.expr) -> bool:
        return any(
            (isinstance(item, ast.expr) and self._key(item) in self.aliases)
            or (isinstance(item, ast.Name) and item.id == "MonkeyPatch")
            or (isinstance(item, ast.Attribute) and item.attr == "MonkeyPatch")
            for item in ast.walk(expression)
        )

    def _bind(self, target: ast.expr, authority: bool) -> None:
        names = self._targets(target)
        self.aliases.difference_update(names)
        if authority and isinstance(target, (ast.Name, ast.Attribute)):
            self.aliases.update(names)
        if self.scope_kinds[-1] != "class":
            self.closure_aliases.difference_update(names)
            self.closure_aliases.update(self.aliases & names)

    def _unbind(self, names: set[str]) -> None:
        self.aliases.difference_update(names)
        if self.scope_kinds[-1] != "class":
            self.closure_aliases.difference_update(names)

    def _state(self) -> tuple[set[str], set[str]]:
        return (set(self.aliases), set(self.closure_aliases))

    def _restore(self, state: tuple[set[str], set[str]]) -> None:
        self.aliases = set(state[0])
        self.closure_aliases = set(state[1])

    @staticmethod
    def _merge(
        states: list[tuple[set[str], set[str]]],
    ) -> tuple[set[str], set[str]]:
        return (
            set().union(*(state[0] for state in states)),
            set().union(*(state[1] for state in states)),
        )

    def _visit_suite(self, statements: list[ast.stmt]) -> _FlowResult:
        result = _FlowResult()
        active = True
        for statement in statements:
            if not active:
                break
            prefix = self._state()
            flowed = self.visit(statement)
            result.exceptional.append(prefix)
            if not isinstance(flowed, _FlowResult):
                continue
            result.breaks.extend(flowed.breaks)
            result.continues.extend(flowed.continues)
            result.returns.extend(flowed.returns)
            result.exceptional.extend(flowed.exceptional)
            if flowed.normal:
                self._restore(self._merge(flowed.normal))
            else:
                active = False
        if active:
            result.normal.append(self._state())
        return result

    def _visit_branches(
        self,
        initial: _MonkeyPatchState,
        suites: list[list[ast.stmt]],
        *,
        include_initial: bool = False,
    ) -> _FlowResult:
        result = _FlowResult(normal=[initial] if include_initial else [])
        for suite in suites:
            self._restore(initial)
            result.extend(self._visit_suite(suite))
        if result.normal:
            self._restore(self._merge(result.normal))
        return result

    def _visit_function_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for expression in _signature_expressions(node.args):
            self.visit(expression)
        if node.returns is not None:
            self.visit(node.returns)

    def _defer_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_function_expressions(node)
        if id(node) not in self.deferred_ids:
            self.deferred_ids.add(id(node))
            self.deferred.append(_DeferredFunction(node, self.cells[-1]))
        self._unbind({node.name})

    def _evaluate_function(self, item: _DeferredFunction) -> None:
        node = item.node
        collector = _scope_collector(self.path, node.body)
        parameters = _argument_names(node.args)
        local_bindings = collector.bindings - collector.globals - collector.nonlocals
        inherited = item.cell.aliases - local_bindings - parameters
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *([node.args.vararg] if node.args.vararg else []),
            *([node.args.kwarg] if node.args.kwarg else []),
        ]:
            if argument.arg == "monkeypatch" or (
                argument.annotation is not None
                and _mentions_monkeypatch(argument.annotation)
            ):
                inherited.add(argument.arg)

        previous = self._state()
        self.aliases = set(inherited)
        self.closure_aliases = set(inherited)
        cell = _ClosureCell(set(inherited))
        self.cells.append(cell)
        self.scope_kinds.append("function")
        flow = self._visit_suite(node.body)
        endings = [*flow.normal, *flow.breaks, *flow.continues, *flow.returns]
        if endings:
            cell.aliases = set(self._merge(endings)[1])
        self.scope_kinds.pop()
        self.cells.pop()
        self._restore(previous)
        self._drain_deferred(cell)

    def _drain_deferred(self, cell: _ClosureCell) -> None:
        while True:
            pending = [item for item in self.deferred if item.cell is cell]
            if not pending:
                return
            self.deferred = [item for item in self.deferred if item.cell is not cell]
            for item in pending:
                self._evaluate_function(item)

    def visit_Module(self, node: ast.Module) -> Any:
        flow = self._visit_suite(node.body)
        endings = list(flow.normal)
        if endings:
            self.cells[-1].aliases = set(self._merge(endings)[1])
        self._drain_deferred(self.cells[-1])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._defer_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._defer_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        previous = (self.aliases, self.closure_aliases)
        self.aliases = set(self.closure_aliases)
        self.scope_kinds.append("class")
        self._visit_suite(node.body)
        self.scope_kinds.pop()
        self.aliases, self.closure_aliases = previous
        self._unbind({node.name})

    def visit_Assign(self, node: ast.Assign) -> Any:
        self.visit(node.value)
        authority = self._is_authority(node.value)
        for target in node.targets:
            self._bind(target, authority)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        if node.value is not None:
            self.visit(node.value)
        self._bind(
            node.target,
            _mentions_monkeypatch(node.annotation)
            or (node.value is not None and self._is_authority(node.value)),
        )

    def visit_AugAssign(self, node: ast.AugAssign) -> Any:
        self.visit(node.value)
        self._bind(node.target, False)

    def visit_Delete(self, node: ast.Delete) -> Any:
        for target in node.targets:
            self._bind(target, False)

    def visit_Import(self, node: ast.Import) -> Any:
        self._unbind(
            {alias.asname or alias.name.split(".", 1)[0] for alias in node.names}
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        self._unbind({alias.asname or alias.name for alias in node.names})

    def visit_If(self, node: ast.If) -> Any:
        self.visit(node.test)
        initial = self._state()
        return self._visit_branches(
            initial,
            [node.body, node.orelse],
            include_initial=not node.orelse,
        )

    def _visit_try(self, node: ast.Try | ast.TryStar) -> _FlowResult:
        initial = self._state()
        self._restore(initial)
        body = self._visit_suite(node.body)
        result = _FlowResult(
            breaks=list(body.breaks),
            continues=list(body.continues),
            returns=list(body.returns),
            exceptional=list(body.exceptional),
        )

        if body.normal:
            self._restore(self._merge(body.normal))
            result.extend(self._visit_suite(node.orelse))

        handler_entry = self._merge(body.exceptional)
        for handler in node.handlers:
            self._restore(handler_entry)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self._bind(ast.Name(id=handler.name, ctx=ast.Store()), False)
            handled = self._visit_suite(handler.body)
            for states in (
                handled.normal,
                handled.breaks,
                handled.continues,
                handled.returns,
                handled.exceptional,
            ):
                for index, state in enumerate(states):
                    self._restore(state)
                    if handler.name is not None:
                        self._unbind({handler.name})
                    states[index] = self._state()
            result.extend(handled)

        if not node.finalbody:
            if result.normal:
                self._restore(self._merge(result.normal))
            return result

        finished = _FlowResult()
        for states, continuation in (
            (result.normal, "normal"),
            (result.breaks, "break"),
            (result.continues, "continue"),
            (result.returns, "return"),
            (result.exceptional, "exceptional"),
        ):
            if not states:
                continue
            self._restore(self._merge(states))
            final = self._visit_suite(node.finalbody)
            finished.breaks.extend(final.breaks)
            finished.continues.extend(final.continues)
            finished.returns.extend(final.returns)
            finished.exceptional.extend(final.exceptional)
            if continuation == "normal":
                finished.normal.extend(final.normal)
            elif continuation == "break":
                finished.breaks.extend(final.normal)
            elif continuation == "continue":
                finished.continues.extend(final.normal)
            elif continuation == "return":
                finished.returns.extend(final.normal)
            else:
                finished.exceptional.extend(final.normal)
        if finished.normal:
            self._restore(self._merge(finished.normal))
        return finished

    def visit_Try(self, node: ast.Try) -> Any:
        return self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> Any:
        return self._visit_try(node)

    def _visit_loop(
        self,
        node: ast.For | ast.AsyncFor | ast.While,
        initial: _MonkeyPatchState,
    ) -> _FlowResult:
        head = initial
        body = _FlowResult()
        while True:
            self._restore(head)
            if isinstance(node, ast.While):
                self.visit(node.test)
            else:
                self._bind(node.target, False)
            body = self._visit_suite(node.body)
            widened = self._merge([initial, *body.normal, *body.continues])
            if widened == head:
                break
            head = widened

        self._restore(head)
        if isinstance(node, ast.While):
            self.visit(node.test)
        no_break = self._visit_suite(node.orelse)
        normal = [*no_break.normal, *body.breaks]
        if normal:
            self._restore(self._merge(normal))
        return _FlowResult(
            normal=normal,
            breaks=no_break.breaks,
            continues=no_break.continues,
            returns=[*body.returns, *no_break.returns],
            exceptional=[*body.exceptional, *no_break.exceptional],
        )

    def visit_Break(self, node: ast.Break) -> Any:
        return _FlowResult(breaks=[self._state()])

    def visit_Continue(self, node: ast.Continue) -> Any:
        return _FlowResult(continues=[self._state()])

    def visit_Raise(self, node: ast.Raise) -> Any:
        if node.exc is not None:
            self.visit(node.exc)
        if node.cause is not None:
            self.visit(node.cause)
        return _FlowResult(exceptional=[self._state()])

    def visit_Return(self, node: ast.Return) -> Any:
        if node.value is not None:
            self.visit(node.value)
        return _FlowResult(returns=[self._state()])

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> _FlowResult:
        self.visit(node.iter)
        return self._visit_loop(node, self._state())

    def visit_For(self, node: ast.For) -> Any:
        return self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> Any:
        return self._visit_for(node)

    def visit_While(self, node: ast.While) -> Any:
        return self._visit_loop(node, self._state())

    @staticmethod
    def _match_names(pattern: ast.pattern) -> set[str]:
        names: set[str] = set()
        for item in ast.walk(pattern):
            if isinstance(item, ast.MatchAs) and item.name is not None:
                names.add(item.name)
            elif isinstance(item, ast.MatchStar) and item.name is not None:
                names.add(item.name)
            elif isinstance(item, ast.MatchMapping) and item.rest is not None:
                names.add(item.rest)
        return names

    @classmethod
    def _is_irrefutable(cls, pattern: ast.pattern) -> bool:
        if isinstance(pattern, ast.MatchAs):
            return pattern.pattern is None or cls._is_irrefutable(pattern.pattern)
        if isinstance(pattern, ast.MatchOr):
            return any(cls._is_irrefutable(item) for item in pattern.patterns)
        return False

    @classmethod
    def _is_catch_all(cls, case: ast.match_case) -> bool:
        return case.guard is None and cls._is_irrefutable(case.pattern)

    def visit_Match(self, node: ast.Match) -> Any:
        self.visit(node.subject)
        initial = self._state()
        authority = self._is_authority(node.subject)
        result = _FlowResult()
        for case in node.cases:
            self._restore(initial)
            for name in self._match_names(case.pattern):
                self._bind(ast.Name(id=name, ctx=ast.Store()), authority)
            if case.guard is not None:
                self.visit(case.guard)
            result.extend(self._visit_suite(case.body))
        if not node.cases or not self._is_catch_all(node.cases[-1]):
            result.normal.append(initial)
        if result.normal:
            self._restore(self._merge(result.normal))
        return result

    def visit_NamedExpr(self, node: ast.NamedExpr) -> Any:
        self.visit(node.value)
        self._bind(node.target, self._is_authority(node.value))

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> _FlowResult:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, self._is_authority(item.context_expr))
        return self._visit_suite(node.body)

    def visit_With(self, node: ast.With) -> Any:
        return self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> Any:
        return self._visit_with(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
            and self._is_authority(node.func.value)
        ):
            self.calls.add(id(node))
        self.generic_visit(node)


def _monkeypatch_calls(path: Path, tree: ast.Module) -> set[int]:
    collector = _MonkeyPatchCallCollector(path)
    collector.visit(tree)
    return collector.calls


class _CollaboratorAliasCollector:
    """Answer each use of a local name bound to a facade collaborator.

    ``cap = extractor._capture`` followed by ``patch.object(cap, ...)`` is the
    same reach-through written over two statements, and without the alias the
    patch lands on a bare local the reader cannot place. Read in source order
    with the branch merging the class suites already use, because a map
    collected over the whole body at once keeps answering ``_capture`` for a
    ``cap`` that a later statement rebound. That error runs the other way from
    the fail-open holes: it reports a foreign object as a collaborator seam, or
    refuses it as an unknown one, and a false failure here blocks every later
    stage. Where a rebinding depends on a branch no single answer fits, so the
    name becomes ambiguous rather than keeping the stale one.
    """

    def __init__(self, path: Path, facade_names: set[str]) -> None:
        self.path = path
        self.facade_names = facade_names
        self.aliases: dict[str, str] = {}
        self.ambiguous: set[str] = set()
        self.resolved: dict[int, str] = {}
        self.unresolved: set[int] = set()

    def _state(self) -> tuple[dict[str, str], set[str]]:
        return (dict(self.aliases), set(self.ambiguous))

    def _restore(self, state: tuple[dict[str, str], set[str]]) -> None:
        self.aliases = dict(state[0])
        self.ambiguous = set(state[1])

    def _merge(self, states: list[tuple[dict[str, str], set[str]]]) -> None:
        names = set().union(*(set(state[0]) | state[1] for state in states))
        self.aliases = {}
        self.ambiguous = set()
        for name in names:
            answers = {state[0].get(name) for state in states}
            answer = next(iter(answers))
            if (
                len(answers) == 1
                and answer is not None
                and not any(name in state[1] for state in states)
            ):
                self.aliases[name] = answer
            else:
                self.ambiguous.add(name)

    def _forget(self, names: set[str]) -> None:
        for name in names:
            self.aliases.pop(name, None)
            self.ambiguous.discard(name)

    def _bind_target(self, target: ast.expr) -> None:
        self._forget(
            {
                item.id
                for item in ast.walk(target)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
            }
        )

    def _collaborator_attribute(self, value: ast.expr | None) -> str | None:
        # A walrus names the same object it evaluates to, so the binding it
        # makes on the way past does not change what the outer name receives.
        while isinstance(value, ast.NamedExpr):
            value = value.value
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id in self.facade_names
            and value.attr in _FACADE_COLLABORATORS
        ):
            return value.attr
        return None

    def _assign(self, name: str, value: ast.expr | None) -> None:
        attribute = self._collaborator_attribute(value)
        if attribute is None:
            self._forget({name})
            return
        self.ambiguous.discard(name)
        self.aliases[name] = attribute

    @staticmethod
    def _nested_scope_names(node: ast.AST) -> set[str]:
        """Name what an expression binds in a scope of its own."""

        names: set[str] = set()
        for item in ast.walk(node):
            if isinstance(item, ast.Lambda):
                names.update(_argument_names(item.args))
            elif isinstance(
                item, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
            ):
                for generator in item.generators:
                    names.update(
                        target.id
                        for target in ast.walk(generator.target)
                        if isinstance(target, ast.Name)
                    )
        return names

    def _record(self, node: ast.AST | None) -> None:
        """Answer every name read inside one expression from the state so far.

        The last answer wins, so a second reading of a loop body overwrites the
        first rather than leaving both on the record.
        """

        if node is None:
            return
        shadowed = self._nested_scope_names(node)
        for item in ast.walk(node):
            if not (isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)):
                continue
            alias = None if item.id in shadowed else self.aliases.get(item.id)
            if alias is not None:
                self.resolved[id(item)] = alias
                self.unresolved.discard(id(item))
                continue
            self.resolved.pop(id(item), None)
            if item.id in self.ambiguous and item.id not in shadowed:
                self.unresolved.add(id(item))
            else:
                self.unresolved.discard(id(item))
        for item in ast.walk(node):
            if isinstance(item, ast.NamedExpr) and isinstance(item.target, ast.Name):
                self._assign(item.target.id, item.value)

    def _bind(self, statement: ast.stmt) -> None:
        collector = _LocalBindingCollector(self.path)
        collector.visit(statement)
        # A walrus binds while the statement's own expressions are read, so
        # `_record` has already answered it; forgetting it again here would
        # retire the alias it just made.
        walrus = {
            item.target.id
            for item in ast.walk(statement)
            if isinstance(item, ast.NamedExpr) and isinstance(item.target, ast.Name)
        }
        self._forget(
            (collector.bindings | collector.globals | collector.nonlocals) - walrus
        )
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            return
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        for target in targets:
            if isinstance(target, ast.Name):
                self._assign(target.id, statement.value)

    def visit_body(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self.visit(statement)

    def visit(self, statement: ast.stmt) -> None:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._visit_function(statement)
        elif isinstance(statement, ast.ClassDef):
            self._visit_class(statement)
        elif isinstance(statement, ast.If):
            self._visit_if(statement)
        elif isinstance(statement, (ast.Try, ast.TryStar)):
            self._visit_try(statement)
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            self._visit_loop(statement)
        elif isinstance(statement, ast.Match):
            self._visit_match(statement)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            self._visit_with(statement)
        else:
            self._record(statement)
            self._bind(statement)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in (
            *node.decorator_list,
            *_signature_expressions(node.args),
            node.returns,
        ):
            self._record(expression)
        # A closure is read against the bindings its definition saw, the same
        # snapshot the scanner's own frames take.
        state = self._state()
        self._forget(_function_shadowed_names(self.path, node))
        self.visit_body(node.body)
        self._restore(state)
        self._forget({node.name})

    def _visit_class(self, node: ast.ClassDef) -> None:
        for expression in (
            *node.decorator_list,
            *node.bases,
            *(keyword.value for keyword in node.keywords),
        ):
            self._record(expression)
        state = self._state()
        self.visit_body(node.body)
        self._restore(state)
        self._forget({node.name})

    def _visit_if(self, node: ast.If) -> None:
        self._record(node.test)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            self.visit_body(node.body if node.test.value else node.orelse)
            return
        before = self._state()
        states: list[tuple[dict[str, str], set[str]]] = []
        for statements in (node.body, node.orelse):
            self._restore(before)
            self.visit_body(statements)
            states.append(self._state())
        self._merge(states)

    def _visit_handler(self, handler: ast.ExceptHandler) -> None:
        self._record(handler.type)
        if handler.name:
            self._forget({handler.name})
        self.visit_body(handler.body)
        if handler.name:
            self._forget({handler.name})

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        outcome, handler_index = _static_try_outcome(node)
        before = self._state()
        if outcome == "normal":
            self.visit_body(node.body)
            self.visit_body(node.orelse)
        elif outcome == "handler" and handler_index is not None:
            self.visit_body(node.body)
            self._visit_handler(node.handlers[handler_index])
        else:
            states: list[tuple[dict[str, str], set[str]]] = []
            self.visit_body(node.body)
            self.visit_body(node.orelse)
            states.append(self._state())
            for handler in node.handlers:
                self._restore(before)
                self._visit_handler(handler)
                states.append(self._state())
            self._merge(states)
        self.visit_body(node.finalbody)

    def _visit_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> None:
        # The body may run zero times or many, so its second reading starts
        # from the entry state merged with whatever one iteration leaves. A
        # single replay reaches the fixed point: a name the two readings
        # disagree on is already ambiguous, and ambiguity is the top here.
        if isinstance(node, ast.While):
            self._record(node.test)
        else:
            self._record(node.iter)
            self._bind_target(node.target)
        entry = self._state()
        self.visit_body(node.body)
        self._merge([self._state(), entry])
        replayed = self._state()
        self.visit_body(node.body)
        self._merge([self._state(), replayed])
        self.visit_body(node.orelse)

    def _visit_match(self, node: ast.Match) -> None:
        self._record(node.subject)
        before = self._state()
        # No case has to match, so falling through is one of the outcomes.
        states = [before]
        for case in node.cases:
            self._restore(before)
            self._forget(_pattern_names(case.pattern))
            self._record(case.guard)
            self.visit_body(case.body)
            states.append(self._state())
        self._merge(states)

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self._record(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        self.visit_body(node.body)


def _collaborator_aliases(
    path: Path,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    facade_names: set[str],
) -> tuple[dict[int, str], set[int]]:
    """Resolve every use of a local name bound to a facade collaborator."""

    collector = _CollaboratorAliasCollector(path, facade_names)
    collector.visit_body(node.body)
    return (collector.resolved, collector.unresolved)


class _CalledMethodCollector(ast.NodeVisitor):
    def __init__(self, path: Path, bindings: set[str], class_names: set[str]) -> None:
        self.path = path
        self.bindings = [set(bindings)]
        self.closure_bindings = [set(bindings)]
        self.class_names = [set(class_names)]
        self.closure_class_names = [set(class_names)]
        self.ambiguous_names = [set[str]()]
        self.closure_ambiguous_names = [set[str]()]
        self.methods: set[str] = set()
        self.errors: list[tuple[ast.Call, str]] = []
        self.class_scope_depths: set[int] = set()

    def visit_Call(self, node: ast.Call) -> Any:
        called = node.func
        receiver: str | None = None
        method: str | None = None
        if isinstance(called, ast.Attribute) and isinstance(called.value, ast.Name):
            receiver = called.value.id
            method = called.attr
        elif (
            isinstance(called, ast.Call)
            and isinstance(called.func, ast.Name)
            and called.func.id == "getattr"
            and len(called.args) >= 2
            and isinstance(called.args[0], ast.Name)
            and isinstance(called.args[1], ast.Constant)
            and isinstance(called.args[1].value, str)
        ):
            receiver = called.args[0].id
            method = called.args[1].value
        if receiver in self.ambiguous_names[-1]:
            self.errors.append((node, receiver))
        elif receiver in self.bindings[-1] and method is not None:
            self.methods.add(method)
        self.generic_visit(node)

    @staticmethod
    def _visit_arguments(
        visitor: _CalledMethodCollector, arguments: ast.arguments
    ) -> None:
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            if argument.annotation is not None:
                visitor.visit(argument.annotation)
        if arguments.vararg and arguments.vararg.annotation is not None:
            visitor.visit(arguments.vararg.annotation)
        if arguments.kwarg and arguments.kwarg.annotation is not None:
            visitor.visit(arguments.kwarg.annotation)
        for default in arguments.defaults:
            visitor.visit(default)
        for default in arguments.kw_defaults:
            if default is not None:
                visitor.visit(default)

    def _visit_function_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_arguments(self, node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        collector = _LocalBindingCollector(self.path)
        for statement in node.body:
            collector.visit(statement)
        local_bindings = (
            collector.bindings - collector.globals - collector.nonlocals
        ) | _argument_names(node.args)
        bindings = self.closure_bindings[-1] - local_bindings - collector.globals
        ambiguous = self.closure_ambiguous_names[-1] - local_bindings
        class_names = self.closure_class_names[-1] - local_bindings
        class_names.update(collector.class_aliases)
        self.bindings.append(bindings)
        self.closure_bindings.append(set(bindings))
        self.class_names.append(class_names)
        self.closure_class_names.append(set(class_names))
        self.ambiguous_names.append(ambiguous)
        self.closure_ambiguous_names.append(set(ambiguous))
        for statement in node.body:
            self.visit(statement)
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
        self.closure_class_names.pop()
        self.class_names.pop()
        self.closure_bindings.pop()
        self.bindings.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function_definition(node)

    def _apply_class_binding(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.AnnAssign) and statement.value is None:
            return
        collector = _LocalBindingCollector(self.path)
        if isinstance(statement, ast.Match):
            targets = set().union(
                *(_pattern_names(case.pattern) for case in statement.cases)
            )
            self.bindings[-1].difference_update(targets)
            self.class_names[-1].difference_update(targets)
            self.ambiguous_names[-1].difference_update(targets)
            return
        if isinstance(
            statement,
            (
                ast.Assign,
                ast.AnnAssign,
                ast.AugAssign,
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        ):
            extractor_instances = _extractor_assignment_names(
                statement, self.class_names[-1]
            )
            collector.visit(statement)
            self.bindings[-1].difference_update(collector.bindings)
            self.class_names[-1].difference_update(collector.bindings)
            self.ambiguous_names[-1].difference_update(collector.bindings)
            self.bindings[-1].update(extractor_instances)
            self.class_names[-1].update(collector.class_aliases)

    def _visit_class_suite(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self.visit(statement)
            self._apply_class_binding(statement)

    def _called_state(self) -> tuple[set[str], set[str], set[str]]:
        return (
            set(self.bindings[-1]),
            set(self.class_names[-1]),
            set(self.ambiguous_names[-1]),
        )

    def _restore_called_state(self, state: tuple[set[str], set[str], set[str]]) -> None:
        self.bindings[-1] = set(state[0])
        self.class_names[-1] = set(state[1])
        self.ambiguous_names[-1] = set(state[2])

    def _merge_called_states(
        self, states: list[tuple[set[str], set[str], set[str]]]
    ) -> None:
        bindings = set.intersection(*(state[0] for state in states))
        class_names = set.intersection(*(state[1] for state in states))
        ambiguous = set().union(*(state[2] for state in states))
        candidates = set().union(*(state[0] | state[1] for state in states))
        ambiguous.update(
            name
            for name in candidates
            if len({(name in state[0], name in state[1]) for state in states}) > 1
        )
        self.bindings[-1] = bindings
        self.class_names[-1] = class_names
        self.ambiguous_names[-1] = ambiguous

    def visit_If(self, node: ast.If) -> Any:
        if len(self.bindings) not in self.class_scope_depths:
            self.generic_visit(node)
            return
        self.visit(node.test)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            self._visit_class_suite(node.body if node.test.value else node.orelse)
            return
        before = self._called_state()
        states: list[tuple[set[str], set[str], set[str]]] = []
        for statements in (node.body, node.orelse):
            self._restore_called_state(before)
            self._visit_class_suite(statements)
            states.append(self._called_state())
        self._merge_called_states(states)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        if len(self.bindings) not in self.class_scope_depths:
            self.generic_visit(node)
            return
        outcome, handler_index = _static_try_outcome(node)
        before = self._called_state()
        if outcome == "normal":
            self._visit_class_suite(node.body)
            self._visit_class_suite(node.orelse)
        elif outcome == "handler" and handler_index is not None:
            self._visit_class_suite(node.body)
            self.visit(node.handlers[handler_index])
        else:
            states: list[tuple[set[str], set[str], set[str]]] = []
            self._restore_called_state(before)
            self._visit_class_suite(node.body)
            self._visit_class_suite(node.orelse)
            states.append(self._called_state())
            for handler in node.handlers:
                self._restore_called_state(before)
                self.visit(handler)
                states.append(self._called_state())
            self._merge_called_states(states)
        self._visit_class_suite(node.finalbody)

    def visit_Try(self, node: ast.Try) -> Any:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> Any:
        self._visit_try(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> Any:
        if len(self.bindings) not in self.class_scope_depths or not node.name:
            self.generic_visit(node)
            return
        if node.type is not None:
            self.visit(node.type)
        self.bindings[-1].discard(node.name)
        self.class_names[-1].discard(node.name)
        self.ambiguous_names[-1].discard(node.name)
        self._visit_class_suite(node.body)
        self.bindings[-1].discard(node.name)
        self.class_names[-1].discard(node.name)
        self.ambiguous_names[-1].discard(node.name)
        if node.name in self.closure_bindings[-1]:
            self.bindings[-1].add(node.name)
        if node.name in self.closure_class_names[-1]:
            self.class_names[-1].add(node.name)
        if node.name in self.closure_ambiguous_names[-1]:
            self.ambiguous_names[-1].add(node.name)

    def visit_match_case(self, node: ast.match_case) -> Any:
        self.visit(node.pattern)
        if len(self.bindings) not in self.class_scope_depths:
            if node.guard is not None:
                self.visit(node.guard)
            for statement in node.body:
                self.visit(statement)
            return
        targets = _pattern_names(node.pattern)
        previous_bindings = set(self.bindings[-1])
        previous_class_names = set(self.class_names[-1])
        previous_ambiguous = set(self.ambiguous_names[-1])
        self.bindings[-1].difference_update(targets)
        self.class_names[-1].difference_update(targets)
        self.ambiguous_names[-1].difference_update(targets)
        if node.guard is not None:
            self.visit(node.guard)
        self._visit_class_suite(node.body)
        self.bindings[-1] = previous_bindings
        self.class_names[-1] = previous_class_names
        self.ambiguous_names[-1] = previous_ambiguous

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        closure = set(self.closure_bindings[-1])
        closure_class_names = set(self.closure_class_names[-1])
        closure_ambiguous = set(self.closure_ambiguous_names[-1])
        self.bindings.append(set(closure))
        self.closure_bindings.append(closure)
        self.class_names.append(set(closure_class_names))
        self.closure_class_names.append(closure_class_names)
        self.ambiguous_names.append(set(closure_ambiguous))
        self.closure_ambiguous_names.append(closure_ambiguous)
        self.class_scope_depths.add(len(self.bindings))
        self._visit_class_suite(node.body)
        self.class_scope_depths.remove(len(self.bindings))
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
        self.closure_class_names.pop()
        self.class_names.pop()
        self.closure_bindings.pop()
        self.bindings.pop()

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        self._visit_arguments(self, node.args)
        collector = _LocalBindingCollector()
        collector.visit(node.body)
        bindings = (
            self.closure_bindings[-1] - collector.bindings - _argument_names(node.args)
        )
        local_bindings = collector.bindings | _argument_names(node.args)
        ambiguous = self.closure_ambiguous_names[-1] - local_bindings
        class_names = self.closure_class_names[-1] - local_bindings
        self.bindings.append(bindings)
        self.closure_bindings.append(set(bindings))
        self.class_names.append(class_names)
        self.closure_class_names.append(set(class_names))
        self.ambiguous_names.append(ambiguous)
        self.closure_ambiguous_names.append(set(ambiguous))
        self.visit(node.body)
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
        self.closure_class_names.pop()
        self.class_names.pop()
        self.closure_bindings.pop()
        self.bindings.pop()

    @staticmethod
    def _target_names(target: ast.expr) -> set[str]:
        return {
            item.id
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        }

    def _visit_comprehension(
        self, generators: list[ast.comprehension], values: list[ast.expr]
    ) -> None:
        first, *remaining = generators
        self.visit(first.iter)
        bindings = set(self.closure_bindings[-1])
        class_names = set(self.closure_class_names[-1])
        ambiguous = set(self.closure_ambiguous_names[-1])
        self.bindings.append(bindings)
        self.closure_bindings.append(bindings)
        self.class_names.append(class_names)
        self.closure_class_names.append(class_names)
        self.ambiguous_names.append(ambiguous)
        self.closure_ambiguous_names.append(ambiguous)
        for generator in (first, *remaining):
            if generator is not first:
                self.visit(generator.iter)
            self.visit(generator.target)
            targets = self._target_names(generator.target)
            self.bindings[-1].difference_update(targets)
            self.class_names[-1].difference_update(targets)
            self.ambiguous_names[-1].difference_update(targets)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
        self.closure_class_names.pop()
        self.class_names.pop()
        self.closure_bindings.pop()
        self.bindings.pop()

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        self._visit_comprehension(node.generators, [node.key, node.value])


def module_aliases(path: Path, tree: ast.AST) -> set[str]:
    """Collect every local name bound to the extractor module in one file."""

    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if _is_scraping_package_import(path, node):
                for alias in node.names:
                    if alias.name == "extractor":
                        aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == EXTRACTOR_MODULE:
                    if not alias.asname:
                        raise UnresolvedSeamError(
                            f"line {node.lineno}: extractor module import requires an as alias"
                        )
                    aliases.add(alias.asname)
    return aliases


def extractor_methods(package: Path = PACKAGE) -> tuple[frozenset[str], frozenset[str]]:
    """Read public and private method names of ``LinkedInExtractor``."""

    tree = ast.parse(
        (package / "scraping" / "extractor.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor":
            methods = frozenset(
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            return (
                frozenset(name for name in methods if not name.startswith("_")),
                frozenset(name for name in methods if name.startswith("_")),
            )
    raise AssertionError("LinkedInExtractor not found in scraping/extractor.py")


# One patched attribute per site re-reads the owning module, so the answer is
# kept. Keyed by package too, because a caller may point the reader at a tree
# other than this one.
_COLLABORATOR_METHODS: dict[tuple[Path, str], frozenset[str]] = {}


def collaborator_methods(owner: str, package: Path = PACKAGE) -> frozenset[str]:
    """Read the method names of a facade collaborator named ``module.Class``.

    Only the class body is read, so a member reached through a base class or
    produced by a decorator is not here and its patch is refused. That is the
    acceptable direction: a loud refusal names the site and asks for the table
    to be widened, while accepting an unknown name would let a patch that
    intercepts nothing through.
    """

    cached = _COLLABORATOR_METHODS.get((package, owner))
    if cached is not None:
        return cached
    module, class_name = owner.split(".", 1)
    tree = ast.parse(
        (package / "scraping" / f"{module}.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = frozenset(
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            _COLLABORATOR_METHODS[package, owner] = methods
            return methods
    raise UnresolvedSeamError(
        f"{class_name} not found in scraping/{module}.py: "
        "rename the entry in _FACADE_COLLABORATORS"
    )


class Scanner(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        module_names: set[str],
        class_names: set[str],
        facade_publics: frozenset[str],
        facade_privates: frozenset[str],
        monkeypatch_calls: set[int],
    ):
        self.path = path
        self.root_module_names = module_names
        self.root_class_names = class_names
        self.facade_publics = facade_publics
        self.facade_privates = facade_privates
        self.monkeypatch_calls = monkeypatch_calls
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.frames = [
            _ScopeFrame(
                kind="module",
                module_names=set(module_names),
                class_names=set(class_names),
                instance_names=set(),
                closure_module_names=set(module_names),
                closure_class_names=set(class_names),
                closure_instance_names=set(),
            )
        ]
        self.caller_contexts: list[set[str]] = []
        self.suppressed_module_attributes: set[int] = set()
        self.seams: list[Seam] = []
        self.errors: list[str] = []

    @property
    def module_names(self) -> set[str]:
        return self.frames[-1].module_names

    @property
    def class_names(self) -> set[str]:
        return self.frames[-1].class_names

    @property
    def relative_path(self) -> str:
        return self.path.relative_to(ROOT).as_posix()

    def _add(
        self,
        kind: str,
        node: ast.expr | ast.stmt,
        target: str,
        owner: str,
        stage: int | None,
    ) -> None:
        self.seams.append(
            Seam(
                kind=kind,
                path=self.relative_path,
                line=node.lineno,
                target=target,
                canonical_owner=owner,
                migration_stage=stage,
            )
        )

    def _error(self, node: ast.AST, target: str, reason: str) -> None:
        line = getattr(node, "lineno", 0)
        self.errors.append(f"{self.relative_path}:{line} {target}: {reason}")

    @staticmethod
    def _adjust_aliases(
        inherited: set[str],
        root: set[str],
        collector: _LocalBindingCollector,
        local_aliases: set[str],
        parameters: set[str] | None = None,
    ) -> set[str]:
        local_bindings = collector.bindings - collector.globals - collector.nonlocals
        names = inherited - local_bindings - (parameters or set())
        for name in collector.globals:
            if name in root:
                names.add(name)
            else:
                names.discard(name)
        names.update(local_aliases)
        return names

    def _visit_arguments(self, arguments: ast.arguments) -> None:
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if arguments.vararg and arguments.vararg.annotation is not None:
            self.visit(arguments.vararg.annotation)
        if arguments.kwarg and arguments.kwarg.annotation is not None:
            self.visit(arguments.kwarg.annotation)
        for default in arguments.defaults:
            self.visit(default)
        for default in arguments.kw_defaults:
            if default is not None:
                self.visit(default)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_arguments(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)

        parent = self.frames[-1]
        collector = _scope_collector(self.path, node.body)
        parameters = _argument_names(node.args)
        module_names = self._adjust_aliases(
            parent.closure_module_names,
            self.root_module_names,
            collector,
            collector.module_aliases,
            parameters,
        )
        class_names = self._adjust_aliases(
            parent.closure_class_names,
            self.root_class_names,
            collector,
            collector.class_aliases,
            parameters,
        )
        instance_names = parent.closure_instance_names - _function_shadowed_names(
            self.path, node
        )
        instance_names.update(
            _extractor_bindings(
                node,
                parent.class_names,
                class_names,
            )
        )
        instance_names.update(
            _EXPLICIT_INSTANCE_BINDINGS.get((self.relative_path, node.name), ())
        )
        instance_names.difference_update(collector.globals)
        aliases, ambiguous_aliases = _collaborator_aliases(
            self.path, node, instance_names | class_names
        )
        self.functions.append(node)
        self.frames.append(
            _ScopeFrame(
                kind="function",
                module_names=module_names,
                class_names=class_names,
                instance_names=instance_names,
                closure_module_names=set(module_names),
                closure_class_names=set(class_names),
                closure_instance_names=set(instance_names),
                collaborator_aliases=aliases,
                ambiguous_aliases=ambiguous_aliases,
            )
        )
        for statement in node.body:
            self.visit(statement)
        self.frames.pop()
        self.functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def _apply_class_bindings(self, statement: ast.stmt, frame: _ScopeFrame) -> None:
        collector = _LocalBindingCollector(self.path)
        if isinstance(statement, ast.AnnAssign) and statement.value is None:
            return
        if isinstance(statement, ast.Match):
            for case in statement.cases:
                collector.visit(case.pattern)
        elif isinstance(
            statement,
            (
                ast.Assign,
                ast.AnnAssign,
                ast.AugAssign,
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Global,
                ast.Nonlocal,
            ),
        ):
            collector.visit(statement)
        else:
            return

        extractor_instances = _extractor_assignment_names(statement, frame.class_names)
        bindings = collector.bindings - collector.globals - collector.nonlocals
        frame.module_names.difference_update(bindings)
        frame.class_names.difference_update(bindings)
        frame.instance_names.difference_update(bindings)
        frame.ambiguous_names.difference_update(bindings)
        for name in collector.globals:
            if name in self.root_module_names:
                frame.module_names.add(name)
            else:
                frame.module_names.discard(name)
            if name in self.root_class_names:
                frame.class_names.add(name)
            else:
                frame.class_names.discard(name)
            frame.instance_names.discard(name)
        parent = self.frames[-2]
        for name in collector.nonlocals:
            if name in parent.closure_module_names:
                frame.module_names.add(name)
            else:
                frame.module_names.discard(name)
            if name in parent.closure_class_names:
                frame.class_names.add(name)
            else:
                frame.class_names.discard(name)
            if name in parent.closure_instance_names:
                frame.instance_names.add(name)
            else:
                frame.instance_names.discard(name)
        frame.module_names.update(collector.module_aliases)
        frame.class_names.update(collector.class_aliases)
        frame.instance_names.update(extractor_instances)
        frame.ambiguous_names.difference_update(
            collector.globals
            | collector.nonlocals
            | collector.module_aliases
            | collector.class_aliases
        )

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> Any:
        if node.type is not None:
            self.visit(node.type)
        if not node.name or self.frames[-1].kind != "class":
            for statement in node.body:
                self.visit(statement)
            return
        frame = self.frames[-1]
        frame.module_names.discard(node.name)
        frame.class_names.discard(node.name)
        frame.instance_names.discard(node.name)
        frame.ambiguous_names.discard(node.name)
        self._visit_class_suite(node.body)
        frame.module_names.discard(node.name)
        frame.class_names.discard(node.name)
        frame.instance_names.discard(node.name)
        frame.ambiguous_names.discard(node.name)
        if node.name in frame.closure_module_names:
            frame.module_names.add(node.name)
        if node.name in frame.closure_class_names:
            frame.class_names.add(node.name)
        if node.name in frame.closure_instance_names:
            frame.instance_names.add(node.name)

    def visit_match_case(self, node: ast.match_case) -> Any:
        self.visit(node.pattern)
        if self.frames[-1].kind != "class":
            if node.guard is not None:
                self.visit(node.guard)
            for statement in node.body:
                self.visit(statement)
            return
        frame = self.frames[-1]
        targets = _pattern_names(node.pattern)
        previous = {
            name: (
                name in frame.module_names,
                name in frame.class_names,
                name in frame.instance_names,
                name in frame.ambiguous_names,
            )
            for name in targets
        }
        frame.module_names.difference_update(targets)
        frame.class_names.difference_update(targets)
        frame.instance_names.difference_update(targets)
        frame.ambiguous_names.difference_update(targets)
        if node.guard is not None:
            self.visit(node.guard)
        self._visit_class_suite(node.body)
        for name, present in previous.items():
            for names, was_present in zip(
                (
                    frame.module_names,
                    frame.class_names,
                    frame.instance_names,
                    frame.ambiguous_names,
                ),
                present,
                strict=True,
            ):
                if was_present:
                    names.add(name)

    @staticmethod
    def _copy_frame(frame: _ScopeFrame) -> _ScopeFrame:
        return _ScopeFrame(
            kind=frame.kind,
            module_names=set(frame.module_names),
            class_names=set(frame.class_names),
            instance_names=set(frame.instance_names),
            closure_module_names=set(frame.closure_module_names),
            closure_class_names=set(frame.closure_class_names),
            closure_instance_names=set(frame.closure_instance_names),
            ambiguous_names=set(frame.ambiguous_names),
            collaborator_aliases=dict(frame.collaborator_aliases),
            ambiguous_aliases=set(frame.ambiguous_aliases),
        )

    def _visit_class_suite(self, statements: list[ast.stmt]) -> None:
        frame = self.frames[-1]
        for statement in statements:
            self.visit(statement)
            self._apply_class_bindings(statement, frame)

    def _merge_class_states(
        self, current: _ScopeFrame, states: list[_ScopeFrame]
    ) -> None:
        candidates = set().union(
            *(
                state.module_names | state.class_names | state.instance_names
                for state in states
            )
        )
        current.ambiguous_names = set().union(
            *(state.ambiguous_names for state in states)
        )
        current.ambiguous_names.update(
            name
            for name in candidates
            if len(
                {
                    (
                        name in state.module_names,
                        name in state.class_names,
                        name in state.instance_names,
                    )
                    for state in states
                }
            )
            > 1
        )
        current.module_names = set.intersection(
            *(state.module_names for state in states)
        )
        current.class_names = set.intersection(*(state.class_names for state in states))
        current.instance_names = set.intersection(
            *(state.instance_names for state in states)
        )

    def visit_If(self, node: ast.If) -> Any:
        if self.frames[-1].kind != "class":
            self.generic_visit(node)
            return
        self.visit(node.test)
        if isinstance(node.test, ast.Constant) and isinstance(node.test.value, bool):
            self._visit_class_suite(node.body if node.test.value else node.orelse)
            return

        current = self.frames[-1]
        states: list[_ScopeFrame] = []
        for statements in (node.body, node.orelse):
            branch = self._copy_frame(current)
            self.frames[-1] = branch
            self._visit_class_suite(statements)
            states.append(branch)
        self.frames[-1] = current
        self._merge_class_states(current, states)

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        if self.frames[-1].kind != "class":
            self.generic_visit(node)
            return
        outcome, handler_index = _static_try_outcome(node)
        current = self.frames[-1]
        if outcome == "normal":
            self._visit_class_suite(node.body)
            self._visit_class_suite(node.orelse)
        elif outcome == "handler" and handler_index is not None:
            self._visit_class_suite(node.body)
            self.visit(node.handlers[handler_index])
        else:
            states: list[_ScopeFrame] = []
            normal = self._copy_frame(current)
            self.frames[-1] = normal
            self._visit_class_suite(node.body)
            self._visit_class_suite(node.orelse)
            states.append(normal)
            for handler in node.handlers:
                branch = self._copy_frame(current)
                self.frames[-1] = branch
                self.visit(handler)
                states.append(branch)
            self.frames[-1] = current
            self._merge_class_states(current, states)
        self._visit_class_suite(node.finalbody)

    def visit_Try(self, node: ast.Try) -> Any:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> Any:
        self._visit_try(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)

        parent = self.frames[-1]
        frame = _ScopeFrame(
            kind="class",
            module_names=set(parent.closure_module_names),
            class_names=set(parent.closure_class_names),
            instance_names=set(parent.closure_instance_names),
            closure_module_names=set(parent.closure_module_names),
            closure_class_names=set(parent.closure_class_names),
            closure_instance_names=set(parent.closure_instance_names),
        )
        self.frames.append(frame)
        self._visit_class_suite(node.body)
        self.frames.pop()

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        self._visit_arguments(node.args)
        parent = self.frames[-1]
        collector = _LocalBindingCollector(self.path)
        collector.visit(node.body)
        parameters = _argument_names(node.args)
        module_names = self._adjust_aliases(
            parent.closure_module_names,
            self.root_module_names,
            collector,
            set(),
            parameters,
        )
        class_names = self._adjust_aliases(
            parent.closure_class_names,
            self.root_class_names,
            collector,
            set(),
            parameters,
        )
        local_bindings = collector.bindings - collector.globals - collector.nonlocals
        instance_names = parent.closure_instance_names - local_bindings - parameters
        self.frames.append(
            _ScopeFrame(
                kind="function",
                module_names=module_names,
                class_names=class_names,
                instance_names=instance_names,
                closure_module_names=set(module_names),
                closure_class_names=set(class_names),
                closure_instance_names=set(instance_names),
            )
        )
        self.visit(node.body)
        self.frames.pop()

    @staticmethod
    def _target_names(target: ast.expr) -> set[str]:
        return {
            item.id
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        }

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: list[ast.expr],
    ) -> None:
        first, *remaining = generators
        self.visit(first.iter)
        parent = self.frames[-1]
        module_names = set(parent.closure_module_names)
        class_names = set(parent.closure_class_names)
        instance_names = set(parent.closure_instance_names)
        frame = _ScopeFrame(
            kind="comprehension",
            module_names=module_names,
            class_names=class_names,
            instance_names=instance_names,
            closure_module_names=module_names,
            closure_class_names=class_names,
            closure_instance_names=instance_names,
        )
        self.frames.append(frame)
        for generator in (first, *remaining):
            if generator is not first:
                self.visit(generator.iter)
            self.visit(generator.target)
            targets = self._target_names(generator.target)
            frame.module_names.difference_update(targets)
            frame.class_names.difference_update(targets)
            frame.instance_names.difference_update(targets)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self.frames.pop()

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def _called_methods(self, nodes: list[ast.stmt], bindings: set[str]) -> set[str]:
        collector = _CalledMethodCollector(self.path, bindings, self.class_names)
        for node in nodes:
            collector.visit(node)
        for node, name in collector.errors:
            self._error(
                node,
                name,
                "ambiguous workflow binding after conditional class control flow",
            )
        return collector.methods

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        bindings = self.frames[-1].instance_names
        self.caller_contexts.append(self._called_methods(node.body, bindings))
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
        self.caller_contexts.pop()
        for statement in node.body:
            self.visit(statement)

    def visit_With(self, node: ast.With) -> Any:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> Any:
        self._visit_with(node)

    def _callers(self, target: str) -> list[tuple[str, int]]:
        if not self.functions:
            return []
        function = self.functions[-1]
        bindings = self.frames[-1].instance_names
        methods = (
            set(self.caller_contexts[-1])
            if self.caller_contexts
            else self._called_methods(function.body, bindings)
        )
        methods.update(self._parameterized_methods(function))
        methods.update(
            _EXPLICIT_CALLER_CONTEXTS.get(
                (self.relative_path, function.name, target), ()
            )
        )
        methods.update(
            _EXPLICIT_CALLER_CONTEXTS.get((self.relative_path, function.name, "*"), ())
        )
        owners = {
            (_WORKFLOW_OWNERS | _PRIVATE_OWNERS)[name]
            for name in methods
            if name in _WORKFLOW_OWNERS or name in _PRIVATE_OWNERS
        }
        return sorted(owners, key=lambda item: (item[1], item[0]))

    @staticmethod
    def _parameterized_methods(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> set[str]:
        methods: set[str] = set()
        for decorator in function.decorator_list:
            if not isinstance(decorator, ast.Call) or len(decorator.args) < 2:
                continue
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names = [part.strip() for part in first.value.split(",")]
            elif isinstance(first, (ast.List, ast.Tuple)) and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in first.elts
            ):
                names = [
                    item.value
                    for item in first.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
            else:
                continue
            if "method" not in names:
                continue
            method_index = names.index("method")
            values = decorator.args[1]
            if not isinstance(values, (ast.List, ast.Tuple)):
                continue
            for row in values.elts:
                candidates = (
                    row.elts if isinstance(row, (ast.List, ast.Tuple)) else [row]
                )
                if method_index >= len(candidates):
                    continue
                candidate = candidates[method_index]
                if isinstance(candidate, ast.Constant) and isinstance(
                    candidate.value, str
                ):
                    methods.add(candidate.value)
        return methods

    def _add_contextual(
        self,
        kind: str,
        node: ast.expr | ast.stmt,
        target: str,
        canonical_binding: str,
    ) -> None:
        callers = self._callers(target)
        if not callers:
            self._error(node, target, "caller workflow could not be resolved")
            return
        for caller_owner, stage in callers:
            self._add(
                kind,
                node,
                target,
                f"{caller_owner} -> {canonical_binding}",
                stage,
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        if _is_extractor_module_import(self.path, node):
            for alias in node.names:
                if alias.name in PERMANENT_ALIASES:
                    self._add(
                        "permanent_alias_import",
                        node,
                        alias.name,
                        PERMANENT_ALIASES[alias.name],
                        None,
                    )
                    continue
                owner_stage = _IMPORT_OWNERS.get(alias.name)
                if owner_stage is None:
                    self._error(node, alias.name, "unknown direct extractor import")
                    continue
                owner, stage = owner_stage
                self._add("direct_import", node, alias.name, owner, stage)
        elif _is_scraping_package_import(self.path, node):
            for alias in node.names:
                if alias.name == "extractor":
                    self._add(
                        "module_alias",
                        node,
                        alias.asname or alias.name,
                        "owner-local scraping modules",
                        14,
                    )
                elif alias.name == "LinkedInExtractor":
                    self._add(
                        "direct_import",
                        node,
                        alias.name,
                        *_IMPORT_OWNERS[alias.name],
                    )
        self.generic_visit(node)

    def _suppress_module_attributes(self, target: ast.expr) -> None:
        for item in ast.walk(target):
            if (
                isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and item.value.id in self.module_names
            ):
                self.suppressed_module_attributes.add(id(item))

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if id(node) not in self.suppressed_module_attributes and isinstance(
            node.value, ast.Name
        ):
            if node.value.id in self.frames[-1].ambiguous_names:
                self._error(
                    node,
                    node.value.id,
                    "ambiguous extractor binding after conditional class control flow",
                )
            elif node.value.id in self.module_names:
                self._direct_module_attribute(node, node.attr)
            elif (
                node.value.id in self.class_names
                or node.value.id in self.frames[-1].instance_names
            ):
                self._direct_facade_attribute(node, node.attr)
        self.generic_visit(node)

    def _direct_facade_attribute(self, node: ast.Attribute, name: str) -> None:
        if not name.startswith("_"):
            return
        collaborator = _FACADE_COLLABORATORS.get(name)
        if collaborator is not None:
            self._add_contextual("private_facade_access", node, name, collaborator[1])
            return
        owner_stage = (
            _PRIVATE_OWNERS.get(name)
            or _WORKFLOW_OWNERS.get(name)
            or _INSTANCE_ATTRIBUTE_OWNERS.get(name)
        )
        if owner_stage is not None:
            self._add("private_facade_access", node, name, *owner_stage)
            return
        if name in self.facade_privates:
            self._error(node, name, "private facade access has no migration owner")
            return
        self._error(node, name, "unknown private facade access")

    def _direct_module_attribute(self, node: ast.Attribute, name: str) -> None:
        if name in _IMPORTED_MODULE_NAMES:
            self._add(
                "module_attribute",
                node,
                name,
                "global imported object, unaffected by relocation",
                None,
            )
            return
        if name in _CONTEXTUAL_MODULE_NAMES:
            self._add_contextual(
                "module_attribute", node, name, f"owner-local {name} binding"
            )
            return
        if name in PERMANENT_ALIASES:
            self._add(
                "module_attribute",
                node,
                name,
                PERMANENT_ALIASES[name],
                None,
            )
            return
        owner_stage = (
            _MODULE_ATTRIBUTE_OWNERS.get(name)
            or _PRIVATE_OWNERS.get(name)
            or _IMPORT_OWNERS.get(name)
        )
        if owner_stage is not None:
            self._add("module_attribute", node, name, *owner_stage)
            return
        owner = _BOUNDARY_OWNERS.get(name)
        if owner is not None:
            self._add_contextual("module_attribute", node, name, owner)
            return
        self._error(node, name, "unknown direct extractor module attribute")

    def visit_Call(self, node: ast.Call) -> Any:
        function = node.func
        if (
            isinstance(function, ast.Name)
            and function.id == "patch"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith(EXTRACTOR_MODULE + ".")
        ):
            self._string_patch(node, node.args[0].value)

        if (
            isinstance(function, ast.Attribute)
            and function.attr == "object"
            and isinstance(function.value, ast.Name)
            and function.value.id == "patch"
        ):
            target, attribute = _patch_object_arguments(node)
            if target is None or attribute is None:
                # An arity or keyword shape the reader cannot take apart hides
                # both ends of the patch. Skipping it on a failed arity gate is
                # indistinguishable from a patch that intercepts nothing, so
                # the call names itself instead.
                self._error(
                    node, ast.unparse(node), "unresolved patch.object arguments"
                )
            else:
                self._suppress_module_attributes(target)
                self._patch_object(node, target, attribute)

        if (isinstance(function, ast.Name) and function.id == "setattr") or (
            isinstance(function, ast.Attribute) and function.attr == "setattr"
        ):
            self._setattr_call(node)
        self.generic_visit(node)

    def _setattr_call(self, node: ast.Call) -> None:
        """Inventory ``setattr`` and ``monkeypatch.setattr`` replacements.

        The method name is matched on its own rather than against a receiver
        named ``monkeypatch``, because the fixture is routinely bound to
        another name and a receiver test would skip exactly the sites this
        exists to see. Everything it resolves to is a foreign object unless the
        target reaches the extractor, so the breadth costs nothing. It costs
        something only where there is no target left to scope, which is what
        ``_refuse_unreadable_setattr`` answers for.
        """

        target, name = _setattr_arguments(node)
        if target is None:
            self._refuse_unreadable_setattr(node)
            return
        if (
            isinstance(target, ast.Constant)
            and isinstance(target.value, str)
            and target.value.startswith(EXTRACTOR_MODULE + ".")
        ):
            # A dotted import string holds the member name itself, so this
            # resolves whether or not the call passed a separate one.
            self._string_patch(node, target.value)
            return
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            self._refuse_unresolved_target(node, target, "setattr")
            return
        self._suppress_module_attributes(target)
        self._replacement(node, target, name.value, "setattr")

    def _refuse_unreadable_setattr(self, node: ast.Call) -> None:
        """Refuse a ``setattr`` whose call shape hides both of its ends.

        There is no target to scope against the extractor, and a skip reads the
        same as a patch that intercepts nothing, so the refusal has to be loud
        where the call could plausibly land on the extractor. Every other
        refusal here is narrowed by ``_reaches_the_extractor``; a blanket one
        answers the same way for a foreign ``helper.setattr(*arguments)``
        anywhere in the tree, and one false failure blocks every later stage.

        The receiver is the only signal the shape leaves. A bare name is the
        builtin, which takes any object including the extractor, so it stays
        loud. A method belongs to whatever the receiver holds, and only a
        pytest fixture patches the extractor through one. That is the trade-off
        against the docstring above: matching the method name alone is what
        keeps an aliased fixture inventoried, and it is free only while a
        readable target does the scoping. With the target gone the receiver has
        to answer instead, so the use-site collector proves that its binding is
        still authoritative in this lexical scope.
        """

        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and id(node) not in self.monkeypatch_calls
        ):
            return
        self._error(node, ast.unparse(node), "unresolved setattr arguments")

    def visit_Assign(self, node: ast.Assign) -> Any:
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                self._assigned_replacement(node, target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        # `owner.name: T = replacement` replaces the member exactly as the
        # unannotated form does, and skipping it left the member name
        # uninventoried while the reach-through above it was recorded. The
        # annotation-only `owner.name: T` binds nothing, so it replaces
        # nothing and must not be read as a patch.
        if node.value is not None and isinstance(node.target, ast.Attribute):
            self._assigned_replacement(node, node.target)
        self.generic_visit(node)

    def _assigned_replacement(
        self, node: ast.Assign | ast.AnnAssign, target: ast.Attribute
    ) -> None:
        """Inventory ``owner.name = replacement`` one level below the facade.

        An attribute assigned on the facade itself is already on the record
        through ``visit_Attribute``, which sees the replaced name directly. One
        level deeper that attribute is the reach-through and never the replaced
        member, so the collaborator's own name goes unchecked unless it is
        resolved here.
        """

        owner = target.value
        if isinstance(owner, ast.Name) and (
            owner.id in self.frames[-1].instance_names
            or owner.id in self.class_names
            or owner.id in self.module_names
        ):
            return
        if self._ambiguous_collaborator_alias(owner):
            self._error(
                node,
                ast.unparse(owner),
                "ambiguous collaborator alias after conditional control flow",
            )
            return
        collaborator = self._collaborator_reach(owner)
        if collaborator is not None:
            self._collaborator_patch(node, collaborator, target.attr)
            return
        self._refuse_unresolved_target(node, owner, "attribute assignment")

    def _string_patch(self, node: ast.Call, value: str) -> None:
        target = value.removeprefix(EXTRACTOR_MODULE + ".")
        root = target.split(".", 1)[0]
        if root in _IMPORTED_MODULE_NAMES and "." in target:
            self._add(
                "imported_module_patch",
                node,
                target,
                "global imported object, unaffected by relocation",
                None,
            )
            return
        if root in _CONTEXTUAL_MODULE_NAMES and "." in target:
            self._add_contextual(
                "imported_module_patch",
                node,
                target,
                f"owner-local {root} binding",
            )
            return
        owner = _BOUNDARY_OWNERS.get(target)
        if owner is not None:
            self._add_contextual("string_patch", node, value, owner)
            return
        owner_stage = _PRIVATE_OWNERS.get(target) or _MODULE_ATTRIBUTE_OWNERS.get(
            target
        )
        if owner_stage is not None:
            self._add("string_patch", node, value, *owner_stage)
            return
        self._error(node, value, "unknown string patch target")

    def _patch_object(
        self, node: ast.Call, target: ast.expr, attribute: ast.expr
    ) -> None:
        if not (
            isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)
        ):
            self._error(node, ast.unparse(target), "dynamic patch.object attribute")
            return
        self._replacement(node, target, attribute.value, "patch.object")

    def _replacement(
        self, node: ast.expr | ast.stmt, target: ast.expr, name: str, form: str
    ) -> None:
        # A walrus names the same object it evaluates to, so the binding is
        # irrelevant to what the patch reaches.
        while isinstance(target, ast.NamedExpr):
            target = target.value
        if (
            isinstance(target, ast.Name)
            and target.id in self.frames[-1].ambiguous_names
        ):
            self._error(
                node,
                target.id,
                "ambiguous extractor binding after conditional class control flow",
            )
            return
        if self._ambiguous_collaborator_alias(target):
            self._error(
                node,
                ast.unparse(target),
                "ambiguous collaborator alias after conditional control flow",
            )
            return
        bindings = self.frames[-1].instance_names
        is_facade_class = isinstance(target, ast.Name) and target.id in self.class_names
        if isinstance(target, ast.Name) and (target.id in bindings or is_facade_class):
            if name in self.facade_privates:
                owner_stage = _PRIVATE_OWNERS.get(name) or _WORKFLOW_OWNERS.get(name)
                if owner_stage is None:
                    self._error(
                        node, name, "private facade patch has no migration owner"
                    )
                    return
                self._add("private_patch_object", node, name, *owner_stage)
                return
            if name not in self.facade_publics:
                self._error(node, name, "unknown public facade patch")
                return
            if is_facade_class:
                owner_stage = _WORKFLOW_OWNERS.get(name)
                if owner_stage is None:
                    self._error(
                        node, name, "public facade patch has no migration owner"
                    )
                    return
                owner, stage = owner_stage
                self._add(
                    "public_patch_object",
                    node,
                    name,
                    f"{owner} dependency",
                    stage,
                )
                return
            callers = self._callers(name)
            if not callers:
                self._error(node, name, "public facade patch has no caller workflow")
                return
            for owner, stage in callers:
                self._add(
                    "public_patch_object",
                    node,
                    name,
                    f"{owner} dependency",
                    stage,
                )
            return

        if isinstance(target, ast.Name) and target.id in self.module_names:
            if name.startswith("_"):
                owner_stage = _PRIVATE_OWNERS.get(name)
                if owner_stage is None:
                    self._error(
                        node, name, "private module patch has no migration owner"
                    )
                    return
                self._add("private_patch_object", node, name, *owner_stage)
                return
            if name in _IMPORTED_MODULE_NAMES | _CONTEXTUAL_MODULE_NAMES:
                self._add_contextual(
                    "module_rebind_patch",
                    node,
                    name,
                    f"owner-local {name} module binding",
                )
                return
            owner = _BOUNDARY_OWNERS.get(name)
            if owner is None:
                self._error(node, name, "unknown extractor module boundary patch")
                return
            self._add_contextual("boundary_patch_object", node, name, owner)
            return

        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in self.module_names
        ):
            patch_target = f"{target.attr}.{name}"
            if target.attr in _CONTEXTUAL_MODULE_NAMES:
                self._add_contextual(
                    "imported_module_patch",
                    node,
                    patch_target,
                    f"owner-local {target.attr} binding",
                )
                return
            if target.attr in _IMPORTED_MODULE_NAMES:
                self._add(
                    "imported_module_patch",
                    node,
                    patch_target,
                    "global imported object, unaffected by relocation",
                    None,
                )
                return
            self._error(
                node,
                patch_target,
                "unknown extractor module object patch",
            )
            return

        collaborator = self._collaborator_reach(target)
        if collaborator is not None:
            self._collaborator_patch(node, collaborator, name)
            return

        self._refuse_unresolved_target(node, target, form)

    def _collaborator_reach(self, target: ast.expr) -> str | None:
        """Name the facade attribute a replacement target reaches through."""

        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and (
                target.value.id in self.frames[-1].instance_names
                or target.value.id in self.class_names
            )
        ):
            return target.attr
        if isinstance(target, ast.Name):
            for frame in reversed(self.frames):
                alias = frame.collaborator_aliases.get(id(target))
                if alias is not None:
                    return alias
        return None

    def _ambiguous_collaborator_alias(self, target: ast.expr) -> bool:
        """Answer whether a local's collaborator binding depends on a branch.

        A name that held the collaborator down one path and something else down
        another fits no single answer, so resolving it either way would invent
        one. Only a name already bound to a collaborator somewhere reaches this,
        which is why the refusal needs no further scoping.
        """

        if not isinstance(target, ast.Name):
            return False
        return any(id(target) in frame.ambiguous_aliases for frame in self.frames)

    def _reaches_the_extractor(self, target: ast.expr) -> bool:
        """Answer whether an unreduced target still names the extractor.

        A refusal has to be narrower than "could not resolve", or every
        ``patch.object`` in the tree that replaces a member of some foreign
        object becomes an error. Two signals survive a shape the reader cannot
        reduce: a name it already knows is the extractor module, the facade
        class or a facade instance, and a facade wiring attribute anywhere in
        the expression. Measured over every site that falls through today:
        neither fires on any of the 137, and between them they catch the
        chained, ``getattr``-routed and ``self``-rooted reach-throughs.
        """

        frame = self.frames[-1]
        known = (
            frame.module_names
            | frame.class_names
            | frame.instance_names
            | frame.ambiguous_names
        )
        wiring = set(_FACADE_COLLABORATORS) | set(_INSTANCE_ATTRIBUTE_OWNERS)
        return any(
            (isinstance(item, ast.Name) and item.id in known)
            or (isinstance(item, ast.Attribute) and item.attr in wiring)
            for item in ast.walk(target)
        )

    def _refuse_unresolved_target(
        self, node: ast.expr | ast.stmt, target: ast.expr, form: str
    ) -> None:
        if not self._reaches_the_extractor(target):
            return
        self._error(node, ast.unparse(target), f"unresolved {form} target")

    def _collaborator_patch(
        self, node: ast.expr | ast.stmt, attribute: str, name: str
    ) -> None:
        collaborator = _FACADE_COLLABORATORS.get(attribute)
        patch_target = f"{attribute}.{name}"
        if collaborator is None:
            self._error(node, patch_target, "unknown facade collaborator patch")
            return
        owner, _ = collaborator
        try:
            methods = collaborator_methods(owner)
        except UnresolvedSeamError as error:
            self._error(node, patch_target, str(error))
            return
        if name not in methods:
            self._error(node, patch_target, f"unknown {owner} patch")
            return
        callers = self._callers(name)
        if not callers:
            self._error(node, patch_target, "collaborator patch has no caller workflow")
            return
        for caller_owner, stage in callers:
            if name.startswith("_"):
                # A private member is owned where it is defined, so the owner
                # stays the collaborator and only the stage follows the caller.
                self._add("private_patch_object", node, name, owner, stage)
                continue
            # A public member *is* the collaborator rather than a dependency of
            # it, so the owner names the workflow consuming it, matching every
            # other `public_patch_object` entry.
            self._add(
                "public_patch_object", node, name, f"{caller_owner} dependency", stage
            )


def scan_source(
    path: Path,
    source: str,
    facade_publics: frozenset[str],
    facade_privates: frozenset[str],
) -> list[Seam]:
    """Scan one source string and fail on every unresolved extractor seam."""

    tree = ast.parse(source, filename=str(path))
    module_aliases(path, tree)
    root = _scope_collector(path, tree.body)
    class_names = set(root.class_aliases)
    if path == PACKAGE / "scraping" / "extractor.py":
        class_names.add("LinkedInExtractor")
    scanner = Scanner(
        path,
        root.module_aliases,
        class_names,
        facade_publics,
        facade_privates,
        _monkeypatch_calls(path, tree),
    )
    scanner.visit(tree)
    if scanner.errors:
        raise UnresolvedSeamError("\n".join(scanner.errors))
    return scanner.seams


def scan() -> dict[str, Any]:
    seams: list[Seam] = []
    publics, privates = extractor_methods()
    sources = sorted(TESTS.rglob("*.py")) + sorted(PACKAGE.rglob("*.py"))
    errors: list[str] = []
    for path in sources:
        try:
            seams.extend(
                scan_source(path, path.read_text(encoding="utf-8"), publics, privates)
            )
        except UnresolvedSeamError as error:
            errors.extend(str(error).splitlines())
    if errors:
        raise UnresolvedSeamError("\n".join(errors))
    seams.sort(
        key=lambda seam: (
            seam.path,
            seam.line,
            seam.kind,
            seam.target,
            seam.migration_stage if seam.migration_stage is not None else -1,
            seam.canonical_owner,
        )
    )
    return {
        "schema_version": 2,
        "extractor_parent": "70e50ada68b9389f8d315df6ab1e56c08f6c985b",
        "seams": [asdict(seam) for seam in seams],
    }


def _defines_class(path: Path, name: str) -> bool:
    if not path.exists():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.ClassDef) and node.name == name for node in tree.body
    )


def _uses_capture_mode(path: Path) -> bool:
    if not path.exists():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"CaptureMode", "CapturePlan"}
        )
        or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CapturePlan"
        )
        for node in ast.walk(tree)
    )


def _explicit_capture_modes(scraping: Path) -> bool:
    return (
        (
            _defines_class(scraping / "capture.py", "CaptureMode")
            or _defines_class(scraping / "capture.py", "CapturePlan")
        )
        and _uses_capture_mode(scraping / "fields.py")
        and all(
            _uses_capture_mode(scraping / module)
            for module in ("person.py", "company.py", "jobs.py", "posts.py")
        )
    )


def _slim_facade(path: Path) -> bool:
    if not path.exists():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    facade = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor"
        ),
        None,
    )
    if facade is None:
        return False
    methods = {
        node.name
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return (
        not {name for name in methods if name.startswith("_") and name != "__init__"}
        and _TOOL_METHODS <= methods
        and methods <= _TOOL_METHODS | _COMPATIBILITY_METHODS | {"__init__"}
    )


def completed_stage(package: Path = PACKAGE) -> int:
    """Infer the completed migration stage from durable tree structure."""

    scraping = package / "scraping"
    completed = 0
    for stage, modules in _STAGE_MODULES.items():
        if not all((scraping / module).is_file() for module in modules):
            break
        completed = stage
    if completed == 12 and _explicit_capture_modes(scraping):
        completed = 13
    if completed == 13 and _slim_facade(scraping / "extractor.py"):
        completed = 14
    return completed


def effective_stage(tree_stage: int, override: int | None) -> int:
    """Apply an optional stage override without weakening the inferred gate."""

    if override is None:
        return tree_stage
    if override < tree_stage:
        raise ValueError(
            f"--stage {override} is below tree-derived completed stage {tree_stage}"
        )
    return override


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _inside_fixture_root(path: Path) -> bool:
    return path.resolve().is_relative_to(FIXTURE_ROOT.resolve())


def _write_new_output(path: Path, content: str) -> None:
    if _inside_fixture_root(path):
        raise ValueError(
            f"refusing to write generated output inside canonical fixture directory: {path}"
        )
    if path.exists():
        raise ValueError(f"refusing to overwrite generated output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--output", type=Path)
    parser.add_argument("--stage", type=int)
    args = parser.parse_args()

    try:
        current = scan()
        stage = effective_stage(completed_stage(), args.stage)
    except (UnresolvedSeamError, ValueError) as error:
        print(f"unresolved extractor migration inventory:\n{error}", file=sys.stderr)
        return 1

    actual = canonical_json(current)
    failed = False
    if args.output is not None:
        try:
            _write_new_output(args.output, actual)
        except ValueError as error:
            parser.error(str(error))
        print(args.output)
    else:
        expected = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        if expected != actual:
            failed = True
            sys.stderr.writelines(
                unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile=str(MANIFEST),
                    tofile="current extractor seam inventory",
                )
            )

    obsolete = [
        seam
        for seam in current["seams"]
        if seam["migration_stage"] is not None and seam["migration_stage"] <= stage
    ]
    if obsolete:
        failed = True
        for seam in obsolete:
            print(
                f"obsolete at stage {stage}: {seam['path']}:{seam['line']} "
                f"{seam['kind']} {seam['target']} -> {seam['canonical_owner']}",
                file=sys.stderr,
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
