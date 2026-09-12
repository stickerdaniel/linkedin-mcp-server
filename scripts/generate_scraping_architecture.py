#!/usr/bin/env python3
"""Generate the scraping architecture reference from Python source."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Iterable

import argparse
import ast
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRAPING = ROOT / "linkedin_mcp_server" / "scraping"
OUTPUT = ROOT / "docs" / "scraping-architecture.md"
PACKAGE = "linkedin_mcp_server.scraping"
FORBIDDEN_LAYER_MODULES = frozenset(
    {"linkedin_mcp_server.core", "linkedin_mcp_server.core.browser"}
)
FORBIDDEN_LAYER_PREFIXES = (
    "linkedin_mcp_server.browser_",
    "linkedin_mcp_server.core.browser.",
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


@dataclass(frozen=True, slots=True)
class ModuleInfo:
    name: str
    path: str
    imports: tuple[str, ...]
    owners: tuple[str, ...]
    source_classification: str


def _module_name(path: Path, scraping: Path) -> str:
    relative = path.relative_to(scraping).with_suffix("")
    parts = relative.parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((PACKAGE, *parts))


def _resolve_import(
    module: str, node: ast.ImportFrom, *, package_initializer: bool
) -> str | None:
    if node.level == 0:
        return node.module
    package = module.split(".")
    if not package_initializer:
        package = package[:-1]
    keep = len(package) - node.level + 1
    if keep < 0:
        return None
    resolved = package[:keep]
    if node.module:
        resolved.extend(node.module.split("."))
    return ".".join(resolved)


def _known_package_modules(scraping: Path) -> frozenset[str]:
    package_root = ROOT / "linkedin_mcp_server"
    paths = {*package_root.rglob("*.py"), *scraping.rglob("*.py")}
    modules = {"linkedin_mcp_server", PACKAGE}
    for path in paths:
        if path.is_relative_to(scraping):
            relative = path.relative_to(scraping).with_suffix("")
            parts = relative.parts
            prefix = PACKAGE
        else:
            relative = path.relative_to(package_root).with_suffix("")
            parts = relative.parts
            prefix = "linkedin_mcp_server"
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for length in range(1, len(parts) + 1):
            modules.add(".".join((prefix, *parts[:length])))
    return frozenset(modules)


def _imports(
    module: str,
    tree: ast.Module,
    known_modules: frozenset[str],
    *,
    package_initializer: bool,
) -> tuple[str, ...]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        resolved = _resolve_import(
            module, node, package_initializer=package_initializer
        )
        if resolved is None:
            continue
        if resolved not in known_modules:
            imports.add(resolved)
            continue
        for alias in node.names:
            candidate = f"{resolved}.{alias.name}"
            imports.add(candidate if candidate in known_modules else resolved)
    return tuple(sorted(imports))


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(target.id for target in targets if isinstance(target, ast.Name))


_TYPE_ALIAS_FACTORIES = frozenset({"Callable", "Literal"})


def _typing_alias_factories(tree: ast.Module) -> frozenset[str]:
    factories: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.module not in {
            "collections.abc",
            "typing",
        }:
            continue
        factories.update(
            alias.asname or alias.name
            for alias in node.names
            if alias.name in _TYPE_ALIAS_FACTORIES
        )
    return frozenset(factories)


def _is_assignment_type_alias(
    node: ast.Assign | ast.AnnAssign, factories: frozenset[str]
) -> bool:
    if not isinstance(node, ast.Assign):
        return False
    value = node.value
    return (
        isinstance(value, ast.Subscript)
        and isinstance(value.value, ast.Name)
        and value.value.id in factories
    )


def _public_owners(tree: ast.Module) -> tuple[str, ...]:
    owners: list[str] = []
    factories = _typing_alias_factories(tree)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            owners.append(node.name)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and not node.name.startswith("_"):
            owners.append(f"{node.name}()")
        elif isinstance(node, ast.TypeAlias):
            if isinstance(node.name, ast.Name) and not node.name.id.startswith("_"):
                owners.append(node.name.id)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            is_type_alias = _is_assignment_type_alias(node, factories)
            owners.extend(
                name
                for name in _assignment_names(node)
                if not name.startswith("_") and (name.isupper() or is_type_alias)
            )
    return tuple(sorted(owners))


def _attribute_parts(node: ast.Attribute) -> tuple[str, ...]:
    parts = [node.attr]
    value = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return tuple(reversed(parts))


def _page_annotation(annotation: ast.expr | None, page_types: frozenset[str]) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    return any(
        isinstance(node, (ast.Name, ast.Attribute)) and ast.unparse(node) in page_types
        for node in ast.walk(annotation)
    )


def _bound_names(nodes: list[ast.stmt]) -> frozenset[str]:
    names: set[str] = set()

    class BindingVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            names.add(node.name)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)

    visitor = BindingVisitor()
    for node in nodes:
        visitor.visit(node)
    return frozenset(names)


_PAGE = "page"
_SOURCE = "source"
_NON_PAGE = "non-page"
_UNBOUND = "unbound"
_AbstractState = frozenset[str]
_ScopeState = dict[tuple[str, ...], _AbstractState]


@dataclass(slots=True)
class _Flow:
    normal: _ScopeState | None
    breaks: list[_ScopeState]
    continues: list[_ScopeState]
    returns: list[_ScopeState]
    exceptions: list[_ScopeState]
    prefixes: list[_ScopeState]


class _PageUseVisitor(ast.NodeVisitor):
    def __init__(self, page_types: frozenset[str]) -> None:
        self.page_types = page_types
        self.scopes: list[_ScopeState] = [{}]
        self.found = False

    @property
    def scope(self) -> _ScopeState:
        return self.scopes[-1]

    def _key(self, node: ast.expr) -> tuple[str, ...] | None:
        if isinstance(node, ast.Name):
            return (node.id,)
        if isinstance(node, ast.Attribute):
            parts = _attribute_parts(node)
            return parts if parts else None
        return None

    def _state(self, key: tuple[str, ...]) -> _AbstractState:
        resolved: set[str] = set()
        for scope in reversed(self.scopes):
            state = scope.get(key)
            if state is None:
                continue
            resolved.update(state - {_UNBOUND})
            if _UNBOUND not in state:
                return frozenset(resolved)
        resolved.add(_UNBOUND)
        return frozenset(resolved)

    def _tracked(self, node: ast.expr) -> bool:
        key = self._key(node)
        return key is not None and _PAGE in self._state(key)

    def _source_root(self, node: ast.expr) -> bool:
        key = self._key(node)
        if key is None:
            return False
        state = self._state(key)
        return _SOURCE in state or (
            key[-1] in {"session", "_session"} and _UNBOUND in state
        )

    def _known_source(self, node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr in {"page", "_page"}
            and self._source_root(node.value)
        )

    def _value_state(self, node: ast.expr | None) -> _AbstractState:
        if node is None:
            return frozenset({_NON_PAGE})
        if self._known_source(node):
            return frozenset({_PAGE})
        key = self._key(node)
        if key is not None:
            state = self._state(key)
            return frozenset(state - {_UNBOUND} or {_NON_PAGE})
        if self._source_root(node):
            return frozenset({_SOURCE})
        return frozenset({_NON_PAGE})

    def _bind(self, target: ast.expr, state: _AbstractState) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element, frozenset({_NON_PAGE}))
            return
        key = self._key(target)
        if key is not None:
            self.scope[key] = state

    def _merge(self, states: Iterable[_ScopeState]) -> _ScopeState:
        alternatives = tuple(states)
        if not alternatives:
            return {}
        keys = set().union(*(state.keys() for state in alternatives))
        return {
            key: frozenset().union(
                *(state.get(key, frozenset({_UNBOUND})) for state in alternatives)
            )
            for key in keys
        }

    def _merge_optional(
        self, states: Iterable[_ScopeState | None]
    ) -> _ScopeState | None:
        present = tuple(state for state in states if state is not None)
        return self._merge(present) if present else None

    def _flow(self, normal: _ScopeState | None) -> _Flow:
        return _Flow(normal, [], [], [], [], [])

    def _run_block(self, statements: Iterable[ast.stmt], initial: _ScopeState) -> _Flow:
        flow = self._flow(dict(initial))
        deferred: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for statement in statements:
            if flow.normal is None:
                break
            flow.prefixes.append(dict(flow.normal))
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deferred.append(statement)
                continue
            statement_flow = self._run_statement(statement, flow.normal)
            flow.normal = statement_flow.normal
            flow.breaks.extend(statement_flow.breaks)
            flow.continues.extend(statement_flow.continues)
            flow.returns.extend(statement_flow.returns)
            flow.exceptions.extend(statement_flow.exceptions)
            flow.prefixes.extend(statement_flow.prefixes)

        closure_states = [
            state
            for state in [flow.normal, *flow.breaks, *flow.continues, *flow.returns]
            if state is not None
        ]
        if deferred and closure_states:
            saved = dict(self.scope)
            self.scopes[-1] = self._merge(closure_states)
            for definition in deferred:
                self._visit_function(definition)
            self.scopes[-1] = saved
        return flow

    def _run_statement(self, node: ast.stmt, initial: _ScopeState) -> _Flow:
        self.scopes[-1] = dict(initial)
        if isinstance(node, ast.If):
            return self._run_if(node)
        if isinstance(node, (ast.Try, ast.TryStar)):
            return self._run_try(node)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            return self._run_loop(node)
        if isinstance(node, ast.Match):
            return self._run_match(node)
        if isinstance(node, ast.Break):
            return _Flow(None, [dict(self.scope)], [], [], [], [])
        if isinstance(node, ast.Continue):
            return _Flow(None, [], [dict(self.scope)], [], [], [])
        if isinstance(node, ast.Return):
            if node.value is not None:
                self.visit(node.value)
            return _Flow(None, [], [], [dict(self.scope)], [], [])
        if isinstance(node, ast.Raise):
            if node.exc is not None:
                self.visit(node.exc)
            if node.cause is not None:
                self.visit(node.cause)
            return _Flow(None, [], [], [], [dict(self.scope)], [])
        self.visit(node)
        return self._flow(dict(self.scope))

    def _visit_arguments(self, arguments: ast.arguments) -> None:
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]:
            if _page_annotation(argument.annotation, self.page_types):
                state = frozenset({_PAGE})
            elif argument.arg in {"session", "_session"}:
                state = frozenset({_SOURCE})
            else:
                state = frozenset({_NON_PAGE})
            self.scope[(argument.arg,)] = state
        if arguments.vararg is not None:
            self.scope[(arguments.vararg.arg,)] = frozenset({_NON_PAGE})
        if arguments.kwarg is not None:
            self.scope[(arguments.kwarg.arg,)] = frozenset({_NON_PAGE})

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        local: _ScopeState = {
            (name,): frozenset({_UNBOUND}) for name in _bound_names(node.body)
        }
        self.scopes.append(local)
        self._visit_arguments(node.args)
        self._run_block(node.body, self.scope)
        self.scopes.pop()

    def _combine(self, flows: Iterable[_Flow]) -> _Flow:
        alternatives = tuple(flows)
        return _Flow(
            self._merge_optional(flow.normal for flow in alternatives),
            [state for flow in alternatives for state in flow.breaks],
            [state for flow in alternatives for state in flow.continues],
            [state for flow in alternatives for state in flow.returns],
            [state for flow in alternatives for state in flow.exceptions],
            [state for flow in alternatives for state in flow.prefixes],
        )

    def _run_if(self, node: ast.If) -> _Flow:
        self.visit(node.test)
        initial = dict(self.scope)
        return self._combine(
            (
                self._run_block(node.body, initial),
                self._run_block(node.orelse, initial),
            )
        )

    def _through_finally(
        self, states: Iterable[_ScopeState], finalbody: list[ast.stmt], transfer: str
    ) -> _Flow:
        alternatives = tuple(states)
        if not alternatives:
            return self._flow(None)
        final = self._run_block(finalbody, self._merge(alternatives))
        if final.normal is not None:
            getattr(final, transfer).append(final.normal)
            final.normal = None
        return final

    def _run_try(self, node: ast.Try | ast.TryStar) -> _Flow:
        initial = dict(self.scope)
        body = self._run_block(node.body, initial)
        orelse = (
            self._run_block(node.orelse, body.normal)
            if body.normal is not None
            else self._flow(None)
        )
        handler_entry = self._merge([initial, *body.prefixes, *body.exceptions])
        handlers: list[_Flow] = []
        for handler in node.handlers:
            self.scopes[-1] = dict(handler_entry)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self.scope[(handler.name,)] = frozenset({_NON_PAGE})
            handlers.append(self._run_block(handler.body, self.scope))

        handler_flow = self._combine(handlers)
        continuing = self._merge_optional((orelse.normal, handler_flow.normal))
        combined = _Flow(
            continuing,
            [*body.breaks, *orelse.breaks, *handler_flow.breaks],
            [*body.continues, *orelse.continues, *handler_flow.continues],
            [*body.returns, *orelse.returns, *handler_flow.returns],
            [
                *body.exceptions,
                *orelse.exceptions,
                *handler_flow.exceptions,
                *body.prefixes,
                *orelse.prefixes,
                *handler_flow.prefixes,
            ],
            [*body.prefixes, *orelse.prefixes, *handler_flow.prefixes],
        )
        if not node.finalbody:
            return combined

        flows: list[_Flow] = []
        if combined.normal is not None:
            flows.append(self._run_block(node.finalbody, combined.normal))
        flows.extend(
            (
                self._through_finally(combined.breaks, node.finalbody, "breaks"),
                self._through_finally(combined.continues, node.finalbody, "continues"),
                self._through_finally(combined.returns, node.finalbody, "returns"),
                self._through_finally(
                    combined.exceptions, node.finalbody, "exceptions"
                ),
            )
        )
        return self._combine(flows)

    def _run_loop(self, node: ast.For | ast.AsyncFor | ast.While) -> _Flow:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self.visit(node.iter)
            target = node.target
        else:
            self.visit(node.test)
            target = None
        entry = dict(self.scope)
        header = dict(entry)
        breaks: list[_ScopeState] = []
        returns: list[_ScopeState] = []
        exceptions: list[_ScopeState] = []
        prefixes: list[_ScopeState] = []
        while True:
            self.scopes[-1] = dict(header)
            if target is not None:
                self._bind(target, frozenset({_NON_PAGE}))
            body = self._run_block(node.body, self.scope)
            back_edges = [
                state for state in [body.normal, *body.continues] if state is not None
            ]
            widened = self._merge((entry, *back_edges))
            breaks.extend(body.breaks)
            returns.extend(body.returns)
            exceptions.extend(body.exceptions)
            prefixes.extend(body.prefixes)
            if widened == header:
                break
            header = widened

        orelse = self._run_block(node.orelse, header)
        normal = self._merge_optional((orelse.normal, *breaks))
        return _Flow(
            normal,
            orelse.breaks,
            orelse.continues,
            [*returns, *orelse.returns],
            [*exceptions, *orelse.exceptions],
            [*prefixes, *orelse.prefixes],
        )

    def _capture_pattern(self, pattern: ast.pattern, state: _AbstractState) -> None:
        for pattern_node in ast.walk(pattern):
            name = getattr(pattern_node, "name", None)
            if isinstance(name, str):
                self.scope[(name,)] = state
            rest = getattr(pattern_node, "rest", None)
            if isinstance(rest, str):
                self.scope[(rest,)] = state

    def _irrefutable_pattern(self, pattern: ast.pattern) -> bool:
        if isinstance(pattern, ast.MatchAs):
            return pattern.pattern is None or self._irrefutable_pattern(pattern.pattern)
        if isinstance(pattern, ast.MatchOr):
            return any(self._irrefutable_pattern(item) for item in pattern.patterns)
        return False

    def _run_match(self, node: ast.Match) -> _Flow:
        self.visit(node.subject)
        initial = dict(self.scope)
        subject_state = self._value_state(node.subject)
        flows: list[_Flow] = []
        exhaustive = False
        for case in node.cases:
            self.scopes[-1] = dict(initial)
            self._capture_pattern(case.pattern, subject_state)
            if case.guard is not None:
                self.visit(case.guard)
            flows.append(self._run_block(case.body, self.scope))
            if case.guard is None and self._irrefutable_pattern(case.pattern):
                exhaustive = True
        if not exhaustive:
            flows.append(self._flow(initial))
        return self._combine(flows)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.scopes.append({})
        self._visit_arguments(node.args)
        self.visit(node.body)
        self.scopes.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        state = self._value_state(node.value)
        for target in node.targets:
            if isinstance(target, ast.Attribute) and _PAGE in state:
                self.found = True
            self._bind(target, state)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        state = (
            frozenset({_PAGE})
            if _page_annotation(node.annotation, self.page_types)
            else self._value_state(node.value)
        )
        if isinstance(node.target, ast.Attribute) and _PAGE in state:
            self.found = True
        self._bind(node.target, state)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind(node.target, frozenset({_NON_PAGE}))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target, self._value_state(node.value))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._known_source(node.value) or self._tracked(node.value):
            self.found = True
        self.generic_visit(node)


def _page_types(tree: ast.Module) -> tuple[frozenset[str], bool]:
    types = {"Page"}
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "patchright.async_api":
            for alias in node.names:
                if alias.name == "Page":
                    types.add(alias.asname or alias.name)
                    imported = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "patchright.async_api":
                    types.add(f"{alias.asname or alias.name}.Page")
        elif isinstance(node, ast.ImportFrom) and node.module == "patchright":
            for alias in node.names:
                if alias.name == "async_api":
                    types.add(f"{alias.asname or alias.name}.Page")
    return frozenset(types), imported


def _has_page_annotation(tree: ast.Module, page_types: frozenset[str]) -> bool:
    for node in ast.walk(tree):
        annotation = None
        if isinstance(node, ast.arg):
            annotation = node.annotation
        elif isinstance(node, ast.AnnAssign):
            annotation = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation = node.returns
        if _page_annotation(annotation, page_types):
            return True
    return False


def _source_classification(tree: ast.Module) -> str:
    page_types, imports_page = _page_types(tree)
    if imports_page or _has_page_annotation(tree, page_types):
        return "page-owning"
    visitor = _PageUseVisitor(page_types)
    visitor.visit(tree)
    return "page-owning" if visitor.found else "browser-free"


def inspect_modules(scraping: Path = SCRAPING) -> tuple[ModuleInfo, ...]:
    modules: list[ModuleInfo] = []
    known_modules = _known_package_modules(scraping)
    for path in sorted(scraping.rglob("*.py")):
        relative_path = path.relative_to(scraping)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path.as_posix())
        module = _module_name(path, scraping)
        modules.append(
            ModuleInfo(
                name=module,
                path=(Path("linkedin_mcp_server/scraping") / relative_path).as_posix(),
                imports=_imports(
                    module,
                    tree,
                    known_modules,
                    package_initializer=path.name == "__init__.py",
                ),
                owners=_public_owners(tree),
                source_classification=_source_classification(tree),
            )
        )
    return tuple(modules)


def _facade(scraping: Path) -> ast.ClassDef:
    path = scraping / "extractor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LinkedInExtractor":
            return node
    raise ValueError("LinkedInExtractor not found in scraping/extractor.py")


def facade_coroutines(scraping: Path = SCRAPING) -> tuple[str, ...]:
    return tuple(
        sorted(
            node.name
            for node in _facade(scraping).body
            if isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
        )
    )


def construction_state(scraping: Path = SCRAPING) -> tuple[str, ...]:
    facade = _facade(scraping)
    init = next(
        (
            node
            for node in facade.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ),
        None,
    )
    if init is None:
        raise ValueError("LinkedInExtractor.__init__ not found")
    return tuple(
        sorted(
            {
                node.attr
                for node in ast.walk(init)
                if isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            }
        )
    )


def dependency_violations(modules: Iterable[ModuleInfo]) -> tuple[str, ...]:
    module_list = tuple(modules)
    names = {module.name for module in module_list}
    graph = {
        module.name: tuple(
            dependency for dependency in module.imports if dependency in names
        )
        for module in module_list
    }
    violations: set[str] = set()

    for module in module_list:
        if module.name not in {PACKAGE, f"{PACKAGE}.extractor"}:
            for dependency in module.imports:
                if dependency in {PACKAGE, f"{PACKAGE}.extractor"}:
                    violations.add(
                        f"reverse facade import: `{module.name}` -> `{dependency}`"
                    )
        for imported in module.imports:
            if imported in FORBIDDEN_LAYER_MODULES or imported.startswith(
                FORBIDDEN_LAYER_PREFIXES
            ):
                violations.add(
                    f"forbidden layer import: `{module.name}` -> `{imported}`"
                )

    visiting: list[str] = []
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            cycle = visiting[visiting.index(module) :] + [module]
            violations.add(
                "import cycle: " + " -> ".join(f"`{item}`" for item in cycle)
            )
            return
        if module in visited:
            return
        visiting.append(module)
        for dependency in graph.get(module, ()):
            visit(dependency)
        visiting.pop()
        visited.add(module)

    for module in sorted(graph):
        visit(module)
    return tuple(sorted(violations))


def _short(module: str) -> str:
    return module.removeprefix(f"{PACKAGE}.") if module != PACKAGE else "__init__"


def render(scraping: Path = SCRAPING) -> str:
    modules = inspect_modules(scraping)
    methods = facade_coroutines(scraping)
    state = construction_state(scraping)
    violations = dependency_violations(modules)
    lines = [
        "# Scraping architecture",
        "",
        "<!-- Generated by scripts/generate_scraping_architecture.py. Do not edit. -->",
        "",
        "This reference is derived from the scraping package's Python AST. Run",
        "`uv run python scripts/generate_scraping_architecture.py` after architecture changes",
        "and use `--check` in validation paths. The check also rejects any detected",
        "dependency-direction violation, even if the document was regenerated.",
        "",
        "## Module ownership",
        "",
        "`page-owning` means the module directly imports `Page` or accesses a page",
        "handle. `browser-free` means its source does neither; it may still orchestrate",
        "a page-owning collaborator.",
        "",
        "| Module | Canonical public owners | Source classification |",
        "| --- | --- | --- |",
    ]
    for module in modules:
        owners = (
            ", ".join(f"`{owner}`" for owner in module.owners)
            or "_(no public definitions)_"
        )
        lines.append(
            f"| `{_short(module.name)}` | {owners} | `{module.source_classification}` |"
        )

    lines.extend(["", "## Internal import graph", ""])
    for module in modules:
        dependencies = [
            dependency
            for dependency in module.imports
            if dependency.startswith(PACKAGE)
        ]
        rendered = ", ".join(f"`{_short(item)}`" for item in dependencies) or "_(none)_"
        lines.append(f"- `{_short(module.name)}` -> {rendered}")

    lines.extend(
        [
            "",
            "## `LinkedInExtractor` public coroutine surface",
            "",
            *[f"- `{method}`" for method in methods],
            "",
            "## `LinkedInExtractor` construction-state allowlist",
            "",
            *[f"- `{attribute}`" for attribute in state],
            "",
            "## Dependency-direction violations",
            "",
        ]
    )
    if violations:
        lines.extend(f"- {violation}" for violation in violations)
    else:
        lines.append("None detected.")
    lines.append("")
    return "\n".join(lines)


def check(output: Path = OUTPUT, scraping: Path = SCRAPING) -> bool:
    modules = inspect_modules(scraping)
    violations = dependency_violations(modules)
    expected = render(scraping)
    actual = output.read_text(encoding="utf-8") if output.exists() else ""
    current = actual == expected
    if not current:
        sys.stderr.writelines(
            unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=output.as_posix(),
                tofile="generated scraping architecture",
            )
        )
    if violations:
        sys.stderr.write("dependency-direction violations detected:\n")
        sys.stderr.writelines(f"- {violation}\n" for violation in violations)
    return current and not violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return 0 if check() else 1
    OUTPUT.write_text(render(), encoding="utf-8")
    print(OUTPUT.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
