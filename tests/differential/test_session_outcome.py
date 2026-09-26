"""R17: the snapshot reads the artefacts, and the outcome follows from them.

Real files written by the product's own writers, no browser. Each case changes
one artefact the way a failure would and checks the outcome it produces. Only
the unchanged session with a post-quit observation that saw it accepted may
read as retained.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from differential.session import (
    CLEARED_BY_USER,
    LAST_VERSION_FILE,
    LOST_ANNOUNCED,
    LOST_SILENT,
    RETAINED,
    UNCERTAIN,
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
from linkedin_mcp_server.scraping.feed_payload import POST_SLUG_URL_RE
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    portable_cookie_path,
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
        # Order matters: a notice answered by a sign-in is no longer news.
        (["Session expired or invalid.", "Successfully signed in"], False),
        (["Successfully signed in", "Session expired or invalid."], True),
        (['{"message": "Processing request of type CallToolRequest"}'], False),
        (["Stdio transport session started"], False),
        (["Browser closed"], False),
    ],
)
def test_only_an_unanswered_notice_announces_a_loss(lines, announced):
    assert announces_loss(lines) is announced


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
