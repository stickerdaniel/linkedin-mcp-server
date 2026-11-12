"""The run-by-hand origin: its ``check`` line, read from real files, the
pages it serves over TLS once sessions are required, and its refusal to
stage over a profile that holds anything. No browser."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from collections.abc import Iterator
from pathlib import Path

import pytest

from differential import manual_origin
from differential.manual_origin import describe_session
from differential.session import digest, write_synthetic_cookie_file
from differential.synthetic_origin import (
    CA_FILE,
    LOGIN_REDIRECT,
    SELF_PATH,
    SELF_USERNAME,
    SyntheticOrigin,
    issue_certificates,
    person_path,
)
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


@pytest.fixture
def strict_origin(tmp_path) -> Iterator[tuple[SyntheticOrigin, Path]]:
    certificates = tmp_path / "certificates"
    issue_certificates(certificates)
    origin = SyntheticOrigin(certificates)
    origin.require_sessions()
    origin.arm_login()
    origin.accept_session("staged-session")
    origin.start()
    try:
        yield origin, certificates
    finally:
        origin.stop()


def _get(
    origin: SyntheticOrigin, certificates: Path, path: str, li_at: str | None
) -> tuple[int, str | None]:
    """One GET as a browser behind the proxy sends it: TLS to the origin as
    www.linkedin.com, trusting only the issued CA, with the session as its
    cookie."""
    context = ssl.create_default_context(cafile=str(certificates / CA_FILE))
    cookie = "" if li_at is None else f"Cookie: li_at={li_at}\r\n"
    request = (
        f"GET {path} HTTP/1.1\r\nHost: www.linkedin.com\r\n{cookie}"
        f"Connection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", origin.port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname="www.linkedin.com") as tls:
            tls.sendall(request.encode())
            response = http.client.HTTPResponse(tls)
            response.begin()
            response.read()
            return response.status, response.getheader("Location")


@pytest.mark.parametrize(
    "path",
    [
        "/feed/",
        person_path("someone", "main_profile"),
        person_path("someone", "experience"),
        SELF_PATH,
    ],
)
@pytest.mark.parametrize("li_at", [None, "lost-or-foreign"])
def test_a_page_without_the_accepted_session_meets_the_wall(strict_origin, path, li_at):
    assert _get(*strict_origin, path, li_at) == (302, LOGIN_REDIRECT)


@pytest.mark.parametrize(
    "path",
    [
        "/feed/",
        person_path("someone", "main_profile"),
        person_path("someone", "education"),
    ],
)
def test_a_page_with_the_accepted_session_is_served(strict_origin, path):
    assert _get(*strict_origin, path, "staged-session") == (200, None)


def test_the_own_profile_alias_leads_to_the_synthetic_member(strict_origin):
    assert _get(*strict_origin, SELF_PATH, "staged-session") == (
        302,
        person_path(SELF_USERNAME, "main_profile"),
    )


def test_staging_refuses_a_profile_that_holds_anything(
    isolate_profile_dir, strict_origin, monkeypatch
):
    def install() -> None:
        raise AssertionError("staging went past its refusal")

    monkeypatch.setattr(
        "linkedin_mcp_server.bootstrap.ensure_browser_installed", install
    )
    leftover = portable_cookie_path(isolate_profile_dir)
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("[]")

    with pytest.raises(SystemExit, match="Refused") as refusal:
        manual_origin.stage(strict_origin[0])

    assert str(leftover) in str(refusal.value)
    assert leftover.read_text() == "[]"
