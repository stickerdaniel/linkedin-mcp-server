"""Linux evidence for identity-stable process-group signaling.

This is deliberately a test-only probe. Issue #809 does not yet change the
process cleanup backend.
"""

from __future__ import annotations

import errno
import json
import os
import platform
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="pidfd process-group signaling is a Linux facility",
)

# Linux UAPI: include/uapi/linux/pidfd.h in torvalds/linux.
# https://github.com/torvalds/linux/blob/master/include/uapi/linux/pidfd.h
_PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2
_UNSUPPORTED_PIDFD_FLAG = 1 << 30
_PidfdOpen = Callable[[int], int]
_PidfdSender = Callable[[int, int, None, int], None]
_pidfd_open = cast(_PidfdOpen | None, getattr(os, "pidfd_open", None))
_pidfd_send_signal = cast(
    _PidfdSender | None,
    getattr(signal, "pidfd_send_signal", None),
)
_UNAVAILABLE_ERRNOS = {errno.ENOSYS, errno.EPERM}

_SUBREAPER_PROBE = rf"""
import ctypes
import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PIDFD_SIGNAL_PROCESS_GROUP = {_PIDFD_SIGNAL_PROCESS_GROUP}
PR_SET_CHILD_SUBREAPER = 36
UNAVAILABLE = {{errno.ENOSYS, errno.EPERM}}

pidfd_open = getattr(os, "pidfd_open", None)
raw_pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
if os.environ.get("LINKEDIN_MCP_TEST_PIDFD_NO_API") == "1":
    pidfd_open = None
if pidfd_open is None or raw_pidfd_send_signal is None:
    print(json.dumps({{"status": "skip", "reason": "Python has no pidfd API", "spawned": False, "cleanup": "not spawned"}}))
    raise SystemExit(0)

pidfd_signal_calls = []


def pidfd_send_signal(pidfd, sent, info, flags):
    pidfd_signal_calls.append((pidfd, sent, flags))
    return raw_pidfd_send_signal(pidfd, sent, info, flags)


libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))

member_code = r'''
import os
import signal
import sys
import time
from pathlib import Path

ready = Path(sys.argv[1])
signaled = Path(sys.argv[2])

def handle(_signum, _frame):
    signaled.write_text(str(os.getpid()))
    raise SystemExit(0)

signal.signal(signal.SIGUSR1, handle)
ready.write_text(str(os.getpid()))
while True:
    time.sleep(60)
'''
leader_code = r'''
import subprocess
import sys

member = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(member.pid, flush=True)
command = sys.stdin.readline()
if command != "release\n":
    member.terminate()
    member.wait(timeout=10)
    print(f"cleaned {{member.pid}}", flush=True)
'''


class ProbeSkipped(Exception):
    pass


def unavailable(exc):
    return isinstance(exc, OSError) and exc.errno in UNAVAILABLE


member_open_fault = os.environ.get("LINKEDIN_MCP_TEST_PIDFD_MEMBER_OPEN_FAULT")


def open_pidfd(pid, role):
    try:
        if role == "member" and member_open_fault:
            injected = getattr(errno, member_open_fault)
            raise OSError(injected, os.strerror(injected))
        return pidfd_open(pid)
    except OSError as exc:
        if unavailable(exc):
            raise ProbeSkipped(f"pidfd_open {{role}}: {{exc}}") from exc
        raise


group_fault = os.environ.get("LINKEDIN_MCP_TEST_PIDFD_GROUP_FAULT")
group_fault_used = False


def send(pidfd, sent, flags):
    global group_fault_used
    try:
        if flags == PIDFD_SIGNAL_PROCESS_GROUP and group_fault and not group_fault_used:
            group_fault_used = True
            injected = getattr(errno, group_fault)
            raise OSError(injected, os.strerror(injected))
        if flags == 0 and sent == 0 and os.environ.get("LINKEDIN_MCP_TEST_PIDFD_PREFLIGHT_FAULT"):
            injected = getattr(errno, os.environ["LINKEDIN_MCP_TEST_PIDFD_PREFLIGHT_FAULT"])
            raise OSError(injected, os.strerror(injected))
        pidfd_send_signal(pidfd, sent, None, flags)
    except OSError as exc:
        if unavailable(exc):
            return "unavailable"
        if exc.errno == errno.ESRCH:
            return "gone"
        if flags == PIDFD_SIGNAL_PROCESS_GROUP and exc.errno == errno.EINVAL:
            return "unsupported"
        raise
    return "sent"


def wait_for(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            value = path.read_text()
        except FileNotFoundError:
            time.sleep(0.01)
            continue
        if value:
            return int(value)
    raise RuntimeError(f"timed out waiting for {{path.name}}")


try:
    preflight_pidfd = open_pidfd(os.getpid(), "preflight")
except ProbeSkipped as exc:
    print(json.dumps({{"status": "skip", "reason": str(exc), "spawned": False, "cleanup": "not spawned"}}))
    raise SystemExit(0)
try:
    preflight = send(preflight_pidfd, 0, 0)
    if preflight == "unavailable":
        print(json.dumps({{"status": "skip", "reason": "pidfd signal unavailable", "spawned": False, "cleanup": "not spawned"}}))
        raise SystemExit(0)
    if preflight != "sent":
        raise RuntimeError("the current process disappeared during pidfd preflight")
finally:
    os.close(preflight_pidfd)


leader = None
leader_pidfd = -1
member_pid = -1
member_pidfd = -1
reaped_leader = False
reaped_member = False
leader_cleanup_confirmed = False
closed_fds = []
report = None
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ready = root / "member-ready"
        signaled = root / "member-signaled"
        leader = subprocess.Popen(
            [sys.executable, "-c", leader_code, member_code, str(ready), str(signaled)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        member_pid = int(leader.stdout.readline())
        if wait_for(ready) != member_pid:
            raise RuntimeError("the ready marker named another process")
        if leader.poll() is not None:
            raise RuntimeError("the leader exited before attribution")
        if os.getpgid(leader.pid) != leader.pid:
            raise RuntimeError("the leader does not own its process group")
        if os.getpgid(member_pid) != leader.pid:
            raise RuntimeError("the member is outside the leader's group")

        leader_pidfd = open_pidfd(leader.pid, "leader")
        member_pidfd = open_pidfd(member_pid, "member")
        support = send(leader_pidfd, 0, PIDFD_SIGNAL_PROCESS_GROUP)
        if support == "unsupported":
            raise ProbeSkipped("group flag unsupported")
        if support == "unavailable":
            raise ProbeSkipped("group signal unavailable")
        if send(member_pidfd, 0, PIDFD_SIGNAL_PROCESS_GROUP) != "gone":
            raise RuntimeError("a member pidfd was accepted as group attribution")

        leader.stdin.write("release\n")
        leader.stdin.flush()
        if leader.wait(timeout=10) != 0:
            raise RuntimeError("the leader did not exit cleanly")
        reaped_leader = True
        if os.getpgid(member_pid) != leader.pid:
            raise RuntimeError("the group changed after leader reaping")

        if send(leader_pidfd, signal.SIGUSR1, PIDFD_SIGNAL_PROCESS_GROUP) != "sent":
            raise RuntimeError("the retained leader pidfd did not signal its group")
        if wait_for(signaled) != member_pid:
            raise RuntimeError("another process handled the group signal")

        waited, status = os.waitpid(member_pid, 0)
        reaped_member = True
        if waited != member_pid or os.waitstatus_to_exitcode(status) != 0:
            raise RuntimeError("the subreaper did not collect the signaled member")
        if send(leader_pidfd, 0, PIDFD_SIGNAL_PROCESS_GROUP) != "gone":
            raise RuntimeError("the reaped empty group did not return ESRCH")

        report = {{"status": "passed"}}
except ProbeSkipped as exc:
    report = {{"status": "skip", "reason": str(exc)}}
finally:
    if leader is not None and leader.poll() is None:
        if member_pid >= 0 and member_pidfd < 0:
            cleanup_mode = os.environ.get(
                "LINKEDIN_MCP_TEST_PIDFD_LEADER_CLEANUP", "command"
            )
            if cleanup_mode == "eof":
                leader.stdin.close()
            else:
                leader.stdin.write("cleanup\n")
                leader.stdin.flush()
            confirmation = leader.stdout.readline().strip()
            leader_cleanup_confirmed = confirmation == f"cleaned {{member_pid}}"
            if not leader_cleanup_confirmed:
                raise RuntimeError("the leader did not confirm member cleanup")
            reaped_member = True
        elif leader_pidfd >= 0:
            try:
                pidfd_send_signal(leader_pidfd, signal.SIGKILL, None, 0)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    raise
        leader.wait(timeout=10)
        reaped_leader = True
    elif leader is not None:
        reaped_leader = True
    if member_pidfd >= 0 and not reaped_member:
        try:
            pidfd_send_signal(member_pidfd, signal.SIGKILL, None, 0)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise
        try:
            waited, _status = os.waitpid(member_pid, 0)
            reaped_member = waited == member_pid
        except ChildProcessError:
            pass
    for descriptor in (member_pidfd, leader_pidfd):
        if descriptor < 0:
            continue
        os.close(descriptor)
        try:
            os.fstat(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
            closed_fds.append(descriptor)
        else:
            raise RuntimeError("pidfd remained open")

if leader is not None and not reaped_leader:
    raise RuntimeError("the helper left its leader unreaped")
if member_pid >= 0 and not reaped_member:
    raise RuntimeError("the helper left its member unreaped")
if report is None:
    raise RuntimeError("the helper produced no result")
report.update(
    {{
        "leader": leader.pid if leader is not None else -1,
        "member": member_pid,
        "spawned": True,
        "cleanup": "spawned and fully cleaned" if reaped_leader and reaped_member and len(closed_fds) == (1 if member_pidfd < 0 else 2) else "incomplete",
        "reaped_leader": reaped_leader,
        "reaped_member": reaped_member,
        "leader_cleanup_confirmed": leader_cleanup_confirmed,
        "closed_fds": len(closed_fds),
        "member_cleanup_pidfd_calls": sum(
            call == (member_pidfd, signal.SIGKILL, 0) for call in pidfd_signal_calls
        ),
    }}
)
print(json.dumps(report))
"""


