"""Rules about ``manifest.json`` that only a reader of the host can know.

``mcpb validate`` in the release workflow checks the manifest against its
schema. A manifest can pass that and still produce a bundle that cannot start,
because the schema says nothing about how a host resolves ``${user_config.X}``.

Measured against Claude Desktop's own substitution routine: it builds one
replacement map from the manifest's ``default`` values, overlays the answers
the user gave, and then rewrites ``${...}`` occurrences for the keys in that
map. A key in neither source is not in the map, and the placeholder is passed
to the process verbatim.

So an optional field with no ``default`` hands the server the string
``${user_config.proxy_server}`` as if it were an address. That shipped in
4.20.0 and stopped every bundle where the proxy fields were left blank, which
is the default for anyone not using a proxy. The rules below are that failure
written down.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER = re.compile(r"\$\{user_config\.([^}]*)\}")


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "manifest.json").read_text(encoding="utf-8"))


def _referenced_keys(node: Any) -> set[str]:
    """Every ``user_config`` key named anywhere below *node*.

    Walks rather than reading ``env`` directly, because a placeholder is just as
    valid inside ``args`` or a ``platform_overrides`` block, and a rule that
    only covers where the placeholders happen to sit today is a rule that stops
    holding the first time one moves.
    """
    if isinstance(node, str):
        return set(_PLACEHOLDER.findall(node))
    if isinstance(node, list):
        return set().union(*(_referenced_keys(item) for item in node), set())
    if isinstance(node, dict):
        return set().union(*(_referenced_keys(value) for value in node.values()), set())
    return set()


def test_every_referenced_key_is_declared(manifest: dict[str, Any]) -> None:
    declared = set(manifest.get("user_config", {}))
    referenced = _referenced_keys(manifest["server"]["mcp_config"])
    assert referenced <= declared, (
        f"mcp_config names user_config keys that do not exist: "
        f"{sorted(referenced - declared)}. A host cannot substitute those, so "
        f"the literal placeholder reaches the server as a setting."
    )


def test_every_optional_referenced_key_has_a_default(manifest: dict[str, Any]) -> None:
    """The rule that #678 exists for.

    ``required`` is the other way to be safe: a host skips the whole MCP config
    while a required field is empty, so no placeholder is ever handed over. An
    optional field has no such protection and needs a ``default`` — an empty
    string is enough, since it substitutes to nothing and the loader reads that
    as unset.
    """
    user_config = manifest.get("user_config", {})
    offenders = sorted(
        key
        for key in _referenced_keys(manifest["server"]["mcp_config"])
        if key in user_config
        and not user_config[key].get("required")
        and user_config[key].get("default") is None
    )
    assert not offenders, (
        f"Optional user_config fields referenced without a default: {offenders}. "
        f'Add "default": "" to each. Without it a host that leaves the field '
        f"blank passes the literal ${{user_config.NAME}} through, and the "
        f"server either refuses to start or treats it as a real value."
    )


def test_every_declared_key_is_referenced(manifest: dict[str, Any]) -> None:
    """A field nobody reads is a setting the user fills in for nothing."""
    declared = set(manifest.get("user_config", {}))
    referenced = _referenced_keys(manifest["server"]["mcp_config"])
    assert declared <= referenced, (
        f"user_config declares keys that mcp_config never uses: "
        f"{sorted(declared - referenced)}."
    )
