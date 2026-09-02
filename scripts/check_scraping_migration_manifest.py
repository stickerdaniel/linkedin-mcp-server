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
MANIFEST = TESTS / "fixtures" / "scraping-policy" / "migration-manifest.json"
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

# Module-level names reached through ``patch.object(<extractor alias>, ...)``.
# A patch here stops intercepting once the caller lives in another module and
# imports the name for itself, so each one is real migration work.
_BOUNDARY_OWNERS: dict[str, tuple[str, int]] = {
    "record_page_trace": ("navigation.PageNavigator", 3),
    "detect_auth_barrier": ("navigation.PageNavigator", 3),
    "detect_auth_barrier_quick": ("navigation.PageNavigator", 3),
    "resolve_remember_me_prompt": ("navigation.PageNavigator", 3),
    "stabilize_navigation": ("navigation.PageNavigator", 3),
    "detect_rate_limit": ("session.ScrapingSession.check_rate_limit", 3),
    "handle_modal_close": ("session.ScrapingSession.dismiss_modal", 3),
    "scroll_to_bottom": ("session.ScrapingSession.scroll_body", 3),
    "scroll_job_sidebar": ("session.ScrapingSession.scroll_job_sidebar", 3),
    "build_issue_diagnostics": ("owning service diagnostic boundary", 14),
    "strip_linkedin_noise": ("text.strip_linkedin_noise", 1),
    "build_references": ("link_metadata.build_references", 1),
}

_CLASS_STAGES = {
    "Feed": 5,
    "ActivityFeed": 5,
    "Person": 6,
    "Profile": 6,
    "Sidebar": 6,
    "Connection": 7,
    "Action": 7,
    "Company": 8,
    "Job": 9,
    "SearchJobs": 9,
    "SavedJobs": 9,
    "Post": 10,
    "Content": 10,
    "Inbox": 11,
    "Conversation": 11,
    "Message": 12,
    "ExtractPage": 4,
    "Navigation": 3,
}

_STRING_OWNERS = {
    "asyncio.sleep": "session.ScrapingSession.sleep",
    "detect_rate_limit": "session.ScrapingSession.check_rate_limit",
    "handle_modal_close": "session.ScrapingSession.dismiss_modal",
    "scroll_to_bottom": "session.ScrapingSession.scroll_body",
    "scroll_job_sidebar": "session.ScrapingSession.scroll_job_sidebar",
    "detect_auth_barrier": "navigation.PageNavigator",
    "detect_auth_barrier_quick": "navigation.PageNavigator",
    "resolve_remember_me_prompt": "navigation.PageNavigator",
    "record_page_trace": "navigation.PageNavigator",
    "build_issue_diagnostics": "owning service diagnostic boundary",
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


@dataclass(frozen=True, slots=True)
class Seam:
    kind: str
    path: str
    line: int
    target: str
    canonical_owner: str
    migration_stage: int | None


def _stage_for_context(classes: list[str], default: int) -> int:
    for class_name in reversed(classes):
        for marker, stage in _CLASS_STAGES.items():
            if marker in class_name:
                return stage
    return default


class Scanner(ast.NodeVisitor):
    def __init__(
        self, path: Path, module_aliases: set[str], facade_privates: frozenset[str]
    ):
        self.path = path
        self.classes: list[str] = []
        self.seams: list[Seam] = []
        self.facade_privates = facade_privates
        # Every local name bound to the extractor module. ``patch.object``
        # against any of them is the same seam, so recognizing only the literal
        # name ``extractor`` would miss a file that imported it as something
        # else and leave that seam out of the inventory entirely.
        self.module_aliases = module_aliases

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
                path=self.path.relative_to(ROOT).as_posix(),
                line=node.lineno,
                target=target,
                canonical_owner=owner,
                migration_stage=stage,
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, str) and node.value.startswith(
            EXTRACTOR_MODULE + "."
        ):
            suffix = node.value.removeprefix(EXTRACTOR_MODULE + ".")
            root_target = next(
                (target for target in _STRING_OWNERS if suffix.startswith(target)),
                suffix,
            )
            owner = _STRING_OWNERS.get(root_target, "extractor migration owner")
            stage = _stage_for_context(self.classes, 14)
            self._add("string_patch", node, node.value, owner, stage)

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
                owner, stage = _IMPORT_OWNERS.get(
                    alias.name, ("extractor compatibility surface", 14)
                )
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
        is_patch_object = (
            isinstance(function, ast.Attribute)
            and function.attr == "object"
            and isinstance(function.value, ast.Name)
            and function.value.id == "patch"
        )
        if is_patch_object and len(node.args) >= 2:
            target, attribute = node.args[:2]
            if not (
                isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)
            ):
                self.generic_visit(node)
                return None
            name = attribute.value
            if isinstance(target, ast.Name) and name in self.facade_privates:
                owner, stage = _PRIVATE_OWNERS.get(name, ("owner-local service", 14))
                self._add("private_patch_object", node, name, owner, stage)
            elif isinstance(target, ast.Name) and target.id in self.module_aliases:
                if name.startswith("_"):
                    owner, stage = _PRIVATE_OWNERS.get(
                        name, ("owner-local service", 14)
                    )
                    kind = "private_patch_object"
                else:
                    owner, stage = _BOUNDARY_OWNERS.get(
                        name, ("owner-local service boundary", 14)
                    )
                    kind = "boundary_patch_object"
                self._add(kind, node, name, owner, stage)
            elif (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in self.module_aliases
            ):
                # ``patch.object(extractor.asyncio, "sleep")`` reaches the
                # standard-library module itself, so relocation cannot break
                # it and it carries no migration stage.
                self._add(
                    "imported_module_patch",
                    node,
                    f"{target.attr}.{name}",
                    "global stdlib module, unaffected by relocation",
                    None,
                )
        self.generic_visit(node)


def extractor_private_methods() -> frozenset[str]:
    """Read the private method names of ``LinkedInExtractor`` from source.

    A ``patch.object`` seam is identified by the *name* it patches, not by what
    the first argument happens to be called: the extractor is bound to a local
    named ``extractor`` in most tests and to something else in others, and a
    module alias shadows neither. Matching on the class's own method names
    catches all three.
    """

    tree = ast.parse(
        (PACKAGE / "scraping" / "extractor.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor":
            return frozenset(
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name.startswith("_")
            )
    raise AssertionError("LinkedInExtractor not found in scraping/extractor.py")


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


def scan() -> dict[str, Any]:
    seams: list[Seam] = []
    privates = extractor_private_methods()
    sources = sorted(TESTS.rglob("*.py")) + sorted(PACKAGE.rglob("*.py"))
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanner = Scanner(path, module_aliases(tree), privates)
        scanner.visit(tree)
        seams.extend(scanner.seams)
    seams.sort(key=lambda seam: (seam.path, seam.line, seam.kind, seam.target))
    return {
        "schema_version": 1,
        "extractor_parent": "c5e5e5a6b142e910374b7b26558addfd49ed7f84",
        "seams": [asdict(seam) for seam in seams],
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stage", type=int, default=0)
    args = parser.parse_args()
    if not args.check:
        parser.error("compare-only checker requires --check")

    current = scan()
    expected = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
    actual = canonical_json(current)
    failed = expected != actual
    if failed:
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
        if seam["migration_stage"] is not None and seam["migration_stage"] <= args.stage
    ]
    if obsolete:
        failed = True
        for seam in obsolete:
            print(
                f"obsolete at stage {args.stage}: {seam['path']}:{seam['line']} "
                f"{seam['kind']} {seam['target']} -> {seam['canonical_owner']}",
                file=sys.stderr,
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
