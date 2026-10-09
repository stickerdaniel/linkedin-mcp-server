"""Local records: one JSON file per change set, profile snapshots, audit log.

Only profile field values and change-set metadata are ever written here.
Cookies, storage state, headers and credentials never pass through this
module, and ``audit`` accepts an explicit allow-list of keys so a caller cannot
log something it should not by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import json
import os
import re
import tempfile

from linkedin_mcp_server.profile_edit.changeset import ChangeSet
from linkedin_mcp_server.profile_edit.errors import (
    ProfileEditError,
    ProfileEditErrorCode,
)

_ID = re.compile(r"^cs_[0-9a-f]{16}$")
AUDIT_KEYS = frozenset(
    {
        "at",
        "tool",
        "changeSetId",
        "section",
        "field",
        "result",
        "verified",
        "error",
        "status",
    }
)
_AUDIT_VALUE_LIMIT = 200


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)  # our own temp file, created above
        raise


class ProfileEditStore:
    def __init__(self, root: Path):
        self.root = root
        self.change_sets = root / "change-sets"
        self.history = root / "profile-history"
        self.audit_log = root / "audit.jsonl"

    def _path(self, change_set_id: str) -> Path:
        if not _ID.match(change_set_id):
            raise ProfileEditError(
                ProfileEditErrorCode.CHANGE_SET_NOT_FOUND,
                "Change set ids look like cs_ followed by 16 hex characters.",
                changeSetId=change_set_id,
            )
        return self.change_sets / f"{change_set_id}.json"

    def save(self, cs: ChangeSet) -> None:
        _atomic_write(
            self._path(cs.id), json.dumps(cs.as_dict(), ensure_ascii=False, indent=2)
        )

    def load(self, change_set_id: str) -> ChangeSet:
        path = self._path(change_set_id)
        if not path.is_file():
            raise ProfileEditError(
                ProfileEditErrorCode.CHANGE_SET_NOT_FOUND, changeSetId=change_set_id
            )
        return ChangeSet.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_snapshot(
        self, at: str, change_set_id: str, values: dict[str, Any]
    ) -> Path:
        """Record the live values about to be changed, for audit and manual rollback."""
        name = re.sub(r"[^0-9A-Za-z]", "-", at)[:19] + f"_{change_set_id}.json"
        path = self.history / name
        _atomic_write(
            path,
            json.dumps(
                {"takenAt": at, "changeSetId": change_set_id, "values": values},
                ensure_ascii=False,
                indent=2,
            ),
        )
        return path

    def audit(self, **event: Any) -> None:
        unknown = set(event) - AUDIT_KEYS
        if unknown:
            raise ValueError(f"audit keys not allowed: {sorted(unknown)}")
        clean = {
            k: (v[:_AUDIT_VALUE_LIMIT] if isinstance(v, str) else v)
            for k, v in event.items()
        }
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")
