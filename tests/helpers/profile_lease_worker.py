"""Child process used by the cross-process profile-lease tests.

Every concurrency test that existed before this one ran inside a single asyncio
event loop, which cannot observe the failure this lease exists to prevent: two
*operating-system processes* opening the same Chromium profile. These workers are
spawned with ``subprocess`` so the kernel, not asyncio, arbitrates.

Commands:

``hold <auth_root> <seconds>``
    Take the lease, write a marker line, hold it, release.

``critical <auth_root> <log> <rounds>``
    Repeatedly take the lease and write ``IN``/``OUT`` markers around a short
    sleep. Overlapping markers in *log* prove a mutual-exclusion failure.

``announce <auth_root> <seconds>``
    Announce interest without taking the lease, so a parent can assert the
    owner's handoff probe notices.

``probe <auth_root>``
    Print whether the lease is free and whether a handoff was requested.

``die-holding <auth_root>``
    Take the lease and exit via ``os._exit`` without releasing, proving the
    kernel frees it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from linkedin_mcp_server.profile_lease import ProfileLease  # noqa: E402


def _lease(auth_root: str) -> ProfileLease:
    return ProfileLease(Path(auth_root))


def _hold(auth_root: str, seconds: float) -> int:
    lease = _lease(auth_root)
    if not lease.try_acquire():
        print("BUSY", flush=True)
        return 1
    print("HELD", flush=True)
    time.sleep(seconds)
    lease.release()
    print("RELEASED", flush=True)
    return 0


def _critical(auth_root: str, log_path: str, rounds: int) -> int:
    lease = _lease(auth_root)
    tag = os.getpid()
    for _ in range(rounds):
        while not lease.try_acquire():
            time.sleep(0.005)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"IN{tag} ")
        time.sleep(0.01)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(f"OUT{tag} ")
        lease.release()
    return 0


def _announce(auth_root: str, seconds: float) -> int:
    """Hold a shared lock on the handoff file, reporting whether it was taken.

    Retries briefly first. An owner probing the same file exclusively holds it
    for a few microseconds at a time, and losing that race is transient and
    expected, unlike a platform whose shared locks do not coexist at all, which
    is the failure this reports.
    """
    lease = _lease(auth_root)
    deadline = time.monotonic() + 5
    while True:
        announcement = lease.announce()
        announcement.__enter__()
        # Reported rather than assumed: announcing degrades to a silent no-op
        # when the lock cannot be had, so a caller waiting only for "ANNOUNCED"
        # would be satisfied by a backend whose shared locks are really
        # exclusive, and every handoff test would pass proving nothing.
        if announcement.holds_lock:
            break
        announcement.__exit__(None, None, None)
        if time.monotonic() >= deadline:
            print("ANNOUNCE_FAILED", flush=True)
            return 1
        time.sleep(0.01)

    try:
        print("ANNOUNCED", flush=True)
        time.sleep(seconds)
    finally:
        announcement.__exit__(None, None, None)
    print("WITHDRAWN", flush=True)
    return 0


def _probe(auth_root: str) -> int:
    lease = _lease(auth_root)
    free = lease.try_acquire()
    if free:
        lease.release()
    print(f"free={free} handoff={lease.handoff_requested()}", flush=True)
    return 0


def _die_holding(auth_root: str) -> int:
    lease = _lease(auth_root)
    if not lease.try_acquire():
        print("BUSY", flush=True)
        return 1
    print("HELD", flush=True)
    sys.stdout.flush()
    os._exit(0)  # no unwinding, no release: only the kernel can free this


def main() -> int:
    command, *rest = sys.argv[1:]
    if command == "hold":
        return _hold(rest[0], float(rest[1]))
    if command == "critical":
        return _critical(rest[0], rest[1], int(rest[2]))
    if command == "announce":
        return _announce(rest[0], float(rest[1]))
    if command == "probe":
        return _probe(rest[0])
    if command == "die-holding":
        return _die_holding(rest[0])
    raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