def _require_pidfd_api(
    opener: _PidfdOpen | None = _pidfd_open,
    sender: _PidfdSender | None = _pidfd_send_signal,
) -> tuple[_PidfdOpen, _PidfdSender]:
    if opener is None or sender is None:
        pytest.skip("Python has no pidfd_open and pidfd_send_signal API")
    return opener, sender


def _open_pidfd(pid: int, *, opener: _PidfdOpen | None = _pidfd_open) -> int:
    opener, _sender = _require_pidfd_api(opener, _pidfd_send_signal)
    try:
        return opener(pid)
    except OSError as exc:
        if exc.errno in _UNAVAILABLE_ERRNOS:
            pytest.skip(f"pidfd_open is unavailable: {exc}")
        raise


def _send_group_signal(
    pidfd: int,
    sent: int,
    *,
    sender: _PidfdSender | None = _pidfd_send_signal,
) -> str:
    """Classify non-fatal kernel answers without a numeric fallback."""
    _opener, sender = _require_pidfd_api(_pidfd_open, sender)
    try:
        sender(pidfd, sent, None, _PIDFD_SIGNAL_PROCESS_GROUP)
    except OSError as exc:
        if exc.errno in _UNAVAILABLE_ERRNOS:
            pytest.skip(f"pidfd_send_signal is unavailable: {exc}")
        if exc.errno == errno.EINVAL:
            return "unsupported"
        if exc.errno == errno.ESRCH:
            return "gone"
        raise
    return "sent"


