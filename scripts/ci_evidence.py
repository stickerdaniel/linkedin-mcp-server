"""Select the manual differential-evidence matrix.

One platform and one evidence group is one job. ``all`` expands that axis.
Anything else fails before writing a matrix, so a bad dispatch does not become
an empty run.
"""

from __future__ import annotations

import json
import os
import sys

PLATFORMS = (
    "ubuntu-latest",
    "ubuntu-24.04-arm",
    "macos-latest",
    "windows-latest",
)
GROUPS = (
    "native",
    "pipes",
    "processes",
    "idle",
    "owner",
    "commands",
    "auth",
    "eligibility",
)
_BROWSERS = {
    "ubuntu-latest": "~/.cache/ms-playwright",
    "ubuntu-24.04-arm": "~/.cache/ms-playwright",
    "macos-latest": "~/Library/Caches/ms-playwright",
    "windows-latest": "~\\AppData\\Local\\ms-playwright",
}


class CiEvidenceError(ValueError):
    """The dispatch asked for a platform or group this workflow does not run."""


def evidence_matrix(platform: str, group: str) -> list[dict[str, str]]:
    """Jobs for one dispatch. ``all`` on an axis includes every value of it."""
    if platform == "all":
        platforms = list(PLATFORMS)
    elif platform in PLATFORMS:
        platforms = [platform]
    else:
        raise CiEvidenceError(f"unknown platform {platform!r}")
    if group == "all":
        groups = list(GROUPS)
    elif group in GROUPS:
        groups = [group]
    else:
        raise CiEvidenceError(f"unknown group {group!r}")
    return [
        {"os": name, "rows": rows, "browsers": _BROWSERS[name]}
        for name in platforms
        for rows in groups
    ]


def _write_output(name: str, value: str) -> None:
    line = f"{name}={value}\n"
    destination = os.environ.get("GITHUB_OUTPUT")
    if not destination:
        sys.stdout.write(line)
        return
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(line)


def main() -> int:
    platform = os.environ.get("EVIDENCE_PLATFORM", "")
    group = os.environ.get("EVIDENCE_GROUP", "")
    try:
        jobs = evidence_matrix(platform, group)
    except CiEvidenceError as exc:
        print(f"ci evidence: {exc}", file=sys.stderr)
        return 1
    if not jobs:
        print("ci evidence: empty matrix", file=sys.stderr)
        return 1
    _write_output("include", json.dumps(jobs, separators=(",", ":")))
    print(f"ci evidence: {len(jobs)} jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
