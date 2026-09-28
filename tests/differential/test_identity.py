"""A row records what it runs, and refuses a runtime it cannot name.

The record is taken from this very checkout, so the first case is a real
reading; the refusals are judged on modelled records.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from differential.harness import REPO_ROOT, evidence_refusal, row_identity


def _record(**changes):
    record = {
        "direct_url": {"url": REPO_ROOT.as_uri(), "dir_info": {"editable": True}},
        "checkout": str(REPO_ROOT),
        "head": "0" * 40,
        "porcelain_empty": True,
        "dirty_paths": [],
    }
    record.update(changes)
    return record


def test_the_record_names_this_interpreter_checkout_and_lock():
    identity = row_identity()
    assert identity["sys_executable"] == sys.executable
    assert identity["checkout"] == str(REPO_ROOT)
    assert isinstance(identity["head"], str) and len(identity["head"]) == 40
    assert isinstance(identity["uv_lock_sha256"], str)
    assert isinstance(identity["porcelain_empty"], bool)
    # The suite runs from an editable install of this checkout.
    assert evidence_refusal(identity, ci=False) is None


def test_a_stray_distribution_earlier_on_the_path_is_not_read(tmp_path, monkeypatch):
    # What a build left in a checkout looks like to importlib: metadata for the
    # same name, found first, with no direct_url.json.
    stray = tmp_path / "mcp_server_linkedin-0.0.0.dist-info"
    stray.mkdir()
    (stray / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: mcp-server-linkedin\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    identity = row_identity()
    assert identity["installed_distributions"] == 1
    assert evidence_refusal(identity, ci=False) is None


def test_a_clean_editable_install_of_this_checkout_is_accepted():
    assert evidence_refusal(_record(), ci=True) is None


@pytest.mark.parametrize(
    ("changes", "ci", "reported"),
    [
        ({"porcelain_empty": False, "dirty_paths": [" M x.py"]}, True, "not clean"),
        ({"porcelain_empty": None}, True, "not clean"),
        ({"direct_url": None}, False, "not an editable install"),
        (
            {"direct_url": {"url": "https://files.pythonhosted.org/x.whl"}},
            False,
            "not an editable install",
        ),
        (
            {
                "direct_url": {
                    "url": Path(REPO_ROOT.parent).as_uri(),
                    "dir_info": {"editable": True},
                }
            },
            False,
            "not from the checkout",
        ),
        ({"head": None}, False, "HEAD"),
    ],
)
def test_a_runtime_that_is_not_this_checkout_is_refused(changes, ci, reported):
    refusal = evidence_refusal(_record(**changes), ci=ci)
    assert refusal is not None and reported in refusal


def test_a_dirty_checkout_is_only_refused_in_ci():
    dirty = _record(porcelain_empty=False, dirty_paths=[" M x.py"])
    assert evidence_refusal(dirty, ci=False) is None
