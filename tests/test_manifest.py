"""Rules about ``manifest.json`` that only a reader of the host can know.

``mcpb validate`` in the release workflow checks the manifest against its
schema. A manifest can pass that and still produce a bundle that cannot start,
because the schema says nothing about how a host resolves ``${user_config.X}``.

Measured against Claude Desktop's own substitution routine. It builds one
replacement map from the manifest's ``default`` values, overlays the answers
the user gave, and then rewrites ``${...}`` occurrences for the keys in that
map. A key in neither source is not in the map, and the placeholder is passed
to the process verbatim.

So an optional field with no ``default`` hands the server the string
``${user_config.proxy_server}`` as if it were an address. That shipped in
4.20.0 and stopped every bundle where the proxy fields were left blank, which
is the default for anyone not using a proxy. The rules below are that failure
written down.

Position decides what a sufficient default is, and the two positions do not
agree. Measured by replaying the routine over a manifest built for the
question:

* In a string, the value is interpolated whatever it is, so ``""`` disappears
  and leaves nothing behind. That is where the four proxy variables sit.
* As an entire element of ``args``, the substitution is guarded by a
  truthiness test on the replacement. ``""`` is falsy, so the element keeps its
  literal and the browser is started with ``${user_config.NAME}`` as an
  argument.
* An array-valued default reached from a string is refused outright: the
  routine logs "Cannot replace with array value" and leaves the literal.

A rule that only knew the first case would bless the other two.
"""

import ast
import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER = re.compile(r"\$\{user_config\.([^}]*)\}")

# What counts as an entire element, which is the form the host's array branch
# looks for. Anything else in a list is treated as a string.
_WHOLE_PLACEHOLDER = re.compile(r"\$\{user_config\.([^}]*)\}\Z")

