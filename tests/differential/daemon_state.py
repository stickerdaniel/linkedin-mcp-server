"""Where a row's daemon state lives, by the runtime that wrote it.

The candidate's owners keep their state where this checkout's
``daemon.descriptor`` says, and are asked to stand down with this checkout's
request (``daemon.owner.stand_down_request``). The frozen baseline
(``baseline.BASELINE_SHA``) predates the clean slate
(``docs/decisions/2026-10-11-daemon-clean-slate.md``): its owners keep their
state under the earlier namespace, publish descriptor schema 2, and know only
the stand-down request with no body. The harness reads that state to observe
K2 rows, never to share it, so this is evidence support for the frozen
baseline and not compatibility in the product.

The frozen layout only reads. It never prepares the earlier namespace, because
a row that must leave no daemon state would otherwise create it by looking.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
from linkedin_mcp_server.daemon import owner as daemon_owner
from linkedin_mcp_server.daemon.descriptor import DaemonDescriptor, DescriptorError

_DESCRIPTOR_FILE = "daemon.json"
_LOG_FILE = "daemon.log"


@dataclass(frozen=True)
class DaemonStateLayout:
    """One runtime's daemon state: its directory, its files and its stand-down."""

    #: The account-level namespace of a frozen runtime; None for the candidate,
    #: whose namespace is whatever this checkout's ``daemon.descriptor`` names.
    namespace: str | None
    #: The descriptor schema a frozen runtime publishes.
    schema_version: int = daemon_descriptor.SCHEMA_VERSION

    def directory(self, auth_root: Path) -> Path:
        """The daemon directory for *auth_root*, computed and never created."""
        if self.namespace is None:
            return daemon_descriptor.daemon_dir(auth_root)
        root = daemon_descriptor._account_home() / self.namespace / "daemon"
        # As the baseline computes it: resolved on POSIX, inspected component
        # by component on Windows.
        if os.name != "nt":
            root = root.resolve()
        return root / daemon_descriptor._daemon_dir_name(auth_root)

    def descriptor_path(self, auth_root: Path) -> Path:
        if self.namespace is None:
            return daemon_descriptor.descriptor_path(auth_root)
        return self.directory(auth_root) / _DESCRIPTOR_FILE

    def log_path(self, auth_root: Path) -> Path:
        if self.namespace is None:
            return daemon_owner.daemon_log_path(auth_root)
        return self.directory(auth_root) / _LOG_FILE

    def read(self, auth_root: Path) -> DaemonDescriptor | None:
        """The published descriptor as its runtime reads it, or None for none."""
        if self.namespace is None:
            return daemon_descriptor.read(auth_root)
        path = self.descriptor_path(auth_root)
        raw = daemon_descriptor._read_own_file(
            path, daemon_descriptor._MAX_DESCRIPTOR_BYTES, missing_is_none=True
        )
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise DescriptorError(
                f"The daemon descriptor at {path} is not valid JSON: {exc}"
            ) from exc
        descriptor = DaemonDescriptor.from_mapping(parsed)
        if descriptor.schema_version != self.schema_version:
            raise DescriptorError(
                f"The frozen daemon descriptor is version "
                f"{descriptor.schema_version}, not {self.schema_version}"
            )
        return descriptor

    def read_token(self, auth_root: Path, descriptor: DaemonDescriptor) -> str:
        """The bearer token beside *descriptor*, checked against its digest."""
        if self.namespace is None:
            return daemon_descriptor.read_token(auth_root, descriptor)
        # The identifier was checked to be a UUID when the descriptor was built.
        path = self.directory(auth_root) / f"token-{descriptor.instance_id}"
        text = daemon_descriptor._read_own_file(
            path,
            daemon_descriptor._MAX_TOKEN_BYTES,
            missing_is_none=False,
            missing_message="The frozen daemon has no token beside its descriptor",
        )
        token = (text or "").strip()
        if not descriptor.matches_token(token):
            raise DescriptorError(
                "The frozen daemon token does not match its descriptor"
            )
        return token

    def stand_down_body(self, instance_id: str) -> dict[str, Any] | None:
        """What asks this runtime's owner to hand over; None means no body."""
        if self.namespace is not None:
            return None
        return daemon_owner.stand_down_request(
            instance_id, mode=daemon_owner.STAND_DOWN_TURNOVER
        )


#: This checkout's own daemon state.
CANDIDATE_LAYOUT = DaemonStateLayout(namespace=None)

#: The frozen baseline's: ``.mcp-server-linkedin``, ``-v2`` on Windows, with
#: descriptor schema 2. Every frozen runtime gets it, because the one frozen
#: runtime is ``baseline.BASELINE_SHA``, which predates the clean slate. Moving
#: the pin past the clean slate has to move that choice with it.
BASELINE_LAYOUT = DaemonStateLayout(
    namespace=".mcp-server-linkedin-v2" if os.name == "nt" else ".mcp-server-linkedin",
    schema_version=2,
)
