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
    "_navigate_to_page": ("navigation.PageNavigator", 3),
    "_raise_if_auth_barrier": ("navigation.PageNavigator", 3),
    "_log_navigation_failure": ("navigation.PageNavigator", 3),
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
    "_build_feed_references": ("feed_payload.build_feed_references", 1),
    "_truncate_linkedin_noise": ("text.truncate_linkedin_noise", 1),
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
    "_RATE_LIMITED_MSG": ("contracts.RATE_LIMITED_SECTION_TEXT", 1),
    "_truncate_linkedin_noise": ("text.truncate_linkedin_noise", 1),
    "_build_feed_references": ("feed_payload.build_feed_references", 1),
    "_CONTENT_DATE_POSTED_MAP": ("search_urls.CONTENT_DATE_POSTED_MAP", 2),
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

_INSTANCE_ATTRIBUTE_OWNERS = {
    "_page": ("facade.LinkedInExtractor._page", 14),
    "_scroll_seconds": ("session.ScrapingSession._scroll_seconds", 3),
}

_MODULE_ATTRIBUTE_OWNERS = {
    "_URL_SETTLE_LAG": ("navigation.PageNavigator.URL_SETTLE_LAG", 3),
    "_URL_SETTLE_QUIET": ("navigation.PageNavigator.URL_SETTLE_QUIET", 3),
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
    "_watching_navigations": ("job_pages.JobPageReader", 9),
    "_document_origin": ("job_pages.JobPageReader", 9),
    "_settle_navigation": ("job_pages.JobPageReader", 9),
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
        "_extract_loaded_section",
        "_extract_overlay_once",
        "_extract_feed_body",
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
        "_extract_loaded_section",
        "_extract_feed_body",
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
    ): ("_extract_loaded_section", "_extract_saved_jobs_page_once"),
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
        "extract_feed",
        "extract_page",
        "_extract_overlay",
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


class _CalledMethodCollector(ast.NodeVisitor):
    def __init__(self, bindings: set[str]) -> None:
        self.bindings = [set(bindings)]
        self.closure_bindings = [set(bindings)]
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
        collector = _LocalBindingCollector()
        for statement in node.body:
            collector.visit(statement)
        local_bindings = (
            collector.bindings - collector.globals - collector.nonlocals
        ) | _argument_names(node.args)
        bindings = self.closure_bindings[-1] - local_bindings - collector.globals
        ambiguous = self.closure_ambiguous_names[-1] - local_bindings
        self.bindings.append(bindings)
        self.closure_bindings.append(set(bindings))
        self.ambiguous_names.append(ambiguous)
        self.closure_ambiguous_names.append(set(ambiguous))
        for statement in node.body:
            self.visit(statement)
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
        self.closure_bindings.pop()
        self.bindings.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function_definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function_definition(node)

    def _apply_class_binding(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.AnnAssign) and statement.value is None:
            return
        collector = _LocalBindingCollector()
        if isinstance(statement, ast.Match):
            targets = set().union(
                *(_pattern_names(case.pattern) for case in statement.cases)
            )
            self.bindings[-1].difference_update(targets)
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
            collector.visit(statement)
            self.bindings[-1].difference_update(collector.bindings)
            self.ambiguous_names[-1].difference_update(collector.bindings)

    def _visit_class_suite(self, statements: list[ast.stmt]) -> None:
        for statement in statements:
            self.visit(statement)
            self._apply_class_binding(statement)

    def _called_state(self) -> tuple[set[str], set[str]]:
        return (set(self.bindings[-1]), set(self.ambiguous_names[-1]))

    def _restore_called_state(self, state: tuple[set[str], set[str]]) -> None:
        self.bindings[-1] = set(state[0])
        self.ambiguous_names[-1] = set(state[1])

    def _merge_called_states(self, states: list[tuple[set[str], set[str]]]) -> None:
        bindings = set.intersection(*(state[0] for state in states))
        ambiguous = set().union(*(state[1] for state in states))
        candidates = set().union(*(state[0] for state in states))
        ambiguous.update(
            name
            for name in candidates
            if len({name in state[0] for state in states}) > 1
        )
        self.bindings[-1] = bindings
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
        states: list[tuple[set[str], set[str]]] = []
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
            states: list[tuple[set[str], set[str]]] = []
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
        self.ambiguous_names[-1].discard(node.name)
        self._visit_class_suite(node.body)
        self.bindings[-1].discard(node.name)
        self.ambiguous_names[-1].discard(node.name)
        if node.name in self.closure_bindings[-1]:
            self.bindings[-1].add(node.name)
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
        previous_ambiguous = set(self.ambiguous_names[-1])
        self.bindings[-1].difference_update(targets)
        self.ambiguous_names[-1].difference_update(targets)
        if node.guard is not None:
            self.visit(node.guard)
        self._visit_class_suite(node.body)
        self.bindings[-1] = previous_bindings
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
        closure_ambiguous = set(self.closure_ambiguous_names[-1])
        self.bindings.append(set(closure))
        self.closure_bindings.append(closure)
        self.ambiguous_names.append(set(closure_ambiguous))
        self.closure_ambiguous_names.append(closure_ambiguous)
        self.class_scope_depths.add(len(self.bindings))
        self._visit_class_suite(node.body)
        self.class_scope_depths.remove(len(self.bindings))
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
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
        self.bindings.append(bindings)
        self.closure_bindings.append(set(bindings))
        self.ambiguous_names.append(ambiguous)
        self.closure_ambiguous_names.append(set(ambiguous))
        self.visit(node.body)
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
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
        ambiguous = set(self.closure_ambiguous_names[-1])
        self.bindings.append(bindings)
        self.closure_bindings.append(bindings)
        self.ambiguous_names.append(ambiguous)
        self.closure_ambiguous_names.append(ambiguous)
        for generator in (first, *remaining):
            if generator is not first:
                self.visit(generator.iter)
            self.visit(generator.target)
            targets = self._target_names(generator.target)
            self.bindings[-1].difference_update(targets)
            self.ambiguous_names[-1].difference_update(targets)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self.closure_ambiguous_names.pop()
        self.ambiguous_names.pop()
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


class Scanner(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        module_names: set[str],
        class_names: set[str],
        facade_publics: frozenset[str],
        facade_privates: frozenset[str],
    ):
        self.path = path
        self.root_module_names = module_names
        self.root_class_names = class_names
        self.facade_publics = facade_publics
        self.facade_privates = facade_privates
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
        collector = _CalledMethodCollector(bindings)
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
            and len(node.args) >= 2
        ):
            self._suppress_module_attributes(node.args[0])
            self._patch_object(node, node.args[0], node.args[1])
        self.generic_visit(node)

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
        name = attribute.value
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