def test_missing_python_pidfd_apis_skip():
    with pytest.raises(pytest.skip.Exception):
        _require_pidfd_api(None, _pidfd_send_signal)
    with pytest.raises(pytest.skip.Exception):
        _require_pidfd_api(_pidfd_open, None)


@pytest.mark.parametrize("failure", [errno.ENOSYS, errno.EPERM])
def test_pidfd_open_platform_refusals_skip(failure: int):
    def unavailable(_pid: int) -> int:
        raise OSError(failure, os.strerror(failure))

    with pytest.raises(pytest.skip.Exception):
        _open_pidfd(17, opener=unavailable)


@pytest.mark.parametrize("failure", [errno.ENOSYS, errno.EPERM])
def test_pidfd_signal_platform_refusals_skip(failure: int):
    def unavailable(_pidfd: int, _sent: int, _info: None, _flags: int) -> None:
        raise OSError(failure, os.strerror(failure))

    with pytest.raises(pytest.skip.Exception):
        _send_group_signal(17, signal.SIGKILL, sender=unavailable)


def test_einval_is_unsupported_without_numeric_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[int, int, None, int]] = []

    def unsupported(pidfd: int, sent: int, info: None, flags: int) -> None:
        calls.append((pidfd, sent, info, flags))
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))

    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: pytest.fail("unsupported pidfd flags fell back to killpg"),
    )

    assert _send_group_signal(17, signal.SIGKILL, sender=unsupported) == "unsupported"
    assert calls == [(17, signal.SIGKILL, None, 4)]


