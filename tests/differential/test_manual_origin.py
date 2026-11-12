"""The run-by-hand origin's ``check`` line, read from real files, no browser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from differential.manual_origin import describe_session
from differential.session import digest, write_synthetic_cookie_file
from linkedin_mcp_server.session_state import (
    portable_cookie_path,
    source_state_path,
    write_source_state,
)


@pytest.fixture
def profile(tmp_path) -> Path:
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    return profile


def _generation(profile: Path) -> str:
    return json.loads(source_state_path(profile).read_text())["login_generation"]


def test_check_names_the_staged_session_and_the_origin_verdict(profile):
    staged = write_synthetic_cookie_file(portable_cookie_path(profile))
    write_source_state(profile)
    generation = _generation(profile)

    accepted = describe_session(
        profile, staged.li_at_digest, {staged.li_at_digest}.__contains__
    )
    refused = describe_session(profile, staged.li_at_digest, lambda _: False)

    assert accepted == (
        f"staged li_at, accepted by the origin; generation {generation}; no quarantine"
    )
    assert refused.startswith("staged li_at, refused by the origin;")


def test_check_tells_a_replaced_session_from_the_staged_one(profile):
    staged = write_synthetic_cookie_file(portable_cookie_path(profile))
    path = portable_cookie_path(profile)
    entries = json.loads(path.read_text())
    for entry in entries:
        if entry["name"] == "li_at":
            entry["value"] = "synthetic-issued-replacement"
    path.write_text(json.dumps(entries))

    line = describe_session(
        profile,
        staged.li_at_digest,
        {digest("synthetic-issued-replacement")}.__contains__,
    )

    assert line == "other li_at, accepted by the origin; generation none; no quarantine"


def test_check_after_a_logout_finds_no_session(profile):
    assert (
        describe_session(profile, digest("anything"), lambda _: True)
        == "no li_at; generation none; no quarantine"
    )
