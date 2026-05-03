"""Project-wide constants."""

import os


def _resolve_tool_timeout_seconds(default: float = 90.0) -> float:
    raw = os.getenv("TOOL_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


TOOL_TIMEOUT_SECONDS: float = _resolve_tool_timeout_seconds()
