"""Linux evidence for identity-stable process-group signaling.

This is deliberately a test-only probe. Issue #809 does not yet change the
process cleanup backend.
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
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
_PidfdSender = Callable[[int, int, None, int], None]
_pidfd_open = cast(Callable[[int], int], getattr(os, "pidfd_open", None))
_pidfd_send_signal = cast(_PidfdSender, getattr(signal, "pidfd_send_signal", None))

_MEMBER = r"""
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
"""

_LEADER = r"""
import subprocess
import sys

member = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(member.pid, flush=True)
sys.stdin.read(1)
"""


def _send_group_signal(
    pidfd: int,
    sent: int,
    *,
    sender: _PidfdSender | None = None,
) -> str:
    """Classify the two non-fatal kernel answers without a numeric fallback."""
    if sender is None:
        sender = _pidfd_send_signal
    try:
        sender(pidfd, sent, None, _PIDFD_SIGNAL_PROCESS_GROUP)
    except OSError as exc:
        if exc.errno == errno.EINVAL:
            return "unsupported"
        if exc.errno == errno.ESRCH:
            return "gone"
        raise
    return "sent"


def _wait_for(path: Path, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = path.read_text()
        except FileNotFoundError:
            time.sleep(0.01)
            continue
        if value:
            return value
    pytest.fail(f"timed out waiting for {path.name}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_process_group_flag_matches_the_linux_uapi():
    assert _PIDFD_SIGNAL_PROCESS_GROUP == 4


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
    pidfd = _pidfd_open(os.getpid())
    try:
        with pytest.raises(OSError) as raised:
            _pidfd_send_signal(pidfd, 0, None, _UNSUPPORTED_PIDFD_FLAG)
    finally:
        os.close(pidfd)

    assert raised.value.errno == errno.EINVAL


def test_retained_leader_pidfd_signals_its_group_after_reaping(tmp_path: Path):
    ready = tmp_path / "member-ready"
    signaled = tmp_path / "member-signaled"
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER, _MEMBER, str(ready), str(signaled)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdin is not None
    assert leader.stdout is not None
    assert leader.stderr is not None
    pidfd = -1
    member_pid = -1
    member_pidfd = -1
    try:
        member_pid = int(leader.stdout.readline())
        assert int(_wait_for(ready)) == member_pid
        assert leader.poll() is None
        assert os.getpgid(leader.pid) == leader.pid
        assert os.getpgid(member_pid) == leader.pid

        # Capture the identity-bearing descriptor while attribution is certain.
        pidfd = _pidfd_open(leader.pid)
        support = _send_group_signal(pidfd, 0)
        if support == "unsupported":
            pytest.skip("kernel does not support PIDFD_SIGNAL_PROCESS_GROUP")
        assert support == "sent"

        # A member pidfd is not valid attribution for this operation. This also
        # distinguishes the group flag from ordinary per-process signaling.
        member_pidfd = _pidfd_open(member_pid)
        with pytest.raises(OSError) as misattributed:
            _pidfd_send_signal(
                member_pidfd,
                0,
                None,
                _PIDFD_SIGNAL_PROCESS_GROUP,
            )
        assert misattributed.value.errno == errno.ESRCH
        os.close(member_pidfd)
        member_pidfd = -1

        leader.stdin.write("x")
        leader.stdin.flush()
        assert leader.wait(timeout=10) == 0
        assert _alive(member_pid), "the group disappeared with its reaped leader"
        assert os.getpgid(member_pid) == leader.pid

        assert _send_group_signal(pidfd, signal.SIGUSR1) == "sent"
        assert int(_wait_for(signaled)) == member_pid

        deadline = time.monotonic() + 10
        while _send_group_signal(pidfd, 0) != "gone":
            assert time.monotonic() < deadline, "the empty group never returned ESRCH"
            time.sleep(0.01)
    finally:
        if member_pidfd >= 0:
            os.close(member_pidfd)
        if pidfd >= 0:
            os.close(pidfd)
            with pytest.raises(OSError) as closed:
                os.fstat(pidfd)
            assert closed.value.errno == errno.EBADF
        if leader.poll() is None:
            leader.kill()
            leader.wait(timeout=10)
        if member_pid >= 0 and _alive(member_pid):
            os.kill(member_pid, signal.SIGKILL)
