"""Decide whether the required `test` check may pass.

Linux must succeed on every automatic run. A pull request also needs a
successful dependency review and a successful path decision. The optional
Windows and platform jobs are required only when that decision says so; on
main, and on a documentation-only pull request, they are required to have been
skipped. Any other result, including a missing or unknown decision, fails.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_REQUIRED = "required"
_SKIP = "skip"
_PASSING = frozenset({"success"})
_BROKEN = frozenset({"failure", "cancelled"})


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str


def _value(result: str | None) -> str:
    return result or ""


def evaluate_gate(
    *,
    event: str | None,
    linux: str | None,
    dependency_review: str | None,
    detection: str | None,
    matrices: str | None,
    windows: str | None,
    platform: str | None,
) -> GateResult:
    """Whether this automatic run's required check passes, and why not."""
    event = _value(event)
    linux = _value(linux)
    dependency_review = _value(dependency_review)
    detection = _value(detection)
    matrices = _value(matrices)
    windows = _value(windows)
    platform = _value(platform)

    if event not in {"pull_request", "push"}:
        return GateResult(False, "event")
    if linux not in _PASSING:
        return GateResult(False, "linux")
    if dependency_review in _BROKEN or (
        event == "pull_request" and dependency_review not in _PASSING
    ):
        return GateResult(False, "dependency-review")
    if detection in _BROKEN or (event == "pull_request" and detection not in _PASSING):
        return GateResult(False, "detection")

    if event == "pull_request":
        if matrices not in {_REQUIRED, _SKIP}:
            return GateResult(False, "detection")
        expected = "success" if matrices == _REQUIRED else "skipped"
    else:
        # Main does not repeat the optional matrices, whatever a detector said.
        expected = "skipped"

    if windows != expected:
        return GateResult(False, "windows-daemon")
    if platform != expected:
        return GateResult(False, "platform-behaviour")
    return GateResult(True, "ok")


def main() -> int:
    result = evaluate_gate(
        event=os.environ.get("EVENT_NAME"),
        linux=os.environ.get("LINUX_RESULT"),
        dependency_review=os.environ.get("DEPENDENCY_REVIEW_RESULT"),
        detection=os.environ.get("DETECTION_RESULT"),
        matrices=os.environ.get("MATRICES"),
        windows=os.environ.get("WINDOWS_DAEMON_RESULT"),
        platform=os.environ.get("PLATFORM_BEHAVIOUR_RESULT"),
    )
    if result.allowed:
        print("ci gate: ok")
        return 0
    print(f"ci gate: {result.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
