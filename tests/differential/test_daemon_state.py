"""The daemon state layouts a row reads its owner through.

The candidate's state is this checkout's own. The frozen baseline's lives in
the namespace earlier releases used, and is read there without being created.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from differential.daemon_state import BASELINE_LAYOUT, CANDIDATE_LAYOUT
from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import DaemonDescriptor, DescriptorError


def _home() -> Path:
    return daemon_descriptor._account_home()


def _auth_root(tmp_path: Path) -> Path:
    auth_root = tmp_path / "auth"
    (auth_root / "profile").mkdir(parents=True)
    return auth_root


def _descriptor(auth_root: Path, token: str) -> DaemonDescriptor:
    return daemon_descriptor.build(
        instance_id=daemon_descriptor.new_instance_id(),
        package_version="4.20.0",
        runtime_id="native",
        profile=auth_root / "profile",
        host="127.0.0.1",
        port=49152,
        path="/mcp",
        token=token,
        config=AppConfig(),
        log_path=auth_root / "daemon.log",
    )


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def test_the_candidate_reads_this_checkouts_namespace(tmp_path: Path):
    auth_root = _auth_root(tmp_path)
    token = daemon_descriptor.new_token()
    published = _descriptor(auth_root, token)
    daemon_descriptor.publish(auth_root, published, token)

    read = CANDIDATE_LAYOUT.read(auth_root)

    assert read is not None and read.instance_id == published.instance_id
    assert CANDIDATE_LAYOUT.read_token(auth_root, read) == token
    assert CANDIDATE_LAYOUT.descriptor_path(auth_root).is_file()
    assert CANDIDATE_LAYOUT.directory(auth_root).is_relative_to(
        _home().resolve() / ".linkedin-mcp-daemon"
    )
    assert BASELINE_LAYOUT.namespace is not None
    assert not (_home() / BASELINE_LAYOUT.namespace).exists()


def test_the_baseline_reads_its_own_schema_where_it_wrote_it(tmp_path: Path):
    # What a K2 row's frozen owner leaves: schema 2, under the earlier
    # namespace, with its token beside it.
    auth_root = _auth_root(tmp_path)
    token = daemon_descriptor.new_token()
    published = _descriptor(auth_root, token)
    raw = json.loads(published.to_json())
    raw["schema_version"] = 2
    # Placed where the frozen release writes it, spelled out rather than taken
    # from the layout under test, so a layout that looks elsewhere fails here.
    released = ".mcp-server-linkedin-v2" if os.name == "nt" else ".mcp-server-linkedin"
    directory = (
        _home() / released / "daemon" / daemon_descriptor._daemon_dir_name(auth_root)
    )
    _write_private(directory / "daemon.json", json.dumps(raw))
    _write_private(directory / f"token-{published.instance_id}", token)

    read = BASELINE_LAYOUT.read(auth_root)

    assert read is not None and read.instance_id == published.instance_id
    assert BASELINE_LAYOUT.read_token(auth_root, read) == token
    assert CANDIDATE_LAYOUT.read(auth_root) is None
    assert BASELINE_LAYOUT.stand_down_body(published.instance_id) is None


def test_the_baseline_refuses_a_descriptor_of_another_schema(tmp_path: Path):
    auth_root = _auth_root(tmp_path)
    token = daemon_descriptor.new_token()
    _write_private(
        BASELINE_LAYOUT.descriptor_path(auth_root),
        _descriptor(auth_root, token).to_json(),
    )

    with pytest.raises(DescriptorError, match="not 2"):
        BASELINE_LAYOUT.read(auth_root)


def test_looking_for_a_baseline_owner_creates_nothing(tmp_path: Path):
    # A Direct row must leave no daemon state, so a lookup that finds none
    # cannot be what makes it.
    auth_root = _auth_root(tmp_path)
    assert BASELINE_LAYOUT.namespace is not None

    assert BASELINE_LAYOUT.read(auth_root) is None
    assert not (_home() / BASELINE_LAYOUT.namespace).exists()
