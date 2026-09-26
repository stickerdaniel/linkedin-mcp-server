"""The harness refuses the user's auth root before it does anything.

Every case runs against a *fake* home: both sources the harness reads a home
from, the environment's and the operating system account's, are pointed at a
temporary directory holding a stand-in ``.linkedin-mcp``. Nothing here reads,
writes or lists the real one.

A refusal must come before the row stages a session, records its runtime or
spawns anything, so each refused case runs the real row entry with spies on
those steps and a census of this process's children. Its server command is a
harmless interpreter call, so even a broken guard could not start the product.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
from pathlib import Path

import psutil
import pytest

from differential import harness
from differential.events import EventLog
from differential.harness import (
    REAL_AUTH_ROOT_NAME,
    ContainmentError,
    claim_account,
    feed_requests,
    measure_host_quit_row,
)
from differential.synthetic_origin import (
    CA_FILE,
    FEED_MARKER,
    EgressProxy,
    SyntheticOrigin,
    issue_certificates,
)
from linkedin_mcp_server import daemon_descriptor


@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / REAL_AUTH_ROOT_NAME).mkdir(parents=True)
    (home / REAL_AUTH_ROOT_NAME / "sentinel").write_text("the user's session")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: home)
    assert Path.home() == home
    return home


def _children() -> set[tuple[int, float]]:
    found = set()
    for child in psutil.Process().children(recursive=True):
        try:
            found.add((child.pid, child.create_time()))
        except psutil.Error:
            continue
    return found


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this platform refused a symlink: {exc}")


async def _refused_before_anything(tmp_path, monkeypatch, profile: Path) -> str:
    """Run the row on *profile* and require a refusal with nothing done."""
    staged: list[object] = []
    recorded: list[object] = []

    async def stage(*args, **kwargs):
        staged.append(args)
        raise AssertionError("staging reached")

    def identity(*args, **kwargs):
        recorded.append(args)
        raise AssertionError("identity reached")

    monkeypatch.setattr(harness, "stage_signed_in_session", stage)
    monkeypatch.setattr(harness, "row_identity", identity)
    issue_certificates(tmp_path / "certificates")
    # Built but never started: the refusal has to come before they matter.
    origin = SyntheticOrigin(tmp_path / "certificates")
    proxy = EgressProxy({})
    log = EventLog(tmp_path / "evidence", run="sentinel")
    before = _children()
    try:
        with pytest.raises(ContainmentError) as refusal:
            await measure_host_quit_row(
                profile=profile,
                experiment="K3",
                daemon=True,
                egress=(origin, proxy),
                log=log,
                work_dir=tmp_path / "row",
                command=[sys.executable, "-c", "pass"],
            )
    finally:
        origin.server_close()
        proxy.server_close()
    assert _children() == before
    assert staged == [] and recorded == []
    assert log.records() == []
    assert not (tmp_path / "row").exists()
    return str(refusal.value)


async def test_the_real_root_as_written_is_refused(tmp_path, monkeypatch, fake_home):
    await _refused_before_anything(
        tmp_path, monkeypatch, fake_home / REAL_AUTH_ROOT_NAME / "profile"
    )
    assert (fake_home / REAL_AUTH_ROOT_NAME / "sentinel").read_text() == (
        "the user's session"
    )


async def test_a_case_alias_of_the_real_root_is_refused(
    tmp_path, monkeypatch, fake_home
):
    alias = fake_home / REAL_AUTH_ROOT_NAME.upper()
    if not alias.exists():
        pytest.skip("this temporary filesystem is case-sensitive")
    await _refused_before_anything(tmp_path, monkeypatch, alias / "profile")


async def test_a_symlink_to_the_real_root_is_refused(tmp_path, monkeypatch, fake_home):
    link = tmp_path / "alias"
    _symlink_or_skip(link, fake_home / REAL_AUTH_ROOT_NAME)
    await _refused_before_anything(tmp_path, monkeypatch, link / "profile")


async def test_a_symlink_into_the_real_root_is_refused(
    tmp_path, monkeypatch, fake_home
):
    nested = fake_home / REAL_AUTH_ROOT_NAME / "nested"
    nested.mkdir()
    link = tmp_path / "inner"
    _symlink_or_skip(link, nested)
    await _refused_before_anything(tmp_path, monkeypatch, link / "profile")


async def test_an_auth_root_above_the_real_root_is_refused(
    tmp_path, monkeypatch, fake_home
):
    # The home itself, by a name the string check cannot recognise.
    link = tmp_path / "home-alias"
    _symlink_or_skip(link, fake_home)
    await _refused_before_anything(tmp_path, monkeypatch, link / "profile")


async def test_the_home_as_the_auth_root_is_refused(tmp_path, monkeypatch, fake_home):
    await _refused_before_anything(tmp_path, monkeypatch, fake_home / "profile")


async def test_an_unknown_account_home_is_refused(tmp_path, monkeypatch, fake_home):
    def unknown():
        raise daemon_descriptor.DescriptorError("no passwd entry")

    monkeypatch.setattr(daemon_descriptor, "_account_home", unknown)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    message = await _refused_before_anything(
        tmp_path, monkeypatch, elsewhere / "profile"
    )
    assert "could not be determined" in message


async def test_a_missing_auth_root_is_refused(tmp_path, monkeypatch, fake_home):
    await _refused_before_anything(
        tmp_path, monkeypatch, tmp_path / "missing" / "profile"
    )


def test_a_distinct_directory_is_accepted(tmp_path, fake_home):
    auth = tmp_path / "auth"
    auth.mkdir()
    account = claim_account(auth / "profile")
    assert account.auth_root == Path(os.path.realpath(auth))


def test_a_distinct_directory_on_a_case_sensitive_volume_is_accepted(
    tmp_path, fake_home
):
    other = fake_home / REAL_AUTH_ROOT_NAME.upper()
    if other.exists():
        pytest.skip("this temporary filesystem is case-insensitive")
    other.mkdir()
    account = claim_account(other / "profile")
    assert account.auth_root == Path(os.path.realpath(other))


def _get(port: int, path: str, cafile: Path, cookie: str | None) -> str:
    """One GET to the origin, verified against the run's CA for the real name.

    Certificate and hostname checks stay on: the context trusts that one CA
    for this one connection, and the name it checks is the one the leaf is
    issued for. No trust store is touched.
    """
    context = ssl.create_default_context(cafile=str(cafile))
    headers = "Host: www.linkedin.com\r\nConnection: close\r\n"
    if cookie:
        headers += f"Cookie: {cookie}\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname="www.linkedin.com") as tls:
            tls.sendall(f"GET {path} HTTP/1.1\r\n{headers}\r\n".encode())
            chunks = []
            while chunk := tls.recv(65536):
                chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def test_the_origin_judges_the_session_each_request_carried(tmp_path):
    certificates = tmp_path / "certificates"
    issue_certificates(certificates)
    origin = SyntheticOrigin(certificates)
    origin.start()
    ca = certificates / CA_FILE
    try:
        before = _get(origin.port, "/feed/", ca, "li_at=staged; lang=en")
        origin.accept_session("staged")
        valid = _get(origin.port, "/feed/", ca, "li_at=staged; lang=en")
        other = _get(origin.port, "/feed/", ca, "li_at=replaced")
        none = _get(origin.port, "/feed/", ca, None)
        missing = _get(origin.port, "/elsewhere", ca, None)
    finally:
        origin.stop()

    assert all(FEED_MARKER in page for page in (before, valid, other, none))
    assert missing.startswith("HTTP/1.0 404")
    feeds = feed_requests(origin.requests)
    assert [r.session_valid for r in feeds] == [None, True, False, False]
    assert feeds[1].cookie_names == ("lang", "li_at")
    assert all(r.t > 0 for r in feeds)
    assert "staged" not in repr(origin.requests)
