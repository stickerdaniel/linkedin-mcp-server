"""Structural contracts for the final extractor facade."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import ast
import inspect
import sys

import pytest

from linkedin_mcp_server.scraping import LinkedInExtractor
from linkedin_mcp_server.scraping import contracts, text


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "linkedin_mcp_server"
SCRAPING = PACKAGE / "scraping"
EXTRACTOR = SCRAPING / "extractor.py"

PUBLIC_SIGNATURES = {
    "click_button_by_text": "(self, text: 'str', *, scope: 'str' = 'main', timeout: 'int' = 5000) -> 'bool'",
    "connect_with_person": "(self, username: 'str', *, note: 'str | None' = None) -> 'dict[str, Any]'",
    "extract_feed": "(self, num_posts: 'int' = 10) -> 'ExtractedSection'",
    "extract_page": "(self, url: 'str', section_name: 'str', max_scrolls: 'int | None' = None) -> 'ExtractedSection'",
    "get_company_employees": "(self, company_name: 'str', keywords: 'str | None' = None) -> 'dict[str, Any]'",
    "get_conversation": "(self, linkedin_username: 'str | None' = None, thread_id: 'str | None' = None, index: 'int' = 0) -> 'dict[str, Any]'",
    "get_inbox": "(self, limit: 'int' = 20) -> 'dict[str, Any]'",
    "get_my_profile": "(self, sections: 'set[str] | None' = None, callbacks: 'ProgressCallback | None' = None, max_scrolls: 'int | None' = None) -> 'dict[str, Any]'",
    "get_page_text": "(self) -> 'str'",
    "get_saved_jobs": "(self, max_pages: 'int' = 3) -> 'dict[str, Any]'",
    "get_sidebar_profiles": "(self, username: 'str') -> 'dict[str, Any]'",
    "scrape_company": "(self, company_name: 'str', requested: 'set[str]', callbacks: 'ProgressCallback | None' = None) -> 'dict[str, Any]'",
    "scrape_job": "(self, job_id: 'str') -> 'dict[str, Any]'",
    "scrape_person": "(self, username: 'str', requested: 'set[str]', callbacks: 'ProgressCallback | None' = None, max_scrolls: 'int | None' = None, *, main_profile_already_loaded: 'bool' = False, allow_self_alias: 'bool' = False) -> 'dict[str, Any]'",
    "search_companies": "(self, keywords: 'str') -> 'dict[str, Any]'",
    "search_conversations": "(self, keywords: 'str', limit: 'int' = 20) -> 'dict[str, Any]'",
    "search_jobs": "(self, keywords: 'str', location: 'str | None' = None, max_pages: 'int' = 3, date_posted: 'str | None' = None, job_type: 'str | None' = None, experience_level: 'str | None' = None, work_type: 'str | None' = None, easy_apply: 'bool' = False, sort_by: 'str | None' = None, tool_timeout: 'float' = 180.0) -> 'dict[str, Any]'",
    "search_people": "(self, keywords: 'str', location: 'str | None' = None, network: 'list[str] | None' = None, current_company: 'str | None' = None) -> 'dict[str, Any]'",
    "search_posts": "(self, keywords: 'str', date_posted: 'str | None' = None, max_pages: 'int' = 3) -> 'dict[str, Any]'",
    "send_message": "(self, linkedin_username: 'str', message: 'str', *, confirm_send: 'bool', profile_urn: 'str | None' = None) -> 'dict[str, Any]'",
}

DELEGATES = {
    "click_button_by_text": ("_content", "click_button_by_text"),
    "connect_with_person": ("_connection", "connect_with_person"),
    "extract_feed": ("_feed", "extract_feed"),
    "extract_page": ("_capture", "extract_page"),
    "get_company_employees": ("_company", "get_company_employees"),
    "get_conversation": ("_conversations", "get_conversation"),
    "get_inbox": ("_conversations", "get_inbox"),
    "get_my_profile": ("_person", "get_my_profile"),
    "get_page_text": ("_content", "get_page_text"),
    "get_saved_jobs": ("_jobs", "get_saved_jobs"),
    "get_sidebar_profiles": ("_person", "get_sidebar_profiles"),
    "scrape_company": ("_company", "scrape_company"),
    "scrape_job": ("_jobs", "scrape_job"),
    "scrape_person": ("_person", "scrape_person"),
    "search_companies": ("_company", "search_companies"),
    "search_conversations": ("_conversations", "search_conversations"),
    "search_jobs": ("_jobs", "search_jobs"),
    "search_people": ("_person", "search_people"),
    "search_posts": ("_posts", "search_posts"),
    "send_message": ("_message_sender", "send_message"),
}

DELEGATE_CALLS = {
    "click_button_by_text": "self._content.click_button_by_text(text, scope=scope, timeout=timeout)",
    "connect_with_person": "self._connection.connect_with_person(username, note=note)",
    "extract_feed": "self._feed.extract_feed(num_posts)",
    "extract_page": "self._capture.extract_page(url, section_name, max_scrolls)",
    "get_company_employees": "self._company.get_company_employees(company_name, keywords)",
    "get_conversation": "self._conversations.get_conversation(linkedin_username, thread_id, index)",
    "get_inbox": "self._conversations.get_inbox(limit)",
    "get_my_profile": "self._person.get_my_profile(sections, callbacks, max_scrolls)",
    "get_page_text": "self._content.get_page_text()",
    "get_saved_jobs": "self._jobs.get_saved_jobs(max_pages)",
    "get_sidebar_profiles": "self._person.get_sidebar_profiles(username)",
    "scrape_company": "self._company.scrape_company(company_name, requested, callbacks)",
    "scrape_job": "self._jobs.scrape_job(job_id)",
    "scrape_person": "self._person.scrape_person(username, requested, callbacks, max_scrolls, main_profile_already_loaded=main_profile_already_loaded, allow_self_alias=allow_self_alias)",
    "search_companies": "self._company.search_companies(keywords)",
    "search_conversations": "self._conversations.search_conversations(keywords, limit)",
    "search_jobs": "self._jobs.search_jobs(keywords, location, max_pages, date_posted, job_type, experience_level, work_type, easy_apply, sort_by, tool_timeout)",
    "search_people": "self._person.search_people(keywords, location=location, network=network, current_company=current_company)",
    "search_posts": "self._posts.search_posts(keywords, date_posted=date_posted, max_pages=max_pages)",
    "send_message": "self._message_sender.send_message(linkedin_username, message, confirm_send=confirm_send, profile_urn=profile_urn)",
}

FACADE_STATE = {
    "_capture",
    "_company",
    "_connection",
    "_content",
    "_conversations",
    "_feed",
    "_jobs",
    "_message_sender",
    "_person",
    "_posts",
}

PERMANENT_ALIASES = {
    "ExtractedSection": contracts.ExtractedSection,
    "FilterValidationError": contracts.FilterValidationError,
    "rate_limited_section_error": contracts.rate_limited_section_error,
    "strip_linkedin_noise": text.strip_linkedin_noise,
    "strip_conversation_chrome": text.strip_conversation_chrome,
}

FORBIDDEN_LAYER_PREFIXES = (
    "linkedin_mcp_server.browser_",
    "linkedin_mcp_server.daemon",
    "linkedin_mcp_server.drivers",
    "linkedin_mcp_server.hidden_target",
    "linkedin_mcp_server.private_state",
    "linkedin_mcp_server.process_",
    "linkedin_mcp_server.profile_claim",
    "linkedin_mcp_server.profile_lease",
    "linkedin_mcp_server.server_role",
    "linkedin_mcp_server.session_state",
)


def _facade(source: str) -> ast.ClassDef:
    tree = ast.parse(source)
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor"
    )


def _method(facade: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _assert_facade_shape(source: str) -> None:
    facade = _facade(source)
    assert not facade.bases
    assert not facade.keywords
    methods = {
        node.name: node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert set(methods) == {*PUBLIC_SIGNATURES, "__init__"}
    assert all(
        isinstance(methods[name], ast.AsyncFunctionDef) for name in PUBLIC_SIGNATURES
    )
    assert isinstance(methods["__init__"], ast.FunctionDef)
    assert "__getattr__" not in methods


def _assert_delegate_bodies(source: str) -> None:
    facade = _facade(source)
    for name, (owner, target) in DELEGATES.items():
        method = _method(facade, name)
        statements = method.body[1:] if ast.get_docstring(method) else method.body
        assert len(statements) == 1
        statement = statements[0]
        assert isinstance(statement, ast.Return)
        assert isinstance(statement.value, ast.Await)
        call = statement.value.value
        assert isinstance(call, ast.Call)
        assert ast.unparse(call) == DELEGATE_CALLS[name]
        assert isinstance(call.func, ast.Attribute) and call.func.attr == target
        receiver = call.func.value
        assert isinstance(receiver, ast.Attribute) and receiver.attr == owner
        assert isinstance(receiver.value, ast.Name) and receiver.value.id == "self"
        assert not any(
            isinstance(node, (ast.Try, ast.Raise)) for node in ast.walk(method)
        )

        parameters = {
            argument.arg
            for argument in [
                *method.args.posonlyargs,
                *method.args.args[1:],
                *method.args.kwonlyargs,
            ]
        }
        forwarded = {
            node.id
            for argument in [*call.args, *(keyword.value for keyword in call.keywords)]
            for node in ast.walk(argument)
            if isinstance(node, ast.Name)
        }
        assert forwarded == parameters


def _assert_state(source: str) -> None:
    init = _method(_facade(source), "__init__")
    assigned = {
        node.attr
        for node in ast.walk(init)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
    }
    assert assigned == FACADE_STATE


def _imports(path: Path, source: str) -> set[str]:
    package_parts = list(path.parent.parts)
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent_parts = package_parts[: len(package_parts) - node.level + 1]
                imported_from = ".".join(
                    [*parent_parts, *(node.module or "").split(".")]
                ).rstrip(".")
            else:
                imported_from = node.module or ""
            if imported_from:
                modules.add(imported_from)
            modules.update(
                f"{imported_from}.{alias.name}" if imported_from else alias.name
                for alias in node.names
                if alias.name != "*"
            )
    return modules


def _assert_scraping_dependencies(sources: dict[Path, str]) -> None:
    graph: dict[str, set[str]] = defaultdict(set)
    for path, source in sources.items():
        module = f"linkedin_mcp_server.scraping.{path.stem}"
        for imported in _imports(path, source):
            assert not imported.startswith(FORBIDDEN_LAYER_PREFIXES)
            if path.name not in {"__init__.py", "extractor.py"}:
                assert imported not in {
                    "linkedin_mcp_server.scraping",
                    "linkedin_mcp_server.scraping.extractor",
                }
            if imported.startswith("linkedin_mcp_server.scraping."):
                graph[module].add(imported)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str) -> None:
        assert module not in visiting, f"scraping import cycle through {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in list(graph):
        visit(module)


def _assert_no_private_facade_accesses(sources: dict[Path, str]) -> None:
    for path, source in sources.items():
        tree = ast.parse(source, filename=str(path))
        facade_names = {"extractor"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "LinkedInExtractor"
                ):
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    facade_names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and isinstance(node.value, ast.Name)
                and node.value.id in facade_names
            ):
                raise AssertionError(
                    f"{path}:{node.lineno}: private facade access {node.attr}"
                )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"object", "setattr"}
                and len(node.args) > 1
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in facade_names | {"LinkedInExtractor"}
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value.startswith("_")
            ):
                raise AssertionError(
                    f"{path}:{node.lineno}: private facade patch {node.args[1].value}"
                )


def _assert_no_obsolete_extractor_seams(sources: dict[Path, str]) -> None:
    for path, source in sources.items():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "linkedin_mcp_server.scraping.extractor"
            ):
                names = {alias.name for alias in node.names}
                assert path == Path("tests/scraping/test_facade_contracts.py")
                assert names <= set(PERMANENT_ALIASES)
            if isinstance(node, ast.Call) and node.args:
                function = node.func
                is_patch = isinstance(function, ast.Name) and function.id == "patch"
                is_setattr = (
                    isinstance(function, ast.Attribute) and function.attr == "setattr"
                )
                target = node.args[0]
                if (
                    (is_patch or is_setattr)
                    and isinstance(target, ast.Constant)
                    and isinstance(target.value, str)
                ):
                    assert not target.value.startswith(
                        "linkedin_mcp_server.scraping.extractor."
                    )


def _sources(root: Path) -> dict[Path, str]:
    return {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
    }


def _assert_runtime_signatures(facade: Any) -> None:
    assert {
        name: str(inspect.signature(getattr(facade, name)))
        for name in PUBLIC_SIGNATURES
    } == PUBLIC_SIGNATURES


def test_public_surface_signatures_and_coroutine_status_are_exact():
    source = EXTRACTOR.read_text(encoding="utf-8")
    _assert_facade_shape(source)
    _assert_runtime_signatures(LinkedInExtractor)


def test_facade_state_and_delegate_bodies_are_explicit():
    source = EXTRACTOR.read_text(encoding="utf-8")
    _assert_state(source)
    _assert_delegate_bodies(source)


def test_package_export_and_permanent_aliases_keep_identity():
    module = sys.modules["linkedin_mcp_server.scraping.extractor"]
    assert LinkedInExtractor is module.LinkedInExtractor
    for name, owner in PERMANENT_ALIASES.items():
        assert getattr(module, name) is owner


def test_scraping_dependencies_are_one_way_acyclic_and_layered():
    _assert_scraping_dependencies(_sources(SCRAPING))


def test_imports_canonicalize_absolute_and_relative_from_imports():
    path = Path("linkedin_mcp_server/scraping/nested/module.py")
    source = """
