"""Every failure the server constructs names its error code.

A bare ``ToolError`` reaches the client as ``internal``, and so does a
``CodedToolError`` whose code was left to its default. Both look intended in a
diff. This walks every module under ``linkedin_mcp_server/`` and refuses either
construction, resolving the names imports bind: ``from ... import X as Y``,
``import a.b as m``, ``from a import b``, relative imports, and qualified
calls. Catching, ``isinstance``, annotations and re-raising an existing
exception construct nothing and stay allowed.

Two places are left alone on purpose. ``error_handler.py`` owns the mapping and
may construct ``ToolError``. The ``CodedToolError`` default exists for
``redacted_copy`` in ``core/proxy_errors.py``, which rebuilds an exception
through ``type(error)(redacted)``; that call names no class, the guard does not
resolve it, and ``_shape_tool_error`` puts the code back. Construction through a
variable holding the class or through ``functools.partial`` is outside what
this resolves.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = _ROOT / "linkedin_mcp_server"

_TOOL_ERROR = "fastmcp.exceptions.ToolError"
_CODED = "linkedin_mcp_server.error_handler.CodedToolError"
_HANDLER = "linkedin_mcp_server.error_handler"
#: Other qualified names the same classes are reachable under.
_SAME_CLASS = {f"{_HANDLER}.ToolError": _TOOL_ERROR}


def _module_name(path: Path) -> str:
    parts = path.relative_to(_ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if node.level == 0:
        return node.module or ""
    package = module.split(".")
    if not is_package:
        package = package[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _bindings(tree: ast.Module, module: str, is_package: bool) -> dict[str, str]:
    """Map every name an import or a top-level class binds to what it names.

    Imports anywhere in the module count, including inside functions, so a
    local import cannot hide a construction.
    """
    names = {
        node.name: f"{module}.{node.name}"
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    names[top] = top
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, module, is_package)
            for alias in node.names:
                names[alias.asname or alias.name] = f"{base}.{alias.name}"
    return names


def _qualified(expr: ast.expr, names: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name):
        found = names.get(expr.id)
    elif isinstance(expr, ast.Attribute):
        base = _qualified(expr.value, names)
        found = f"{base}.{expr.attr}" if base else None
    else:
        return None
    return _SAME_CLASS.get(found, found) if found else None


def _names_a_code(call: ast.Call) -> bool:
    return len(call.args) >= 2 or any(kw.arg == "code" for kw in call.keywords)


def violations(source: str, module: str, path: str, is_package: bool = False):
    """Every uncoded construction in *source*, as ``path:line: what to do``."""
    tree = ast.parse(source)
    names = _bindings(tree, module, is_package)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = _qualified(node.func, names)
            coded = _names_a_code(node)
        elif isinstance(node, ast.Raise) and node.exc is not None:
            # `raise ToolError` instantiates the class, with no arguments.
            target = _qualified(node.exc, names)
            coded = False
        else:
            continue
        if target == _TOOL_ERROR and module != _HANDLER:
            found.append(
                f"{path}:{node.lineno}: constructs fastmcp's ToolError, which a "
                "client reads as `internal`. Raise CodedToolError(message, "
                "ErrorCode.<CODE>), or pass the domain exception to "
                "raise_tool_error."
            )
        elif target == _CODED and not coded:
            found.append(
                f"{path}:{node.lineno}: constructs CodedToolError without a "
                "code, which defaults to `internal`. Pass the ErrorCode as the "
                "second argument or as code=, ErrorCode.INTERNAL included."
            )
    return found


def test_every_constructed_failure_names_its_code():
    found = [
        violation
        for path in sorted(_PACKAGE.rglob("*.py"))
        for violation in violations(
            path.read_text(encoding="utf-8"),
            _module_name(path),
            str(path.relative_to(_ROOT)),
            is_package=path.name == "__init__.py",
        )
    ]
    assert found == [], "\n".join(found)


_TOOLS = "linkedin_mcp_server.tools.example"


@pytest.mark.parametrize(
    ("module", "source"),
    [
        pytest.param(
            _TOOLS,
            "from fastmcp.exceptions import ToolError as Failure\nraise Failure('x')\n",
            id="aliased import",
        ),
        pytest.param(
            _TOOLS,
            "import fastmcp.exceptions\nraise fastmcp.exceptions.ToolError('x')\n",
            id="qualified call",
        ),
        pytest.param(
            _TOOLS,
            "import fastmcp.exceptions as fe\nraise fe.ToolError('x')\n",
            id="module alias",
        ),
        pytest.param(
            _TOOLS,
            "from fastmcp import exceptions\nraise exceptions.ToolError('x')\n",
            id="submodule import",
        ),
        pytest.param(
            _TOOLS,
            "from linkedin_mcp_server.error_handler import ToolError\n"
            "raise ToolError('x')\n",
            id="re-exported name",
        ),
        pytest.param(
            _TOOLS,
            "def f():\n"
            "    from fastmcp.exceptions import ToolError\n"
            "    raise ToolError\n",
            id="local import, raised as a class",
        ),
        pytest.param(
            _TOOLS,
            "from linkedin_mcp_server.error_handler import CodedToolError\n"
            "raise CodedToolError('x')\n",
            id="coded without a code",
        ),
        pytest.param(
            "linkedin_mcp_server.daemon.example",
            "from ..error_handler import CodedToolError as Coded\n"
            "raise Coded('x', retry_after=3)\n",
            id="relative alias without a code",
        ),
        pytest.param(
            _HANDLER,
            "class CodedToolError(Exception): ...\nraise CodedToolError('x')\n",
            id="coded without a code in the handler itself",
        ),
    ],
)
def test_an_uncoded_construction_is_refused_with_its_line(module, source):
    found = violations(source, module, "linkedin_mcp_server/example.py")

    assert len(found) == 1
    assert found[0].startswith(
        f"linkedin_mcp_server/example.py:{len(source.splitlines())}: "
    )


@pytest.mark.parametrize(
    ("module", "source"),
    [
        pytest.param(
            _TOOLS,
            "from fastmcp.exceptions import ToolError\n"
            "def f(e: ToolError) -> ToolError:\n"
            "    try:\n"
            "        raise e\n"
            "    except ToolError:\n"
            "        raise\n"
            "    if isinstance(e, ToolError):\n"
            "        raise e from None\n",
            id="catch, isinstance, annotation and re-raise",
        ),
        pytest.param(
            _TOOLS,
            "from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode\n"
            "raise CodedToolError('x', ErrorCode.INVALID_ARGUMENT)\n",
            id="coded with a positional code",
        ),
        pytest.param(
            _TOOLS,
            "from linkedin_mcp_server import error_handler as eh\n"
            "raise eh.CodedToolError('x', code=eh.ErrorCode.INTERNAL)\n",
            id="coded with a keyword code",
        ),
        pytest.param(
            _TOOLS,
            "from somewhere_else import ToolError\nraise ToolError('x')\n",
            id="another library's ToolError",
        ),
        pytest.param(
            _HANDLER,
            "from fastmcp.exceptions import ToolError\nraise ToolError('x')\n",
            id="ToolError in the handler",
        ),
    ],
)
def test_a_coded_construction_or_a_type_reference_is_allowed(module, source):
    assert violations(source, module, "linkedin_mcp_server/example.py") == []
