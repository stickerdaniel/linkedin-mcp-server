"""The harness refuses the user's own auth root before it does anything.

The sentinel hands the row runner the real ``~/.linkedin-mcp`` and requires a
refusal with nothing spawned: no watcher, no server, no browser, and no event
written. Its server command is a harmless interpreter call, so even a broken
guard could not start the product against that directory; the process census
is what catches the break.

Nothing here reads, writes or lists anything under the real root. The profile
path is judged as a string first, and a string inside it is refused there.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
from pathlib import Path

import psutil
import pytest

from differential.events import EventLog
from differential.harness import (
    REAL_AUTH_ROOT_NAME,
    ContainmentError,
    RowVector,
    claim_account,
    compare_repeat,
    compare_to_direct,
    feed_requests,
    measure_host_quit_row,
    row_expectations,
)
from differential.session import LOST_SILENT, RETAINED
from differential.synthetic_origin import (
    CA_FILE,
    FEED_MARKER,
    EgressProxy,
    SyntheticOrigin,
    issue_certificates,
)


def _children() -> set[tuple[int, float]]:
    found = set()
    for child in psutil.Process().children(recursive=True):
        try:
            found.add((child.pid, child.create_time()))
        except psutil.Error:
            continue
    return found


async def test_the_real_auth_root_is_refused_before_anything_is_spawned(tmp_path):
    real_profile = Path.home() / REAL_AUTH_ROOT_NAME / "profile"
    issue_certificates(tmp_path / "certificates")
    # Built but never started: the refusal has to come before they matter.
    origin = SyntheticOrigin(tmp_path / "certificates")
    proxy = EgressProxy({})
    log = EventLog(tmp_path / "evidence", run="sentinel")
    before = _children()
    try:
        with pytest.raises(ContainmentError, match="overlaps the account's own"):
            await measure_host_quit_row(
                profile=real_profile,
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
    assert log.records() == []
    assert not (tmp_path / "row").exists()


@pytest.mark.parametrize(
    "relative",
    [
        (REAL_AUTH_ROOT_NAME, "profile"),
        (REAL_AUTH_ROOT_NAME, "nested", "profile"),
        # The profile *is* the real root, so the auth root is the home itself.
        (REAL_AUTH_ROOT_NAME,),
        # An auth root that contains the real one.
        ("profile",),
    ],
)
def test_every_overlap_with_the_real_root_is_refused(relative):
    with pytest.raises(ContainmentError):
        claim_account(Path.home().joinpath(*relative))


def test_a_temporary_auth_root_is_accepted(tmp_path):
    account = claim_account(tmp_path / "auth" / "profile")
    assert account.auth_root == Path(os.path.realpath(tmp_path / "auth"))


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


def test_the_origin_records_feed_requests_and_the_session_they_carried(tmp_path):
    certificates = tmp_path / "certificates"
    issue_certificates(certificates)
    origin = SyntheticOrigin(certificates)
    origin.start()
    try:
        feed = _get(origin.port, "/feed/", certificates / CA_FILE, "li_at=s; lang=en")
        other = _get(origin.port, "/elsewhere", certificates / CA_FILE, None)
    finally:
        origin.stop()

    assert feed.startswith("HTTP/1.0 200") and FEED_MARKER in feed
    assert other.startswith("HTTP/1.0 404")
    feeds = feed_requests(origin.requests)
    assert [r.path for r in feeds] == ["/feed/"]
    assert feeds[0].cookie_names == ("lang", "li_at")
    assert feeds[0].t > 0
    assert feed_requests([r for r in origin.requests if r.path != "/feed/"]) == []


def _vector(**changes) -> RowVector:
    fields = {
        "o1_single_browser": True,
        "browser_seen": True,
        "o4_session": RETAINED,
        "origin_saw_feed": True,
        "feed_carried_session": True,
        "tool_succeeded": True,
    }
    fields.update(changes)
    return RowVector(**fields)


def test_a_clean_row_meets_every_expectation():
    assert row_expectations(_vector()) == []


@pytest.mark.parametrize(
    ("change", "reported"),
    [
        ({"o1_single_browser": False}, "O1"),
        ({"browser_seen": False}, "never saw a browser"),
        ({"origin_saw_feed": False}, "no /feed/ request"),
        ({"feed_carried_session": False}, "li_at"),
        ({"tool_succeeded": False}, "did not return"),
        ({"o4_session": LOST_SILENT}, "O4"),
    ],
)
def test_each_unmet_expectation_fails_the_row(change, reported):
    (failure,) = row_expectations(_vector(**change))
    assert reported in failure


def test_k0_compares_every_field():
    assert compare_repeat(_vector(), _vector()) == []
    for change in (
        {"o1_single_browser": False},
        {"browser_seen": False},
        {"o4_session": LOST_SILENT},
        {"origin_saw_feed": False},
        {"feed_carried_session": False},
        {"tool_succeeded": False},
    ):
        assert len(compare_repeat(_vector(), _vector(**change))) == 1, change


def test_k3_is_held_to_k1_on_o1_and_o4():
    assert compare_to_direct(_vector(), _vector()) == []
    assert compare_to_direct(_vector(), _vector(o1_single_browser=False))
    assert compare_to_direct(_vector(), _vector(o4_session=LOST_SILENT))
    # Not O1 or O4: those are the row's own expectations, not this comparison.
    assert compare_to_direct(_vector(), _vector(tool_succeeded=False)) == []
