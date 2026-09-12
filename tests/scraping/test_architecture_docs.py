"""Contracts for the generated scraping architecture reference."""

from __future__ import annotations

import ast
import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Callable, cast

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "generate_scraping_architecture.py"
SPEC = importlib.util.spec_from_file_location("generate_scraping_architecture", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GENERATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)

render = cast(Callable[[Path], str], GENERATOR.render)
check = cast(Callable[[Path, Path], bool], GENERATOR.check)
dependency_violations = GENERATOR.dependency_violations
inspect_modules = GENERATOR.inspect_modules
ModuleInfo = GENERATOR.ModuleInfo
source_classification = GENERATOR._source_classification


def _copy_scraping(tmp_path: Path) -> Path:
    target = tmp_path / "scraping"
    shutil.copytree(ROOT / "linkedin_mcp_server" / "scraping", target)
    return target


def _replace(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def _prepend(path: Path, source: str) -> None:
    path.write_text(source + path.read_text(encoding="utf-8"), encoding="utf-8")


def test_generated_architecture_is_current_deterministic_and_checkout_neutral():
    first = render(ROOT / "linkedin_mcp_server" / "scraping")
    second = render(ROOT / "linkedin_mcp_server" / "scraping")

    assert first == second
    assert first == (ROOT / "docs" / "scraping-architecture.md").read_text(
        encoding="utf-8"
    )
    assert str(ROOT) not in first
    assert "None detected." in first


def test_import_from_normalization_keeps_modules_and_symbols_distinct(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    _prepend(
        scraping / "connection.py",
        "from . import capture\n"
        "from .capture import SectionCapture\n"
        "from ..scraping import navigation\n"
        "from linkedin_mcp_server import process_protocol\n",
    )

    connection = next(
        module
        for module in inspect_modules(scraping)
        if module.name == "linkedin_mcp_server.scraping.connection"
    )

    assert "linkedin_mcp_server.scraping.capture" in connection.imports
    assert "linkedin_mcp_server.scraping.navigation" in connection.imports
    assert "linkedin_mcp_server.process_protocol" in connection.imports
    assert (
        "linkedin_mcp_server.scraping.capture.SectionCapture" not in connection.imports
    )


def test_namespace_package_imports_resolve_to_known_leaf_modules(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    nested = scraping / "nested"
    nested.mkdir()
    (nested / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    _prepend(scraping / "connection.py", "from .nested import worker\n")

    connection = next(
        module
        for module in inspect_modules(scraping)
        if module.name == "linkedin_mcp_server.scraping.connection"
    )

    assert "linkedin_mcp_server.scraping.nested.worker" in connection.imports
    assert "linkedin_mcp_server.scraping.nested" not in connection.imports


def test_public_assignments_are_owners_without_incidental_bindings():
    modules = {
        module.name: module
        for module in inspect_modules(ROOT / "linkedin_mcp_server" / "scraping")
    }

    assert "COMPANY_SECTIONS" in modules["linkedin_mcp_server.scraping.fields"].owners
    assert "PERSON_SECTIONS" in modules["linkedin_mcp_server.scraping.fields"].owners
    assert (
        "ConnectionState" in modules["linkedin_mcp_server.scraping.connection"].owners
    )
    assert (
        "ReferenceKind" in modules["linkedin_mcp_server.scraping.link_metadata"].owners
    )
    assert "WaitUntil" in modules["linkedin_mcp_server.scraping.navigation"].owners
    assert (
        "ReadMainProfile"
        in modules["linkedin_mcp_server.scraping.connection_actions"].owners
    )
    assert (
        "ReadMessageTarget"
        in modules["linkedin_mcp_server.scraping.profile_page"].owners
    )
    assert all("logger" not in module.owners for module in modules.values())


def test_explicit_and_assignment_type_aliases_are_public_owners(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    (scraping / "aliases.py").write_text(
        "from typing import Literal\n\n"
        "type ExplicitAlias = str\n"
        "AssignmentAlias = Literal['value']\n"
        "logger = object()\n",
        encoding="utf-8",
    )

    aliases = next(
        module
        for module in inspect_modules(scraping)
        if module.name == "linkedin_mcp_server.scraping.aliases"
    )

    assert aliases.owners == ("AssignmentAlias", "ExplicitAlias")


@pytest.mark.parametrize(
    "source",
    [
        "page.evaluate('1')\n",
        "_page.evaluate('1')\n",
        "session.page\n",
        "self._session.page.evaluate('1')\n",
    ],
)
def test_direct_page_handles_are_classified_as_page_owning(source: str):
    assert source_classification(ast.parse(source)) == "page-owning"


@pytest.mark.parametrize(
    "source",
    [
        "self._profile_page.read_identity()\n",
        "browser_page.evaluate('1')\n",
        "self._job_page.capture()\n",
    ],
)
def test_page_named_collaborators_remain_browser_free(source: str):
    assert source_classification(ast.parse(source)) == "browser-free"


def test_nested_package_modules_are_inspected_with_stable_paths(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    nested = scraping / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text(
        "PUBLIC_VALUE = 1\n"
        "class NestedOwner:\n    pass\n\n"
        "def _inspect(page):\n    return page.evaluate('1')\n",
        encoding="utf-8",
    )
    (nested / "worker.py").write_text(
        "from ..fields import PERSON_SECTIONS\n", encoding="utf-8"
    )

    modules = {module.name: module for module in inspect_modules(scraping)}
    package = modules["linkedin_mcp_server.scraping.nested"]
    worker = modules["linkedin_mcp_server.scraping.nested.worker"]

    assert package.path == "linkedin_mcp_server/scraping/nested/__init__.py"
    assert package.owners == ("NestedOwner", "PUBLIC_VALUE")
    assert package.source_classification == "page-owning"
    assert worker.path == "linkedin_mcp_server/scraping/nested/worker.py"
    assert worker.imports == ("linkedin_mcp_server.scraping.fields",)


def test_nested_package_violations_cannot_bypass_checks(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    nested = scraping / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text(
        "from ..extractor import LinkedInExtractor\n"
        "from ...core import browser\n"
        "from . import worker\n",
        encoding="utf-8",
    )
    (nested / "worker.py").write_text(
        "from . import LinkedInExtractor\n", encoding="utf-8"
    )

    violations = dependency_violations(inspect_modules(scraping))

    assert (
        "reverse facade import: `linkedin_mcp_server.scraping.nested` -> "
        "`linkedin_mcp_server.scraping.extractor`"
    ) in violations
    assert (
        "forbidden layer import: `linkedin_mcp_server.scraping.nested` -> "
        "`linkedin_mcp_server.core.browser`"
    ) in violations
    assert any(
        violation.startswith("import cycle: `linkedin_mcp_server.scraping.nested`")
        for violation in violations
    )


def test_namespace_package_cycle_fails_even_after_regeneration(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    nested = scraping / "nested"
    nested.mkdir()
    (nested / "worker.py").write_text("from .. import connection\n", encoding="utf-8")
    _prepend(scraping / "connection.py", "from .nested import worker\n")
    output = tmp_path / "scraping-architecture.md"
    output.write_text(render(scraping), encoding="utf-8")

    assert not check(output, scraping)
    assert any(
        "`linkedin_mcp_server.scraping.connection` -> "
        "`linkedin_mcp_server.scraping.nested.worker` -> "
        "`linkedin_mcp_server.scraping.connection`" in violation
        for violation in dependency_violations(inspect_modules(scraping))
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda scraping: _replace(
                scraping / "content.py",
                "from linkedin_mcp_server.scraping.session import ScrapingSession\n",
                "from linkedin_mcp_server.scraping.navigation import PageNavigator\n"
                "from linkedin_mcp_server.scraping.session import ScrapingSession\n",
            ),
            id="import-edge",
        ),
        pytest.param(
            lambda scraping: _replace(
                scraping / "extractor.py",
                "    async def get_page_text(self) -> str:\n",
                "    async def renamed_page_text(self) -> str:\n",
            ),
            id="facade-method",
        ),
        pytest.param(
            lambda scraping: _replace(
                scraping / "extractor.py",
                "        self._content = content\n",
                "        self._content = content\n        self._extra = content\n",
            ),
            id="facade-state",
        ),
        pytest.param(
            lambda scraping: (scraping / "connection.py").write_text(
                (scraping / "connection.py").read_text(encoding="utf-8")
                + "\n\ndef public_helper():\n    return None\n",
                encoding="utf-8",
            ),
            id="ownership",
        ),
        pytest.param(
            lambda scraping: (scraping / "connection.py").write_text(
                (scraping / "connection.py").read_text(encoding="utf-8")
                + "\n\ndef _inspect_page(page):\n    return page.evaluate('1')\n",
                encoding="utf-8",
            ),
            id="source-classification-bare-page",
        ),
    ],
)
def test_source_mutations_make_the_generated_check_fail(tmp_path: Path, mutate):
    scraping = _copy_scraping(tmp_path)
    output = tmp_path / "scraping-architecture.md"
    output.write_text(render(scraping), encoding="utf-8")

    mutate(scraping)

    assert not check(output, scraping)


def test_dependency_direction_violations_are_reported_deterministically():
    modules = (
        ModuleInfo(
            "linkedin_mcp_server.scraping.alpha",
            "linkedin_mcp_server/scraping/alpha.py",
            (
                "linkedin_mcp_server.daemon_owner",
                "linkedin_mcp_server.scraping.beta",
                "linkedin_mcp_server.scraping.extractor",
            ),
            (),
            "browser-free",
        ),
        ModuleInfo(
            "linkedin_mcp_server.scraping.beta",
            "linkedin_mcp_server/scraping/beta.py",
            ("linkedin_mcp_server.scraping.alpha",),
            (),
            "browser-free",
        ),
    )

    assert dependency_violations(modules) == (
        "forbidden layer import: `linkedin_mcp_server.scraping.alpha` -> "
        "`linkedin_mcp_server.daemon_owner`",
        "import cycle: `linkedin_mcp_server.scraping.alpha` -> "
        "`linkedin_mcp_server.scraping.beta` -> "
        "`linkedin_mcp_server.scraping.alpha`",
        "reverse facade import: `linkedin_mcp_server.scraping.alpha` -> "
        "`linkedin_mcp_server.scraping.extractor`",
    )


def test_intended_core_leaf_imports_are_allowed():
    module = ModuleInfo(
        "linkedin_mcp_server.scraping.alpha",
        "linkedin_mcp_server/scraping/alpha.py",
        (
            "linkedin_mcp_server.core.auth",
            "linkedin_mcp_server.core.exceptions",
            "linkedin_mcp_server.core.proxy_errors",
            "linkedin_mcp_server.core.utils",
        ),
        (),
        "browser-free",
    )

    assert dependency_violations((module,)) == ()


@pytest.mark.parametrize(
    "path,source",
    [
        pytest.param(
            "connection.py",
            "from linkedin_mcp_server import process_protocol\n",
            id="forbidden-package-alias",
        ),
        pytest.param(
            "session.py",
            "from . import content\n",
            id="relative-package-cycle",
        ),
        pytest.param(
            "session.py",
            "from .capture import SectionCapture\n",
            id="relative-module-cycle",
        ),
        pytest.param(
            "session.py",
            "from ..scraping import capture\n",
            id="multi-level-relative-cycle",
        ),
        pytest.param(
            "session.py",
            "from . import session\n",
            id="relative-self-cycle",
        ),
        pytest.param(
            "connection.py",
            "import linkedin_mcp_server.core\n",
            id="forbidden-core-aggregate",
        ),
        pytest.param(
            "connection.py",
            "from linkedin_mcp_server import core\n",
            id="forbidden-core-aggregate-package-submodule",
        ),
        pytest.param(
            "connection.py",
            "import linkedin_mcp_server.core.browser\n",
            id="forbidden-core-browser",
        ),
        pytest.param(
            "connection.py",
            "from linkedin_mcp_server.core import browser\n",
            id="forbidden-core-browser-package-submodule",
        ),
    ],
)
def test_normalized_violations_fail_after_regeneration(
    tmp_path: Path, path: str, source: str
):
    scraping = _copy_scraping(tmp_path)
    _prepend(scraping / path, source)
    output = tmp_path / "scraping-architecture.md"
    output.write_text(render(scraping), encoding="utf-8")

    assert not check(output, scraping)


def test_check_fails_even_when_a_violation_is_regenerated(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    connection = scraping / "connection.py"
    connection.write_text(
        "from linkedin_mcp_server.scraping.extractor import LinkedInExtractor\n"
        + connection.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    output = tmp_path / "scraping-architecture.md"
    output.write_text(render(scraping), encoding="utf-8")

    assert not check(output, scraping)


def test_generated_content_mutation_makes_check_fail(tmp_path: Path):
    scraping = _copy_scraping(tmp_path)
    output = tmp_path / "scraping-architecture.md"
    output.write_text(render(scraping) + "manual drift\n", encoding="utf-8")

    assert not check(output, scraping)
