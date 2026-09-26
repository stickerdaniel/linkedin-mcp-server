"""R17: the snapshot reads the four artefacts, and the outcome follows from them.

Real files written by the product's own writers, no browser. Each case changes
one artefact the way a failure would and checks the outcome it produces.
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
    announces_session,
    r17_outcome,
    snapshot,
    write_synthetic_cookie_file,
)
from differential.synthetic_origin import (
    POST_MARKER,
    SYNTHETIC_POST_URL,
    _FEED_PAGE,
    cookie_names,
)
from linkedin_mcp_server.scraping.feed_payload import POST_SLUG_URL_RE
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    portable_cookie_path,
    source_state_path,
    write_source_state,
)


@pytest.fixture
def signed_in(tmp_path) -> Path:
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    (profile / LAST_VERSION_FILE).write_text("153.0.8010.12")
    write_synthetic_cookie_file(portable_cookie_path(profile))
    write_source_state(profile)
    return profile


def test_a_staged_session_reads_as_one(signed_in):
    before = snapshot(signed_in)
    assert before.has_session
    assert "li_at" in before.cookie_names
    assert before.last_version == "153.0.8010.12"
    assert before.quarantine == ()
    assert before.unreadable == ()


def test_the_snapshot_never_carries_a_cookie_value(signed_in):
    values = [
        entry["value"]
        for entry in json.loads(portable_cookie_path(signed_in).read_text())
    ]
    serialised = json.dumps(snapshot(signed_in).as_event_fields())
    assert values and not any(value in serialised for value in values)


def test_nothing_changed_is_retained(signed_in):
    before = snapshot(signed_in)
    assert r17_outcome(before, snapshot(signed_in), []) == RETAINED


def test_a_refreshed_cookie_file_is_still_retained(signed_in):
    before = snapshot(signed_in)
    write_synthetic_cookie_file(portable_cookie_path(signed_in))
    after = snapshot(signed_in)
    assert after.cookies_sha256 != before.cookies_sha256
    assert r17_outcome(before, after, []) == RETAINED


def test_a_lost_cookie_file_nobody_mentions_is_lost_silently(signed_in):
    before = snapshot(signed_in)
    portable_cookie_path(signed_in).unlink()
    assert r17_outcome(before, snapshot(signed_in), ["Browser closed"]) == LOST_SILENT


def test_a_lost_cookie_file_the_user_was_told_about_is_announced(signed_in):
    before = snapshot(signed_in)
    portable_cookie_path(signed_in).unlink()
    output = ["❌ Session expired or invalid", "   Run with --login to re-authenticate"]
    assert r17_outcome(before, snapshot(signed_in), output) == LOST_ANNOUNCED


def test_a_cookie_file_without_li_at_is_a_loss(signed_in):
    before = snapshot(signed_in)
    path = portable_cookie_path(signed_in)
    entries = [e for e in json.loads(path.read_text()) if e["name"] != "li_at"]
    path.write_text(json.dumps(entries))
    assert r17_outcome(before, snapshot(signed_in), []) == LOST_SILENT


def test_a_new_quarantine_is_a_loss(signed_in):
    before = snapshot(signed_in)
    (signed_in.parent / f"{QUARANTINE_PREFIX}20260926T000000").mkdir()
    assert r17_outcome(before, snapshot(signed_in), []) == LOST_SILENT


def test_a_moved_generation_is_a_loss(signed_in):
    before = snapshot(signed_in)
    write_source_state(signed_in)
    assert r17_outcome(before, snapshot(signed_in), []) == LOST_SILENT


def test_a_cleared_session_the_user_asked_for_is_cleared_by_user(signed_in):
    before = snapshot(signed_in)
    shutil.rmtree(signed_in)
    portable_cookie_path(signed_in).unlink()
    source_state_path(signed_in).unlink()
    assert (
        r17_outcome(before, snapshot(signed_in), [], user_cleared=True)
        == CLEARED_BY_USER
    )


def test_an_unreadable_artefact_is_uncertain(signed_in):
    before = snapshot(signed_in)
    portable_cookie_path(signed_in).write_text("{not json")
    after = snapshot(signed_in)
    assert after.unreadable
    assert r17_outcome(before, after, []) == UNCERTAIN


def test_no_session_to_begin_with_is_uncertain(tmp_path):
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True)
    empty = snapshot(profile)
    assert not empty.has_session
    assert r17_outcome(empty, empty, []) == UNCERTAIN


@pytest.mark.parametrize(
    ("line", "announces"),
    [
        ("Run with --login to create a profile.", True),
        ("Sign in to LinkedIn again", True),
        ("Session expired or invalid.", True),
        ('{"message": "Processing request of type CallToolRequest"}', False),
        ("Stdio transport session started", False),
        ("Browser closed", False),
    ],
)
def test_only_a_line_about_the_session_announces_its_loss(line, announces):
    assert announces_session([line]) is announces


def test_the_synthetic_feed_carries_a_permalink_the_feed_extractor_reads():
    page = _FEED_PAGE.decode()
    slugs = [match.group("slug") for match in POST_SLUG_URL_RE.finditer(page)]
    assert slugs == [SYNTHETIC_POST_URL.rsplit("/", 1)[1]]
    assert POST_MARKER in page


def test_the_origin_keeps_cookie_names_and_drops_values():
    header = 'li_at=secret-value; JSESSIONID="ajax:1=2"; lang=v=2&lang=en-us'
    assert cookie_names(header) == ("JSESSIONID", "lang", "li_at")
    assert cookie_names(None) == ()
