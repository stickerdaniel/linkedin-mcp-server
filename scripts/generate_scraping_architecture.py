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

    def _page_value(self, node: ast.expr | None) -> bool:
        return node is not None and (self._tracked(node) or self._known_source(node))

    def _value_state(self, node: ast.expr | None) -> _AbstractState:
        if node is not None and self._page_value(node):
            return frozenset({_PAGE})
        if node is not None and self._source_root(node):
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
        keys = set().union(*(state.keys() for state in alternatives))
        return {
            key: frozenset().union(
                *(state.get(key, frozenset({_UNBOUND})) for state in alternatives)
            )
            for key in keys
        }

    def _run_block(
        self, statements: Iterable[ast.stmt], initial: _ScopeState
    ) -> _ScopeState:
        self.scopes[-1] = dict(initial)
        for statement in statements:
            self.visit(statement)
        return dict(self.scope)

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
            (name,): frozenset({_NON_PAGE}) for name in _bound_names(node.body)
        }
        self.scopes.append(local)
        self._visit_arguments(node.args)
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()

    def _loop_has_break(self, statements: Iterable[ast.stmt]) -> bool:
        class BreakVisitor(ast.NodeVisitor):
            found = False

            def visit_Break(self, node: ast.Break) -> None:
                self.found = True

            def visit_For(self, node: ast.For) -> None:
                return

            def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
                return

            def visit_While(self, node: ast.While) -> None:
                return

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                return

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                return

            def visit_Lambda(self, node: ast.Lambda) -> None:
                return

        visitor = BreakVisitor()
        for statement in statements:
            visitor.visit(statement)
        return visitor.found

    def _visit_loop(
        self,
        body: list[ast.stmt],
        orelse: list[ast.stmt],
        initial: _ScopeState,
        target: ast.expr | None = None,
    ) -> None:
        entry = dict(initial)
        iteration_exit = dict(entry)
        while True:
            iteration_entry = self._merge((entry, iteration_exit))
            self.scopes[-1] = iteration_entry
            if target is not None:
                self._bind(target, frozenset({_NON_PAGE}))
            body_exit = self._run_block(body, self.scope)
            widened = self._merge((iteration_exit, body_exit))
            if widened == iteration_exit:
                break
            iteration_exit = widened

        normal_exit = self._run_block(orelse, self._merge((entry, iteration_exit)))
        if self._loop_has_break(body):
            self.scopes[-1] = self._merge((normal_exit, iteration_exit))
        else:
            self.scopes[-1] = normal_exit

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
        self._bind(node.target, state)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        self._bind(node.target, frozenset({_NON_PAGE}))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target, self._value_state(node.value))

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        initial = dict(self.scope)
        body = self._run_block(node.body, initial)
        orelse = self._run_block(node.orelse, initial)
        self.scopes[-1] = self._merge((body, orelse))

    def _visit_try(self, node: ast.Try | ast.TryStar) -> None:
        initial = dict(self.scope)
        prefixes = [initial]
        self.scopes[-1] = dict(initial)
        for statement in node.body:
            self.visit(statement)
            prefixes.append(dict(self.scope))
        body_exit = dict(self.scope)
        normal_exit = self._run_block(node.orelse, body_exit)
        handler_entry = self._merge(prefixes)
        handler_exits: list[_ScopeState] = []
        for handler in node.handlers:
            self.scopes[-1] = dict(handler_entry)
            if handler.type is not None:
                self.visit(handler.type)
            if handler.name is not None:
                self.scope[(handler.name,)] = frozenset({_NON_PAGE})
            handler_exits.append(self._run_block(handler.body, self.scope))
        continuing = self._merge((normal_exit, *handler_exits))
        if not node.finalbody:
            self.scopes[-1] = continuing
            return

        final_entry = self._merge((handler_entry, continuing))
        self._run_block(node.finalbody, final_entry)
        self.scopes[-1] = self._run_block(node.finalbody, continuing)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._visit_try(node)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._visit_loop(node.body, node.orelse, dict(self.scope), node.target)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._visit_loop(node.body, node.orelse, dict(self.scope), node.target)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_loop(node.body, node.orelse, dict(self.scope))

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        initial = dict(self.scope)
        exits: list[_ScopeState] = []
        exhaustive = False
        for case in node.cases:
            self.scopes[-1] = dict(initial)
            for pattern_node in ast.walk(case.pattern):
                name = getattr(pattern_node, "name", None)
                if isinstance(name, str):
                    self.scope[(name,)] = frozenset({_NON_PAGE})
            if case.guard is not None:
                self.visit(case.guard)
            exits.append(self._run_block(case.body, self.scope))
            if (
                case.guard is None
                and isinstance(case.pattern, ast.MatchAs)
                and case.pattern.pattern is None
            ):
                exhaustive = True
        if not exhaustive:
            exits.append(initial)
        self.scopes[-1] = self._merge(exits)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._known_source(node) or self._tracked(node.value):
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


def _source_classification(tree: ast.Module) -> str:
    page_types, imports_page = _page_types(tree)
    if imports_page:
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