def test_esrch_is_gone_without_numeric_fallback():
    calls: list[tuple[int, int, None, int]] = []

    def gone(pidfd: int, sent: int, info: None, flags: int) -> None:
        calls.append((pidfd, sent, info, flags))
        raise OSError(errno.ESRCH, os.strerror(errno.ESRCH))

    assert _send_group_signal(17, signal.SIGKILL, sender=gone) == "gone"
    assert calls == [(17, signal.SIGKILL, None, 4)]


def test_python_passes_nonzero_pidfd_flags_to_linux():
    pidfd = _open_pidfd(os.getpid())
    _opener, sender = _require_pidfd_api()
    try:
        with pytest.raises(OSError) as raised:
            sender(pidfd, 0, None, _UNSUPPORTED_PIDFD_FLAG)
    finally:
        os.close(pidfd)

    if raised.value.errno in _UNAVAILABLE_ERRNOS:
        pytest.skip(f"pidfd_send_signal is unavailable: {raised.value}")
    assert raised.value.errno == errno.EINVAL


def _run_probe(
    tmp_path: Path,
    fault: str | None = None,
    *,
    member_open_fault: str | None = None,
    leader_cleanup: str = "command",
) -> dict[str, object]:
    environment = os.environ.copy()
    if fault is not None:
        environment["LINKEDIN_MCP_TEST_PIDFD_GROUP_FAULT"] = fault
    if member_open_fault is not None:
        environment["LINKEDIN_MCP_TEST_PIDFD_MEMBER_OPEN_FAULT"] = member_open_fault
        environment["LINKEDIN_MCP_TEST_PIDFD_LEADER_CLEANUP"] = leader_cleanup
    result = subprocess.run(
        [sys.executable, "-c", _SUBREAPER_PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    return cast(dict[str, object], json.loads(result.stdout.splitlines()[-1]))


def _write_probe_summary(status: str, cleanup: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    source = os.environ.get("GITHUB_SHA", "local")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        source = "local"
    runner = os.environ.get("ImageOS", platform.system())
    if not re.fullmatch(r"[A-Za-z0-9._-]+", runner):
        runner = platform.system()
    line = (
        f"pidfd L1 | source={source} | runner={runner} "
        f"| kernel={platform.release()} | arch={platform.machine()} "
        f"| Python={platform.python_version()} | Patchright={version('patchright')} "
        f"| status={status} | cleanup={cleanup}\n"
    )
    with Path(summary).open("a", encoding="utf-8") as output:
        output.write(line)
    print(line, end="")


def _assert_probe_reaped_every_process(
    report: dict[str, object], *, expected_fds: int = 2
) -> None:
    assert report["spawned"] is True
    assert report["cleanup"] == "spawned and fully cleaned"
    assert report["leader"] != report["member"]
    assert report["reaped_leader"] is True
    assert report["reaped_member"] is True
    assert report["closed_fds"] == expected_fds


def test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path: Path):
    status = "ERROR"
    cleanup = "unconfirmed"
    try:
        report = _run_probe(tmp_path)
        cleanup = cast(str, report["cleanup"])
        if report["status"] == "skip":
            if report["spawned"] is True:
                if cast(str, report["reason"]).startswith("pidfd_open member:"):
                    assert report["leader_cleanup_confirmed"] is True
                    _assert_probe_reaped_every_process(report, expected_fds=1)
                else:
                    _assert_probe_reaped_every_process(report)
            else:
                assert report["spawned"] is False
                assert cleanup == "not spawned"
            status = "UNSUPPORTED"
            pytest.skip(cast(str, report["reason"]))
        _assert_probe_reaped_every_process(report)
        assert report["status"] == "passed"
        status = "SUPPORTED_OBSERVATION"
    finally:
        primary_error = sys.exc_info()[0]
        try:
            _write_probe_summary(status, cleanup)
        except Exception:
            if primary_error is not None and not issubclass(
                primary_error, pytest.skip.Exception
            ):
                print("pidfd evidence summary unavailable", file=sys.stderr)
            else:
                raise RuntimeError("pidfd evidence summary unavailable") from None


def test_pidfd_evidence_log_matches_step_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path)

    evidence = summary.read_text()
    assert capsys.readouterr().out == evidence
    assert "status=SUPPORTED_OBSERVATION" in evidence
    assert "cleanup=spawned and fully cleaned" in evidence


def test_member_open_refusal_reports_unsupported_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("LINKEDIN_MCP_TEST_PIDFD_MEMBER_OPEN_FAULT", "EPERM")

    with pytest.raises(pytest.skip.Exception, match="pidfd_open member"):
        test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path)

    evidence = summary.read_text()
    assert "status=UNSUPPORTED" in evidence
    assert "cleanup=spawned and fully cleaned" in evidence


