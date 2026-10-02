"""Profile-edit settings, read from the environment at call time.

Writes are off unless ``MCP_LINKEDIN_WRITE_ENABLED`` is explicitly true. That
flag and ``confirm=true`` on ``apply_profile_changes`` are independent
safeguards; neither implies the other.
"""

from __future__ import annotations

from pathlib import Path

from linkedin_mcp_server.config.loaders import _env

WRITE_ENABLED_ENV = "MCP_LINKEDIN_WRITE_ENABLED"
EDITS_DIR_ENV = "LINKEDIN_PROFILE_EDITS_DIR"
DEFAULT_EDITS_DIR = "~/.linkedin-mcp/profile-edits"

# Pause between two writes in one change set. Profile maintenance is occasional
# and human-directed; there is no bulk mode, and nothing retries a failed write.
WRITE_PACING_SECONDS = 3.0


def writes_enabled() -> bool:
    return (_env(WRITE_ENABLED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def edits_root() -> Path:
    return Path(_env(EDITS_DIR_ENV) or DEFAULT_EDITS_DIR).expanduser().resolve()
