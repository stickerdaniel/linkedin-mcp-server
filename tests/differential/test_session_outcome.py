"""R17: the snapshot reads the artefacts, and the outcome follows from them.

Real files written by the product's own writers, no browser. Each case changes
one artefact the way a failure would and checks the outcome it produces. Only
the unchanged session with a post-quit observation that saw it accepted may
read as retained.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from differential import harness
from linkedin_mcp_server import browser_launch
from linkedin_mcp_server.logging_config import MCPJSONFormatter
from differential.session import (
    CALLER,
    CLEARED_BY_USER,
    LAST_VERSION_FILE,
    LOST_ANNOUNCED,
    LOST_SILENT,
    PRESERVATION,
    PROBE,
    RETAINED,
    ROW,
    UNCERTAIN,
    Shown,
    StagedSession,
    announces_loss,
    r17_outcome,
    snapshot,
    write_synthetic_cookie_file,
)
from differential.synthetic_origin import (
    POST_MARKER,
    SYNTHETIC_POST_URL,
    _FEED_PAGE,
    cookie_names,
    cookie_values,
)
from linkedin_mcp_server.linkedin.feed_payload import POST_SLUG_URL_RE
from linkedin_mcp_server.profile_claim import ensure_profile_claim
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    clear_auth_state,
    portable_cookie_path,
    rotate_source_profile,
    source_state_path,
    write_source_state,
)


@pytest.fixture
def signed_in(tmp_path) -> tuple[Path, StagedSession]:
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    (profile / LAST_VERSION_FILE).write_text("153.0.8010.12")
    staged = write_synthetic_cookie_file(portable_cookie_path(profile))
    write_source_state(profile)
    return profile, staged


def _read(profile: Path, staged: StagedSession):
    return snapshot(profile, expected_digest=staged.li_at_digest)


def _rewrite_li_at(profile: Path, change) -> None:
    path = portable_cookie_path(profile)
    entries = json.loads(path.read_text())
    for entry in entries:
        if entry["name"] == "li_at":
            change(entry)
    path.write_text(json.dumps(entries))


def test_a_staged_session_reads_as_usable(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    assert before.usable
    assert before.li_at_staged and before.li_at_on_domain and before.li_at_unexpired
    assert before.profile_present
    assert before.last_version == "153.0.8010.12"
    assert before.quarantine == () and before.unreadable == ()


def test_without_the_staged_value_nothing_is_usable(signed_in):
    profile, _ = signed_in
    unknown = snapshot(profile)
    assert unknown.li_at_staged is None
    assert not unknown.usable


def test_the_snapshot_never_carries_a_cookie_value(signed_in):
    profile, staged = signed_in
    values = [
        entry["value"]
        for entry in json.loads(portable_cookie_path(profile).read_text())
    ]
    serialised = json.dumps(_read(profile, staged).as_event_fields())
    assert staged.li_at in values
    assert not any(value in serialised for value in values)
    assert staged.li_at not in repr(staged)


def test_unchanged_and_accepted_after_quit_is_retained(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    assert r17_outcome(before, _read(profile, staged), [], post_quit=True) == RETAINED


def test_a_re_exported_file_with_the_same_session_is_still_retained(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    # What a close's export does: the same cookies, written again differently.
    path = portable_cookie_path(profile)
    path.write_text(json.dumps(json.loads(path.read_text())))
    after = _read(profile, staged)
    assert after.cookies_sha256 != before.cookies_sha256
    assert r17_outcome(before, after, [], post_quit=True) == RETAINED


def test_unchanged_files_without_a_post_quit_observation_are_uncertain(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    assert r17_outcome(before, _read(profile, staged), [], post_quit=None) == UNCERTAIN


def test_unchanged_files_the_origin_no_longer_accepts_are_a_loss(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    after = _read(profile, staged)
    assert r17_outcome(before, after, [], post_quit=False) == LOST_SILENT


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(
            lambda p: portable_cookie_path(p).write_text(
                json.dumps([{"name": "li_at"}])
            ),
            id="only-a-name",
        ),
        pytest.param(
            lambda p: _rewrite_li_at(p, lambda e: e.update(value="")),
            id="empty-value",
        ),
        pytest.param(
            lambda p: _rewrite_li_at(p, lambda e: e.update(value="synthetic-other")),
            id="another-session",
        ),
        pytest.param(
            lambda p: _rewrite_li_at(p, lambda e: e.update(expires=1)),
            id="expired",
        ),
        pytest.param(
            lambda p: _rewrite_li_at(p, lambda e: e.update(expires=-1)),
            id="session-only",
        ),
        pytest.param(
            lambda p: _rewrite_li_at(p, lambda e: e.update(domain="example.invalid")),
            id="wrong-domain",
        ),
        pytest.param(lambda p: shutil.rmtree(p), id="profile-deleted"),
        pytest.param(lambda p: portable_cookie_path(p).unlink(), id="cookie-file-gone"),
        pytest.param(
            lambda p: _rewrite_li_at(p, lambda e: e.update(name="li_at_gone")),
            id="no-li-at",
        ),
        pytest.param(
            lambda p: (p.parent / f"{QUARANTINE_PREFIX}20260926T000000").mkdir(),
            id="new-quarantine",
        ),
        pytest.param(lambda p: write_source_state(p), id="moved-generation"),
    ],
)
def test_an_unusable_after_state_is_never_retained(signed_in, corrupt):
    profile, staged = signed_in
    before = _read(profile, staged)
    corrupt(profile)
    outcome = r17_outcome(before, _read(profile, staged), [], post_quit=True)
    assert outcome in (LOST_SILENT, UNCERTAIN), outcome
    assert outcome != RETAINED


def test_a_malformed_li_at_is_uncertain_not_retained(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    _rewrite_li_at(profile, lambda e: e.update(value=None))
    after = _read(profile, staged)
    assert after.unreadable
    assert r17_outcome(before, after, [], post_quit=True) == UNCERTAIN


def test_corruption_after_the_tool_call_and_before_exit_fails_o4(signed_in):
    # The tool call succeeded against the page; then, before the server exited,
    # the session on disk was replaced. Neither the earlier success nor an
    # unchanged generation may carry it to retained.
    profile, staged = signed_in
    before = _read(profile, staged)
    output = ["Using source profile for runtime", "get_feed returned 1 post"]
    _rewrite_li_at(profile, lambda e: e.update(value="synthetic-replaced"))
    after = _read(profile, staged)
    assert after.generation == before.generation
    assert r17_outcome(before, after, output, post_quit=False) == LOST_SILENT


def test_a_loss_the_user_was_told_about_is_announced(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    portable_cookie_path(profile).unlink()
    output = ["❌ Session expired or invalid", "   Run with --login to re-authenticate"]
    assert (
        r17_outcome(before, _read(profile, staged), output, post_quit=False)
        == LOST_ANNOUNCED
    )


def test_a_success_line_before_the_loss_does_not_announce_it(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    portable_cookie_path(profile).unlink()
    output = ["✅ Successfully signed in to LinkedIn"]
    assert (
        r17_outcome(before, _read(profile, staged), output, post_quit=False)
        == LOST_SILENT
    )


def test_a_cleared_session_the_user_asked_for_is_cleared_by_user(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    shutil.rmtree(profile)
    portable_cookie_path(profile).unlink()
    source_state_path(profile).unlink()
    after = _read(profile, staged)
    assert (
        r17_outcome(before, after, [], post_quit=False, user_cleared=True)
        == CLEARED_BY_USER
    )


def _log_out(profile: Path) -> None:
    """The product's own logout, on a root the test claims first."""
    ensure_profile_claim(profile, claim_anyway=True)
    assert clear_auth_state(profile) is True