from linkedin_mcp_server import process_protocol
from .. import capture
from ..capture import SectionCapture
from ...config import settings
"""

    imports = _imports(path, source)

    assert "linkedin_mcp_server.process_protocol" in imports
    assert "linkedin_mcp_server.scraping.capture" in imports
    assert "linkedin_mcp_server.config" in imports


def test_obsolete_extractor_seams_and_private_accesses_are_absent():
    sources = {**_sources(PACKAGE), **_sources(ROOT / "tests")}
    _assert_no_obsolete_extractor_seams(sources)
    _assert_no_private_facade_accesses(sources)


@pytest.mark.parametrize(
    "mutate,guard",
    [
        (
            lambda value: value.replace(
                "async def get_page_text", "async def page_text", 1
            ),
            _assert_facade_shape,
        ),
        (
            lambda value: value.replace(
                "async def get_page_text", "def get_page_text", 1
            ),
            _assert_facade_shape,
        ),
        (
            lambda value: value.replace(
                "self._content = content",
                "self._content = content\n        self._page = page",
                1,
            ),
            _assert_state,
        ),
        (
            lambda value: value.replace(
                "self._jobs.search_jobs", "self._posts.search_jobs", 1
            ),
            _assert_delegate_bodies,
        ),
        (
            lambda value: value.replace(
                "            keywords,\n            location,",
                "            location,\n            keywords,",
                1,
            ),
            _assert_delegate_bodies,
        ),
    ],
)
def test_facade_ast_guards_reject_representative_mutations(mutate, guard):
    with pytest.raises(AssertionError):
        guard(mutate(EXTRACTOR.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "path,addition",
    [
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from . import capture\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from .capture import SectionCapture\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from linkedin_mcp_server import process_protocol\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from .. import process_protocol\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from linkedin_mcp_server.daemon_owner import owner\n",
        ),
        (
            Path("linkedin_mcp_server/scraping/content.py"),
            "from linkedin_mcp_server.scraping import LinkedInExtractor\n",
        ),
    ],
)
def test_dependency_guards_reject_cycles_layers_and_reverse_imports(path, addition):
    sources = _sources(SCRAPING)
    sources[path] = addition + sources[path]
    with pytest.raises(AssertionError):
        _assert_scraping_dependencies(sources)


@pytest.mark.parametrize(
    "path,addition",
    [
        (
            Path("linkedin_mcp_server/tools/feed.py"),
            "from linkedin_mcp_server.scraping.extractor import LinkedInExtractor\n",
        ),
        (
            Path("tests/test_tools.py"),
            "from unittest.mock import patch\npatch('linkedin_mcp_server.scraping.extractor.logger')\n",
        ),
    ],
)
def test_obsolete_extractor_seam_guard_rejects_mutations(path, addition):
    sources = {path: addition}
    with pytest.raises(AssertionError):
        _assert_no_obsolete_extractor_seams(sources)


@pytest.mark.parametrize(
    "addition",
    [
        "extractor = LinkedInExtractor(page)\nextractor._content\n",
        "patch.object(LinkedInExtractor, '_content', replacement)\n",
    ],
)
def test_private_facade_access_guard_rejects_mutations(addition):
    with pytest.raises(AssertionError):
        _assert_no_private_facade_accesses({Path("tests/synthetic.py"): addition})


def test_signature_guard_rejects_a_mutated_default():
    class MutatedFacade:
        pass

    for name in PUBLIC_SIGNATURES:
        setattr(MutatedFacade, name, getattr(LinkedInExtractor, name))

    async def get_page_text(self, unexpected: bool = False) -> str:
        return ""

    setattr(MutatedFacade, "get_page_text", get_page_text)
    with pytest.raises(AssertionError):
        _assert_runtime_signatures(MutatedFacade)
