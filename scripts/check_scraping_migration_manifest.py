#!/usr/bin/env python3
"""Check the extractor patch/import migration inventory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
    "_read_profile_display_name": ("profile_page.ProfilePageReader", 6),
    "_resolve_message_compose_href": ("message_sender.MessageSender", 12),
    "_wait_for_message_surface": ("message_sender.MessageSender", 12),
    "_select_message_recipient": ("message_sender.MessageSender", 12),
    "_resolve_message_compose_box": ("message_sender.MessageSender", 12),
    "_compose_page_matches_recipient": ("message_sender.MessageSender", 12),
    "_message_text_visible": ("message_sender.MessageSender", 12),
    "_dismiss_message_ui": ("message_sender.MessageSender", 12),
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
    ("tests/test_scraping.py", "_patch_send_message_to_compose"): ("extractor",),
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
    ): ("extract_page",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "handle_modal_close",
    ): ("extract_page",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "scroll_to_bottom",
    ): ("extract_page",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "scroll_job_sidebar",
    ): ("extract_page",),
    (
        "tests/scraping/policy_scenarios.py",
        "boundaries",
        "build_issue_diagnostics",
    ): ("extract_page",),
    (
        "tests/test_scraping.py",
        "_patch_send_message_to_compose",
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
_IMPORTED_MODULE_NAMES = {"asyncio", "time", "logger"}


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


def _extractor_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef, class_aliases: set[str]
) -> set[str]:
    bindings = {
        argument.arg
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if _annotation_name(argument.annotation) in class_aliases
    }
    if (
        node.args.vararg
        and _annotation_name(node.args.vararg.annotation) in class_aliases
    ):
        bindings.add(node.args.vararg.arg)
    if (
        node.args.kwarg
        and _annotation_name(node.args.kwarg.annotation) in class_aliases
    ):
        bindings.add(node.args.kwarg.arg)

    for item in _walk_scope(node):
        if not isinstance(item, (ast.Assign, ast.AnnAssign)):
            continue
        value = item.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in class_aliases
        ):
            continue
        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
        bindings.update(target.id for target in targets if isinstance(target, ast.Name))
    return bindings


def _class_aliases(tree: ast.AST) -> set[str]:
    aliases = {"LinkedInExtractor"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module not in {EXTRACTOR_MODULE, "linkedin_mcp_server.scraping"}:
            continue
        for alias in node.names:
            if alias.name == "LinkedInExtractor":
                aliases.add(alias.asname or alias.name)
    return aliases


def module_aliases(tree: ast.AST) -> set[str]:
    """Collect every local name bound to the extractor module in one file."""

    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "linkedin_mcp_server.scraping":
                for alias in node.names:
                    if alias.name == "extractor":
                        aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == EXTRACTOR_MODULE and alias.asname:
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
        self.module_names = module_names
        self.class_names = class_names
        self.facade_publics = facade_publics
        self.facade_privates = facade_privates
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.instance_bindings: list[set[str]] = []
        self.caller_contexts: list[set[str]] = []
        self.seams: list[Seam] = []
        self.errors: list[str] = []

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

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node)
        bindings = _extractor_bindings(node, self.class_names)
        bindings.update(
            _EXPLICIT_INSTANCE_BINDINGS.get((self.relative_path, node.name), ())
        )
        self.instance_bindings.append(bindings)
        self.generic_visit(node)
        self.instance_bindings.pop()
        self.functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    @staticmethod
    def _called_methods(nodes: list[ast.stmt], bindings: set[str]) -> set[str]:
        methods: set[str] = set()
        for node in nodes:
            for item in (node, *_walk_scope(node)):
                if not isinstance(item, ast.Call):
                    continue
                called = item.func
                if (
                    isinstance(called, ast.Attribute)
                    and isinstance(called.value, ast.Name)
                    and called.value.id in bindings
                ):
                    methods.add(called.attr)
                    continue
                if not (
                    isinstance(called, ast.Call)
                    and isinstance(called.func, ast.Name)
                    and called.func.id == "getattr"
                    and len(called.args) >= 2
                    and isinstance(called.args[0], ast.Name)
                    and called.args[0].id in bindings
                    and isinstance(called.args[1], ast.Constant)
                    and isinstance(called.args[1].value, str)
                ):
                    continue
                methods.add(called.args[1].value)
        return methods

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        bindings = self.instance_bindings[-1] if self.instance_bindings else set()
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
        bindings = self.instance_bindings[-1]
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
        if node.module == EXTRACTOR_MODULE:
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
        elif node.module == "linkedin_mcp_server.scraping":
            for alias in node.names:
                if alias.name == "extractor":
                    self._add(
                        "module_alias",
                        node,
                        alias.asname or alias.name,
                        "owner-local scraping modules",
                        14,
                    )
        self.generic_visit(node)

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
        owner = _BOUNDARY_OWNERS.get(target)
        if owner is not None:
            self._add_contextual("string_patch", node, value, owner)
            return
        private = _PRIVATE_OWNERS.get(target)
        if private is not None:
            self._add("string_patch", node, value, *private)
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
        bindings = self.instance_bindings[-1] if self.instance_bindings else set()
        if isinstance(target, ast.Name) and target.id in bindings:
            if name in self.facade_privates:
                owner_stage = _PRIVATE_OWNERS.get(name)
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
            if name in _IMPORTED_MODULE_NAMES:
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
            if target.attr not in _IMPORTED_MODULE_NAMES:
                self._error(
                    node,
                    patch_target,
                    "unknown extractor module object patch",
                )
                return
            self._add(
                "imported_module_patch",
                node,
                patch_target,
                "global imported object, unaffected by relocation",
                None,
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
    scanner = Scanner(
        path,
        module_aliases(tree),
        _class_aliases(tree),
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
        "extractor_parent": "c5e5e5a6b142e910374b7b26558addfd49ed7f84",
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