def test_a_confirmed_logout_that_cleared_is_cleared_by_user(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    _log_out(profile)
    after = _read(profile, staged)
    assert (
        r17_outcome(before, after, [], post_quit=False, user_cleared=True)
        == CLEARED_BY_USER
    )


def test_a_clear_nobody_confirmed_is_a_loss(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    _log_out(profile)
    after = _read(profile, staged)
    assert r17_outcome(before, after, [], post_quit=False) == LOST_SILENT


@pytest.mark.parametrize("post_quit", [True, False])
def test_a_confirmation_over_a_session_still_there_is_no_clear(signed_in, post_quit):
    # The user confirmed, and the logout never happened.
    profile, staged = signed_in
    before = _read(profile, staged)
    outcome = r17_outcome(
        before, _read(profile, staged), [], post_quit=post_quit, user_cleared=True
    )
    assert outcome == (RETAINED if post_quit else LOST_SILENT)


def _keep_only(kept: str):
    """Remove what a logout removes except *kept*."""

    def damage(profile: Path) -> None:
        if kept != "profile":
            shutil.rmtree(profile)
        if kept != "cookie-file":
            portable_cookie_path(profile).unlink()
        if kept != "generation":
            source_state_path(profile).unlink()

    return damage


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_keep_only("profile"), id="profile-left"),
        pytest.param(_keep_only("cookie-file"), id="cookie-file-left"),
        pytest.param(_keep_only("generation"), id="generation-left"),
        pytest.param(
            lambda p: (
                _log_out(p),
                (p.parent / f"{QUARANTINE_PREFIX}20261001T000000").mkdir(),
            ),
            id="quarantine-left",
        ),
    ],
)
def test_a_confirmation_over_a_partial_clear_is_no_clear(signed_in, damage):
    profile, staged = signed_in
    before = _read(profile, staged)
    damage(profile)
    after = _read(profile, staged)
    assert not after.unreadable
    outcome = r17_outcome(before, after, [], post_quit=False, user_cleared=True)
    assert outcome == LOST_SILENT


