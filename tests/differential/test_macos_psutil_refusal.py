"""psutil 7.2.2's macOS refusal is read as the refusal it is (#1216).

psutil 7.2.2 on macOS raises a refused ``sysctl(KERN_PROCARGS2)`` that reports
errno 0 as a ``SystemError`` caused by a ``PermissionError``
(giampaolo/psutil#2854). Every caller that reads another process's command
line or environment reaches the verdict it reaches for ``psutil.AccessDenied``:
a possible browser stays unknown, a marker read is asked again, a census stays
incomplete and the profile not shown empty. Any other ``SystemError`` still
stops it.

When #1216 removes ``read_arguments``, the ``access-denied`` cases stay as
guards of each caller's verdict on a refusal, next to that caller; the
``macos-system-error`` cases and the conversion tests retire with it.
"""

from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path
from typing import Any

import psutil
import pytest

from differential import harness
from differential.profile_commands import COMMAND_MARKER_ENV, TerminalCommand
from differential.test_row_judgement import OTHER, _judged_failed_read, _stage
from differential.test_signals import _crashpad_table
from differential.test_watcher import _observe, _row_table, _sampler
from differential.test_unconfirmed_close import MARKER, _records
from differential.unconfirmed_close import launch_marker
from differential.watcher import (
    BROWSER_MARKER_ENV,
    Tracker,
    canonical_user_data_dir,
    read_arguments,
    read_launcher,
)


def _macos_refusal(_pid: int = 0, function: str = "proc_cmdline") -> SystemError:
    """The exception psutil 7.2.2 raises, chained as CPython chains it: the
    ``PermissionError`` is both its cause and its context (measured)."""
    cause = PermissionError(
        errno.EACCES, "(originated from sysctl(KERN_PROCARGS2) -> errno 0)"
    )
    error = SystemError(
        f"<built-in function {function}> returned a result with an exception set"
    )
    error.__cause__ = cause
    error.__context__ = cause
    return error


def _defect(_pid: int = 0) -> SystemError:
    """A ``SystemError`` that is no refusal: an extension defect."""
    return SystemError("<built-in function proc_cmdline> returned NULL without error")


def _refusals():
    return pytest.mark.parametrize(
        "refuse",
        [
            pytest.param(psutil.AccessDenied, id="access-denied"),
            pytest.param(_macos_refusal, id="macos-system-error"),
        ],
    )


# --- The conversion itself -------------------------------------------------------


class _Reader:
    """A process with a name psutil already knows, whose reads raise."""

    def __init__(self, error: BaseException):
        self.pid, self._name, self._error = 70, "chrome", error

    def cmdline(self):
        raise self._error

    def environ(self):
        raise self._error


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
@pytest.mark.parametrize("field", ["cmdline", "environ"])
def test_the_macos_refusal_is_access_denied_for_the_same_process(link, field):
    error = SystemError("returned a result with an exception set")
    setattr(error, link, PermissionError(errno.EACCES, "denied"))
    with pytest.raises(psutil.AccessDenied) as raised:
        read_arguments(_Reader(error), field)
    assert (raised.value.pid, raised.value.name) == (70, "chrome")
    assert raised.value.__cause__ is error


@pytest.mark.parametrize("field", ["cmdline", "environ"])
def test_a_system_error_without_a_cause_is_raised_unchanged(field):
    error = _defect()
    with pytest.raises(SystemError) as raised:
        read_arguments(_Reader(error), field)
    assert raised.value is error


def _guessing_exe(monkeypatch, refuse, *, native: str):
    """This process, as psutil's own ``Process.exe()`` sees it when the native
    executable lookup is refused or empty: it falls back to the command line,
    and that read is the refused one."""
    process = psutil.Process(os.getpid())
    process._exe = None  # psutil caches a read executable on the instance

    platform = type(process._proc)
    real_exe = platform.exe

    def native_exe(self):
        if self.pid != process.pid:
            return real_exe(self)
        if native == "denied":
            raise psutil.AccessDenied(self.pid)
        return ""

    def refused_cmdline():
        raise refuse(process.pid)

    # The platform class has slots, so the lookup is replaced on the class and
    # answers differently only for this one process.
    monkeypatch.setattr(platform, "exe", native_exe)
    monkeypatch.setattr(process, "cmdline", refused_cmdline)
    return process


@_refusals()
def test_an_executable_guessed_from_a_refused_command_line_is_refused(
    monkeypatch, refuse
):
    # An empty native answer is psutil's own business: it reads a refused
    # guess as an empty executable, and a SystemError cannot be resumed into
    # that, so it stays a refusal. The census below holds both to one verdict.
    process = _guessing_exe(monkeypatch, refuse, native="denied")
    with pytest.raises(psutil.AccessDenied):
        read_arguments(process, "exe")