STRING = "string"
ARRAY_ELEMENT = "array element"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _references(node: Any) -> Iterator[tuple[str, str]]:
    """Every ``user_config`` key named below *node*, with where it is named.

    A full walk, because a placeholder is just as valid inside ``args`` or a
    ``platform_overrides`` block as it is in ``env``. A rule that only covers
    where the placeholders happen to sit today stops holding the first time one
    moves.

    The shape mirrors the host's own traversal: lists check each element for a
    whole placeholder first and fall back to the string rule, dictionaries
    descend into values and never into keys.
    """
    if isinstance(node, str):
        for key in _PLACEHOLDER.findall(node):
            yield key, STRING
    elif isinstance(node, list):
        for item in node:
            whole = (
                _WHOLE_PLACEHOLDER.fullmatch(item) if isinstance(item, str) else None
            )
            if whole is not None:
                yield whole.group(1), ARRAY_ELEMENT
            else:
                yield from _references(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _references(value)


def _mcp_config(manifest: dict[str, Any]) -> Any:
    return manifest["server"]["mcp_config"]


def _env_mappings(manifest: dict[str, Any]) -> dict[str, str]:
    """The environment mapping of the root config and of every platform.

    A platform override replaces the root ``env`` wholesale, so a placeholder
    that only appears under one platform is as real as one in the root.
    """
    config = _mcp_config(manifest)
    blocks = [config.get("env", {})]
    blocks += [
        override.get("env", {})
        for override in config.get("platform_overrides", {}).values()
    ]
    return {
        variable: value
        for block in blocks
        for variable, value in block.items()
        if _PLACEHOLDER.fullmatch(value)
    }


def test_every_referenced_key_is_declared(manifest: dict[str, Any]) -> None:
    declared = set(manifest.get("user_config", {}))
    referenced = {key for key, _ in _references(_mcp_config(manifest))}
    assert referenced <= declared, (
        f"mcp_config names user_config keys that do not exist: "
        f"{sorted(referenced - declared)}. A host cannot substitute those, so "
        f"the literal placeholder reaches the server as a setting."
    )


def test_every_optional_reference_survives_a_blank_field(
    manifest: dict[str, Any],
) -> None:
    """The rule that #678 exists for.

    ``required`` is the other way to be safe: a host skips the whole MCP config
    while a required field is empty, so no placeholder is ever handed over.

    An optional field needs a ``default`` the host will actually substitute,
    and what qualifies depends on where the placeholder sits. In a string any
    default works and ``""`` is the natural one. As a whole element of ``args``
    the substitution is guarded by a truthiness test, so ``""`` and ``0`` leave
    the literal in place.
    """
    user_config = manifest.get("user_config", {})
    offenders = []
    for key, position in _references(_mcp_config(manifest)):
        entry = user_config.get(key)
        if entry is None or entry.get("required"):
            continue
        default = entry.get("default")
        if default is None:
            offenders.append(f"{key} ({position}): no default")
        elif position == STRING and isinstance(default, list):
            # The host refuses to interpolate an array into a string and keeps
            # the literal, with "Cannot replace with array value" on its console.
            offenders.append(f"{key} ({position}): default is a list")
        elif position == ARRAY_ELEMENT and default in ("", 0, False):
            offenders.append(f"{key} ({position}): default is falsy")
    assert not offenders, (
        f"Optional user_config references a blank field would not survive: "
        f'{sorted(offenders)}. In a string give the field a default; "" is '
        f"enough. As a whole args element give it a non-empty one, or drop the "
        f"argument instead of passing an empty value."
    )


def test_every_declared_key_is_referenced(manifest: dict[str, Any]) -> None:
    """A field nobody reads is a setting the user fills in for nothing."""
    declared = set(manifest.get("user_config", {}))
    referenced = {key for key, _ in _references(_mcp_config(manifest))}
    assert declared <= referenced, (
        f"user_config declares keys that mcp_config never uses: "
        f"{sorted(declared - referenced)}."
    )


def test_no_default_is_itself_a_placeholder(manifest: dict[str, Any]) -> None:
    """A default that names another field only moves the problem.

    The host resolves a default the same way it resolves anything else, so
    ``"default": "${user_config.missing}"`` satisfies the rule above while
    still handing a literal to the server. A list default is checked element by
    element, since ``["${user_config.missing}"]`` hides the same thing.
    """

    def names_a_placeholder(default: Any) -> bool:
        if isinstance(default, str):
            return _PLACEHOLDER.search(default) is not None
        if isinstance(default, list):
            return any(names_a_placeholder(item) for item in default)
        return False

    offenders = sorted(
        key
        for key, entry in manifest.get("user_config", {}).items()
        if names_a_placeholder(entry.get("default"))
    )
    assert not offenders, (
        f"user_config defaults that are themselves placeholders: {offenders}."
    )


def test_the_loader_knows_the_same_mapping(manifest: dict[str, Any]) -> None:
    """``_MCPB_PLACEHOLDERS`` has to say what the manifest says.

    The loader drops a leftover placeholder by comparing against the exact
    string, which is what stops it from also eating a password that happens to
    read like one. Exactness is only worth anything while the two agree, and
    they are edited in different files.
    """
    from linkedin_mcp_server.config.loaders import _MCPB_PLACEHOLDERS

    assert _MCPB_PLACEHOLDERS == _env_mappings(manifest), (
        "config/loaders.py and manifest.json disagree about which environment "
        "variables carry user_config placeholders, or about their exact text. "
        "A variable the loader does not know keeps its literal; a string that "
        "does not match is compared against nothing."
    )


def _named_variable(node: ast.expr, environment_keys: Any) -> str | None:
    """The variable name *node* stands for, if it names one at all.

    Both spellings the loader could use: the constant off ``EnvironmentKeys``,
    which is the house style, and a bare string, which is what somebody writes
    who has not noticed the class.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute) and ast.unparse(node.value) == "EnvironmentKeys":
        value = getattr(environment_keys, node.attr, None)
        return value if isinstance(value, str) else None
    return None


def test_a_mapped_variable_is_never_read_raw() -> None:
    """Knowing the mapping is worth nothing if the read goes around it.

    ``_MCPB_PLACEHOLDERS`` is a table, and a table can be extended while the
    branch that reads the variable still reaches into ``os.environ`` directly.
    Every test above would stay green and the literal would be taken for the
    setting, so the check is on the reading code itself.

    Every way of asking ``os.environ`` for one name counts, since a rule that
    only knew ``.get`` would be satisfied by a subscript.
    """
    from linkedin_mcp_server.config import loaders

    guarded = set(loaders._MCPB_PLACEHOLDERS)
    keys = loaders.EnvironmentKeys
    offenders = set()
    for node in ast.walk(ast.parse(Path(loaders.__file__).read_text(encoding="utf-8"))):
        named = None
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "pop", "setdefault")
            and ast.unparse(node.func.value) == "os.environ"
        ):
            named = _named_variable(node.args[0], keys)
        elif (
            isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ"
        ):
            named = _named_variable(node.slice, keys)
        if named in guarded:
            offenders.add(named)
    assert not offenders, (
        f"These carry a user_config placeholder but are read straight out of "
        f"os.environ: {sorted(offenders)}. Read them through _env(), which is "
        f"what turns an unsubstituted placeholder back into an unset value."
    )