def test_a_cookie_file_that_does_not_read_is_not_cleared(signed_in):
    profile, staged = signed_in
    _log_out(profile)
    portable_cookie_path(profile).mkdir()
    after = _read(profile, staged)
    assert after.unreadable and after.cookies_sha256 is None
    assert not after.cleared


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="root ignores directory modes",
)
def test_a_quarantine_listing_that_fails_is_uncertain_not_empty(signed_in):
    # Everything else still reads by name; only the auth root cannot be listed.
    profile, staged = signed_in
    before = _read(profile, staged)
    profile.parent.chmod(0o300)
    try:
        after = _read(profile, staged)
    finally:
        profile.parent.chmod(0o700)
    assert after.usable
    assert after.unreadable == ("quarantine: PermissionError",)
    assert r17_outcome(before, after, [], post_quit=True) == UNCERTAIN


def test_the_quarantine_reader_lists_what_the_product_quarantined(signed_in):
    profile, staged = signed_in
    ensure_profile_claim(profile, claim_anyway=True)
    rotated = rotate_source_profile(profile)
    assert rotated is not None
    reading = _read(profile, staged)
    assert reading.quarantine == (rotated.name,)
    assert reading.unreadable == ()


def test_an_unreadable_artefact_is_uncertain(signed_in):
    profile, staged = signed_in
    before = _read(profile, staged)
    portable_cookie_path(profile).write_text("{not json")
    after = _read(profile, staged)
    assert after.unreadable
    assert r17_outcome(before, after, [], post_quit=True) == UNCERTAIN


def test_no_session_to_begin_with_is_uncertain(tmp_path):
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    empty = snapshot(profile, expected_digest="0" * 64)
    assert not empty.usable
    assert r17_outcome(empty, empty, [], post_quit=True) == UNCERTAIN


@pytest.mark.parametrize(
    ("lines", "announced"),
    [
        (["Run with --login to create a profile."], True),
        (["Sign in to LinkedIn again"], True),
        (["Session expired or invalid."], True),
        (["Successfully signed in"], False),
        (["✅ Session is valid (profile: /p)"], False),
        # A sign-in after a notice does not take the notice back.
        (["Session expired or invalid.", "Successfully signed in"], True),
        (["Successfully signed in", "Session expired or invalid."], True),
        (['{"message": "Processing request of type CallToolRequest"}'], False),
        (["Stdio transport session started"], False),
        (["Browser closed"], False),
    ],
)
def test_only_a_notice_announces_a_loss(lines, announced):
    assert announces_loss(lines) is announced


def test_a_driver_launch_through_a_login_cache_does_not_announce_the_loss(signed_in):
    # Measured from both locked drivers: the ``<launching>`` line names the
    # browser cache, and a cache directory called ``login`` matches the loss
    # notice. The line is the driver's, so it is kept out of what the row
    # classifies; the same words from the server would still announce.
    profile, staged = signed_in
    before = _read(profile, staged)
    launch = (
        "2026-10-05T09:27:25.691Z pw:browser <launching> "
        "/cache/login/browsers/chromium-1243/chrome-mac-arm64/Google Chrome "
        "for Testing.app/Contents/MacOS/Google Chrome for Testing "
        "--disable-sync --user-data-dir=/profile"
    )
    assert announces_loss([launch]) is True
    heard = SimpleNamespace(stderr=[], user_lines=[])
    harness.note_server_output(heard, launch)
    assert launch not in heard.user_lines
    assert launch not in heard.stderr
    assert r17_outcome(before, before, heard.user_lines, post_quit=False) == LOST_SILENT
    harness.note_server_output(heard, _NOTICE)
    assert r17_outcome(before, before, heard.user_lines, post_quit=False) == (
        LOST_ANNOUNCED
    )