@_refusals()
@pytest.mark.parametrize("native", ["denied", "empty"])
def test_a_watcher_whose_executable_guess_is_refused_records_the_refusal(
    monkeypatch, refuse, native
):
    process = _guessing_exe(monkeypatch, refuse, native=native)
    _ppid, _exe, cmdline, failures, _read = _sampler([])._read(process, None)
    assert cmdline == () and "cmdline: AccessDenied" in failures


@_refusals()
@pytest.mark.parametrize("native", ["denied", "empty"])
def test_a_census_whose_executable_guess_is_refused_stays_unresolved(
    monkeypatch, tmp_path, refuse, native
):
    process = _guessing_exe(monkeypatch, refuse, native=native)
    monkeypatch.setattr(psutil, "process_iter", lambda *_a, **_k: iter([process]))
    account, browsers, _chrome = _scene(tmp_path)
    census = harness.profile_census(account, browser_dir=browsers)
    assert census.unresolved == [process.pid] and not census.complete


@pytest.mark.parametrize(
    "cause",
    [OSError(errno.EIO, "input/output error"), ValueError("bad buffer")],
    ids=["oserror", "valueerror"],
)
def test_a_system_error_caused_by_anything_but_a_refusal_is_raised(cause):
    error = SystemError("returned a result with an exception set")
    error.__cause__ = cause
    with pytest.raises(SystemError) as raised:
        read_arguments(_Reader(error), "cmdline")
    assert raised.value is error


# --- The watcher -----------------------------------------------------------------


@pytest.fixture
def staged(tmp_path):
    """The row's staged profile, as ``judge_row`` reads it."""
    return _stage(tmp_path / "auth" / "profile")


@_refusals()
def test_an_unreadable_new_possible_root_leaves_o1_unknown(staged, refuse):
    census, vector, failures = _judged_failed_read(staged, "never-read", refuse=refuse)
    (episode,) = census["relevant_read_failures"]
    assert episode["pid"] == 73 and episode["failures"] == ["cmdline: AccessDenied"]
    assert not vector.o1_single_browser and not vector.watcher_healthy
    assert failures


@_refusals()
def test_a_known_roots_refused_read_is_retained_on_its_profile(staged, refuse):
    census, vector, failures = _judged_failed_read(staged, "known-root", refuse=refuse)
    assert failures == []
    assert vector.o1_single_browser and vector.watcher_healthy
    (note,) = census["read_failures"]
    assert note["resolution"] == "a known browser root's earlier reading retained"


@_refusals()
def test_a_root_retained_on_another_profile_stays_unknown_for_the_row(staged, refuse):
    census, vector, failures = _judged_failed_read(
        staged, "other-profile-hidden", refuse=refuse
    )
    (note,) = census["read_failures"]
    assert note["retained_profile"] == canonical_user_data_dir(OTHER)
    assert not vector.o1_single_browser and not vector.watcher_healthy
    assert any("possible browser" in failure for failure in failures)


@_refusals()
def test_a_failed_marker_read_is_asked_again_and_then_found(refuse):
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table.update(_crashpad_table(refuse(60)))
    _observe(sampler, tracker, 1.0)
    assert sampler.sample()[60].browser_marker is None
    table[60]["environ"] = {BROWSER_MARKER_ENV: "d" * 64}
    assert sampler.sample()[60].browser_marker is not None


def test_an_unrelated_system_error_still_stops_the_watcher():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table[2] = {"start": 2.0, "ppid": 1, "cmdline": _defect(2)}
    with pytest.raises(SystemError):
        sampler.sample()


def test_an_unrelated_system_error_still_stops_a_marker_read():
    table = _row_table()
    sampler, tracker = _sampler(table), Tracker()
    _observe(sampler, tracker, 0.0)
    table.update(_crashpad_table(_defect(60)))
    with pytest.raises(SystemError):
        sampler.sample()


def test_a_refused_launcher_read_leaves_no_launcher_and_a_defect_raises():
    assert read_launcher(_Reader(_macos_refusal(70, "proc_environ"))) is None
    with pytest.raises(SystemError):
        read_launcher(_Reader(_defect()))


# --- The preservation census and the wait for no browser -------------------------


ME = "harness-user"


class _Listed:
    """A process as a whole-table scan lists it; any field may raise."""

    def __init__(self, pid: int, *, cmdline: Any, exe: Any, environ: Any = None):
        self.pid, self._name, self.user = pid, None, ME
        self._fields = {
            "cmdline": cmdline,
            "exe": exe,
            "environ": environ if environ is not None else {},
            "status": psutil.STATUS_RUNNING,
            "create_time": float(pid),
        }

    def _read(self, name: str) -> Any:
        value = self._fields[name]
        if isinstance(value, BaseException):
            raise value
        return value

    def oneshot(self):
        return contextlib.nullcontext()

    def cmdline(self):
        return self._read("cmdline")

    def exe(self):
        return self._read("exe")

    def environ(self):
        return self._read("environ")

    def status(self):
        return self._read("status")

    def create_time(self):
        return self._read("create_time")