def test_summary_failure_preserves_primary_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def fail_probe(_tmp_path: Path) -> dict[str, object]:
        raise RuntimeError("probe crashed")

    def fail_summary(_status: str, _cleanup: str) -> None:
        raise OSError("private summary path")

    monkeypatch.setattr(sys.modules[__name__], "_run_probe", fail_probe)
    monkeypatch.setattr(sys.modules[__name__], "_write_probe_summary", fail_summary)

    with pytest.raises(RuntimeError, match="^probe crashed$"):
        test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path)
    diagnostic = capsys.readouterr().err
    assert "pidfd evidence summary unavailable" in diagnostic
    assert "private summary path" not in diagnostic


def test_summary_failure_rejects_successful_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing" / "summary"))

    with pytest.raises(RuntimeError, match="^pidfd evidence summary unavailable$"):
        test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path)


def test_summary_failure_rejects_unsupported_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LINKEDIN_MCP_TEST_PIDFD_NO_API", "1")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "missing" / "summary"))

    with pytest.raises(RuntimeError, match="^pidfd evidence summary unavailable$"):
        test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path)


@pytest.mark.parametrize("preflight", ["no_api", "ENOSYS", "EPERM"])
def test_preflight_unavailable_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preflight: str
):
    if preflight == "no_api":
        monkeypatch.setenv("LINKEDIN_MCP_TEST_PIDFD_NO_API", "1")
    else:
        monkeypatch.setenv("LINKEDIN_MCP_TEST_PIDFD_PREFLIGHT_FAULT", preflight)
    report = _run_probe(tmp_path)

    assert report["status"] == "skip"
    assert report["spawned"] is False
    assert report["cleanup"] == "not spawned"
    assert "leader" not in report
    assert "member" not in report


@pytest.mark.parametrize("fault", ["EINVAL", "ENOSYS", "EPERM"])
def test_group_probe_skip_reaps_every_spawned_process(tmp_path: Path, fault: str):
    report = _run_probe(tmp_path, fault)

    assert report["status"] == "skip"
    _assert_probe_reaped_every_process(report)
    assert report["member_cleanup_pidfd_calls"] == 1


@pytest.mark.parametrize("fault", ["ENOSYS", "EPERM"])
@pytest.mark.parametrize("leader_cleanup", ["command", "eof"])
def test_member_pidfd_open_failure_uses_leader_cleanup_contract(
    tmp_path: Path,
    fault: str,
    leader_cleanup: str,
):
    report = _run_probe(
        tmp_path,
        member_open_fault=fault,
        leader_cleanup=leader_cleanup,
    )

    assert report["status"] == "skip"
    assert report["reaped_leader"] is True
    assert report["reaped_member"] is True
    assert report["leader_cleanup_confirmed"] is True
    assert report["closed_fds"] == 1
    assert report["member_cleanup_pidfd_calls"] == 0
