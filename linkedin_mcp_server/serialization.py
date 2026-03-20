"""Output serialization utilities for MCP tool responses."""

from typing import Any


def strip_none(data: Any) -> Any:
    """Recursively remove keys with None values from dicts.

    Contract:
    - Only removes keys whose value ``is None``.
    - Never strips falsy values: ``0``, ``False``, ``""``, ``[]``, ``{}``.
    - Processes nested dicts and lists.
    - Non-dict/list inputs are returned unchanged.
    """
    if isinstance(data, dict):
        return {k: strip_none(v) for k, v in data.items() if v is not None}
    if isinstance(data, list):
        return [strip_none(item) for item in data]
    return data
