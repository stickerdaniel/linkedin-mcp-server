"""The suite's own check that no test leaves daemon state under the real root.

A check that never fires looks exactly like a suite that never leaks, so these
run it where it has to fire. Each case runs a separate pytest process over a
small generated suite with ``tests/conftest.py`` loaded, its real root swapped
for one under ``pytester``'s directory. The leak is what an owner that lost the
temporary account home does: ``prepare_daemon_state`` under the account's home,
keyed by an auth root in the test's ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.private_state import harden_directory

pytest_plugins = ["pytester"]

TESTS_DIR = Path(__file__).resolve().parent

_SUITE = """
import os
from pathlib import Path

import pytest

from linkedin_mcp_server.daemon_descriptor import (
    TEST_ACCOUNT_HOME_ENV,
    daemon_dir,
    prepare_daemon_state,
)

REAL_HOME = {real_home!r}
LEAKED = Path({leaked!r})


@pytest.fixture(scope="session")
def real_daemon_state_root():
    return Path({real_root!r})


def test_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv(TEST_ACCOUNT_HOME_ENV, REAL_HOME)
    auth_root = tmp_path / "auth"
    prepare_daemon_state(auth_root)
    LEAKED.write_text(str(daemon_dir(auth_root)))


def test_leaks_and_replaces_the_filesystem(tmp_path, monkeypatch):
    # Still in force when the guard reads the keys, as with the tests in this
    # suite that replace these.
    monkeypatch.setenv(TEST_ACCOUNT_HOME_ENV, REAL_HOME)
    prepare_daemon_state(tmp_path / "auth")

    def refused(*args, **kwargs):
        raise PermissionError("refused")

    monkeypatch.setattr(os, "walk", refused)
    monkeypatch.setattr(os, "scandir", refused)
    monkeypatch.setattr(Path, "exists", lambda self, **kwargs: False)


def test_stays_in_its_own_home(tmp_path):
    prepare_daemon_state(tmp_path / "auth")


def test_inherits_stale_state(tmp_path):
    # The plugin below wrote this directory's state before the test started.
    pass
"""

#: Loaded ahead of ``tests/conftest.py``, so its autouse fixture runs before the
#: guard records the real root: state an earlier run left under the key this
#: test's ``tmp_path`` happens to get, as a reused inode would.
_STALE = """
import pytest

from linkedin_mcp_server.daemon_descriptor import TEST_ACCOUNT_HOME_ENV, prepare_daemon_state


@pytest.fixture(autouse=True)
def stale_state(request, tmp_path):
    if request.node.name == "test_inherits_stale_state":
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv(TEST_ACCOUNT_HOME_ENV, {real_home!r})
            prepare_daemon_state(tmp_path)
"""


@pytest.mark.parametrize("retention", ["all", "failed"])
def test_only_the_leaking_tests_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, retention: str
):
    real_home = pytester.path / "real-home"
    harden_directory(real_home)
    monkeypatch.setenv(daemon_descriptor.TEST_ACCOUNT_HOME_ENV, str(real_home))
    real_root = daemon_descriptor.daemon_state_root()
    leaked = pytester.path / "leaked.txt"
    values = {
        "real_home": str(real_home),
        "real_root": str(real_root),
        "leaked": str(leaked),
    }
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))
    pytester.makeini("[pytest]\n")
    pytester.makepyfile(
        stale_plugin=_STALE.format(**values), test_suite=_SUITE.format(**values)
    )

    result = pytester.runpytest_subprocess(
        "-p",
        "stale_plugin",
        "-p",
        "conftest",
        "-p",
        "no:cacheprovider",
        "-o",
        f"tmp_path_retention_policy={retention}",
    )

    result.assert_outcomes(passed=4, errors=2)
    errors = [
        line.split(" - ")[0] for line in result.outlines if line.startswith("ERROR ")
    ]
    assert errors == [
        "ERROR test_suite.py::test_leaks",
        "ERROR test_suite.py::test_leaks_and_replaces_the_filesystem",
    ], result.outlines
    message = f"wrote daemon state under the real {real_root}: {leaked.read_text()}."
    assert message in result.stdout.str()