def test_a_server_record_that_mentions_the_channel_is_still_the_servers(monkeypatch):
    # The words ``pw:browser`` in a Chrome path are the server's own INFO
    # record, from ``describe_launch`` through the JSON formatter. The driver
    # writes its channel at the start of the line, after its timestamp.
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setFormatter(MCPJSONFormatter())
    logger = browser_launch.logger
    monkeypatch.setattr(logger, "level", logging.INFO)
    logger.addHandler(handler)
    try:
        browser_launch.describe_launch(
            {"executable_path": ("/Applications/Chrome pw:browser stable.app/Chrome")}
        )
    finally:
        logger.removeHandler(handler)
    line = captured.getvalue().strip()
    assert json.loads(line)["message"].startswith("Using custom Chrome path: ")
    heard = SimpleNamespace(stderr=[], user_lines=[])
    harness.note_server_output(heard, line)
    assert harness.driver_diagnostic(line) is False
    assert heard.stderr == [line]
    assert heard.user_lines == [line]


#: The product's own success lines, as it prints or logs them.
_SUCCESS_LINES = [
    "The sign-in finished",
    "Signed in; running the call again",
    "Signed in; not repeating a call that could change something",
    "Another client already signed in; using its session",
    "   Another client already signed in; using its session.",
    "Another client already signed in; keeping its session",
    "Another LinkedIn MCP client has already signed in. Retry this tool to use "
    "its session.",
    "Manual login completed successfully",
]

_NOTICE = "Session expired or invalid. Run with --login to re-authenticate"


def _lost(profile: Path, staged: StagedSession):
    before = _read(profile, staged)
    portable_cookie_path(profile).unlink()
    return before, _read(profile, staged)


@pytest.mark.parametrize("line", _SUCCESS_LINES)
def test_a_success_line_does_not_announce_a_loss(signed_in, line):
    before, after = _lost(*signed_in)
    assert r17_outcome(before, after, [line], post_quit=False) == LOST_SILENT


@pytest.mark.parametrize("line", _SUCCESS_LINES)
def test_a_success_after_the_original_notice_keeps_the_loss(signed_in, line):
    # A repair after the original session was lost and the caller told: the
    # session the row staged is still gone, and the caller was still told.
    before, after = _lost(*signed_in)
    outcome = r17_outcome(before, after, [_NOTICE, line], post_quit=True)
    assert outcome == LOST_ANNOUNCED


@pytest.mark.parametrize(
    ("output", "outcome"),
    [
        pytest.param([Shown(ROW, CALLER, _NOTICE)], LOST_ANNOUNCED, id="caller"),
        pytest.param(
            [Shown(PRESERVATION, PROBE, _NOTICE)], LOST_SILENT, id="probe-after"
        ),
        pytest.param(
            [Shown(PRESERVATION, CALLER, _NOTICE)], LOST_SILENT, id="caller-after"
        ),
        pytest.param([Shown(ROW, "host-b", _NOTICE)], LOST_SILENT, id="other-host"),
    ],
)
def test_only_the_callers_own_notice_during_the_row_announces(
    signed_in, output, outcome
):
    before, after = _lost(*signed_in)
    assert r17_outcome(before, after, output, post_quit=False) == outcome


def test_the_synthetic_feed_carries_a_permalink_the_feed_extractor_reads():
    page = _FEED_PAGE.decode()
    slugs = [match.group("slug") for match in POST_SLUG_URL_RE.finditer(page)]
    assert slugs == [SYNTHETIC_POST_URL.rsplit("/", 1)[1]]
    assert POST_MARKER in page


def test_the_origin_keeps_cookie_names_and_reads_values_only_to_compare():
    header = 'li_at=secret-value; JSESSIONID="ajax:1=2"; lang=v=2&lang=en-us'
    assert cookie_names(header) == ("JSESSIONID", "lang", "li_at")
    assert cookie_names(None) == ()
    assert cookie_values(header, "li_at") == ["secret-value"]
    assert cookie_values(header, "missing") == []