@pytest.fixture
def table(monkeypatch):
    """The machine's process table, as the census and the wait list it."""
    processes: list[_Listed] = []
    monkeypatch.setattr(harness.psutil, "process_iter", lambda *a, **k: processes)
    monkeypatch.setattr(harness, "harness_user", lambda: ME)
    monkeypatch.setattr(harness, "process_user", lambda process: process.user)
    return processes


def _scene(tmp_path: Path) -> tuple[Any, Path, str]:
    account = harness.ActorAccount(tmp_path / "auth" / "profile")
    browsers = tmp_path / "browsers"
    return account, browsers, str(browsers / "chromium" / "chrome")


@_refusals()
def test_a_refused_census_read_leaves_a_possible_browser_unresolved(
    table, tmp_path, refuse
):
    account, browsers, chrome = _scene(tmp_path)
    table.extend(
        [
            _Listed(70, cmdline=refuse(70), exe=chrome),
            # Listed after the refusal: the census still reaches it.
            _Listed(
                71, cmdline=[chrome, f"--user-data-dir={account.profile}"], exe=chrome
            ),
        ]
    )
    census = harness.profile_census(account, browser_dir=browsers)
    assert census.unresolved == [70] and not census.complete
    assert census.pids == [71]


@_refusals()
def test_a_refused_census_read_of_no_possible_browser_is_settled_by_its_exe(
    table, tmp_path, refuse
):
    account, browsers, _chrome = _scene(tmp_path)
    table.append(_Listed(70, cmdline=refuse(70), exe="/usr/bin/login"))
    census = harness.profile_census(account, browser_dir=browsers)
    assert census.complete and census.pids == []


@_refusals()
def test_an_incomplete_empty_census_is_not_a_drained_profile(table, tmp_path, refuse):
    account, browsers, chrome = _scene(tmp_path)
    table.append(_Listed(70, cmdline=refuse(70), exe=chrome))
    assert harness.wait_for_no_browser(account, 0.0, browser_dir=browsers) == [70]
    table.clear()
    assert harness.wait_for_no_browser(account, 0.0, browser_dir=browsers) == []


def test_an_unrelated_system_error_still_stops_the_census(table, tmp_path):
    account, browsers, chrome = _scene(tmp_path)
    table.append(_Listed(70, cmdline=_defect(70), exe=chrome))
    with pytest.raises(SystemError):
        harness.profile_census(account, browser_dir=browsers)


# --- A profile command's marked processes ----------------------------------------


def _command(tmp_path: Path) -> TerminalCommand:
    return TerminalCommand(
        argv=["true"], args=[], env={}, cwd=tmp_path, terminal=False, label="probe"
    )


@_refusals()
def test_a_refused_environment_is_skipped_and_the_scan_goes_on(
    monkeypatch, tmp_path, refuse
):
    command = _command(tmp_path)
    marked = _Listed(
        71, cmdline=[], exe="/bin/sleep", environ={COMMAND_MARKER_ENV: command.marker}
    )
    processes = [
        _Listed(70, cmdline=[], exe="/bin/sleep", environ=refuse(70)),
        marked,
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(processes))
    command._marked()
    assert list(command.descendants) == [(71, 71.0)]


def test_an_unrelated_system_error_still_stops_the_marked_scan(monkeypatch, tmp_path):
    command = _command(tmp_path)
    processes = [_Listed(70, cmdline=[], exe="/bin/sleep", environ=_defect(70))]
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(processes))
    with pytest.raises(SystemError):
        command._marked()


# --- The launch marker read back from the row's browser --------------------------


class _Browser:
    def __init__(self, environ: Any):
        self._environ = environ
        self.pid, self._name = 200, None

    def create_time(self):
        return 20.0

    def environ(self):
        if isinstance(self._environ, BaseException):
            raise self._environ
        return self._environ


@_refusals()
def test_a_refused_launch_marker_read_is_not_found_until_it_reads(refuse):
    def opener(environ: Any):
        return lambda _pid: _Browser(environ)

    assert (
        launch_marker(_records(), (100, 10.0), open_process=opener(refuse(200))) is None
    )
    found = launch_marker(
        _records(), (100, 10.0), open_process=opener({BROWSER_MARKER_ENV: MARKER})
    )
    assert found is not None and found.value == MARKER


def test_an_unrelated_system_error_still_stops_the_launch_marker_read():
    with pytest.raises(SystemError):
        launch_marker(
            _records(),
            (100, 10.0),
            open_process=lambda _pid: _Browser(_defect(200)),
        )
