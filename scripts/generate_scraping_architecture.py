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


def _resolve_import(module: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    package = module.split(".")
    if module != PACKAGE:
        package = package[:-1]
    keep = len(package) - node.level + 1
    if keep < 0:
        return None
    resolved = package[:keep]
    if node.module:
        resolved.extend(node.module.split("."))
    return ".".join(resolved)


def _imports(module: str, tree: ast.Module) -> tuple[str, ...]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import(module, node)
            if resolved is not None:
                imports.add(resolved)
    imports.discard(module)
    return tuple(sorted(imports))


def _public_owners(tree: ast.Module) -> tuple[str, ...]:
    owners: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            owners.append(node.name)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and not node.name.startswith("_"):
            owners.append(f"{node.name}()")
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


def _source_classification(tree: ast.Module) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "patchright.async_api":
            if any(alias.name == "Page" for alias in node.names):
                return "page-owning"
        if isinstance(node, ast.Attribute):
            parts = _attribute_parts(node)
            if parts[-1] == "page" or "_page" in parts:
                return "page-owning"
    return "browser-free"


def inspect_modules(scraping: Path = SCRAPING) -> tuple[ModuleInfo, ...]:
    modules: list[ModuleInfo] = []
    for path in sorted(scraping.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path.name)
        module = _module_name(path, scraping)
        modules.append(
            ModuleInfo(
                name=module,
                path=f"linkedin_mcp_server/scraping/{path.name}",
                imports=_imports(module, tree),
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
            if imported.startswith(FORBIDDEN_LAYER_PREFIXES):
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
