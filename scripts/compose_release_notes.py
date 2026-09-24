"""Compose a GitHub release body from CHANGELOG.md and the install template.

The body is the version's CHANGELOG section without its heading, then the
install instructions, then the compare link. Runs in the release workflow
before anything is built or published, so a missing section stops the release
while nothing exists yet that would have to be withdrawn.
"""

from __future__ import annotations

import argparse
import re
import string
from pathlib import Path

# The fragment directory's own documentation, which towncrier also skips.
_README = "README.md"
_INSTALL_HEADING = re.compile(r"^## Install or update$", re.MULTILINE)


class ReleaseNotesError(Exception):
    pass


def _section(changelog: str, version: str) -> str:
    build = f"uv run towncrier build --version {version} --yes"
    heading = re.compile(rf"## {re.escape(version)} \(")
    lines = changelog.splitlines()
    starts = [index for index, line in enumerate(lines) if heading.match(line)]
    if not starts:
        raise ReleaseNotesError(
            f"CHANGELOG.md has no section for {version}. "
            f"Run `{build}` in the version bump."
        )
    if len(starts) > 1:
        raise ReleaseNotesError(
            f"CHANGELOG.md has {len(starts)} sections for {version}. "
            f"Keep the one `{build}` wrote and fold the others into it."
        )
    start = starts[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = "\n".join(lines[start:end]).strip()
    if not section:
        raise ReleaseNotesError(
            f"CHANGELOG.md has an empty section for {version}. "
            f"Run `{build}` in the version bump."
        )
    return section


def _leftover_fragments(fragments_dir: Path) -> list[str]:
    if not fragments_dir.is_dir():
        return []
    return sorted(path.name for path in fragments_dir.iterdir() if path.name != _README)


def _install(template: str, version: str) -> str:
    if not _INSTALL_HEADING.search(template):
        raise ReleaseNotesError(
            "The release notes template has no `## Install or update` heading."
        )
    try:
        return string.Template(template).substitute(VERSION=version).strip()
    except (KeyError, ValueError) as error:
        raise ReleaseNotesError(
            f"The release notes template has a placeholder other than VERSION: {error}"
        ) from None


def compose(
    changelog: str,
    template: str,
    leftovers: list[str],
    version: str,
    previous_version: str,
    repository: str,
) -> str:
    if leftovers:
        raise ReleaseNotesError(
            "Fragments remain after the version bump: "
            f"{', '.join(leftovers)}. They arrived after `towncrier build` ran; "
            "fold them into the CHANGELOG section for "
            f"{version} and delete them."
        )
    section = _section(changelog, version)
    install = _install(template, version)
    compare = (
        f"**Full Changelog**: https://github.com/{repository}/compare/"
        f"v{previous_version}...v{version}"
    )
    return f"{section}\n\n{install}\n\n{compare}\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--fragments-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--previous-version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        body = compose(
            args.changelog.read_text(encoding="utf-8"),
            args.template.read_text(encoding="utf-8"),
            _leftover_fragments(args.fragments_dir),
            args.version,
            args.previous_version,
            args.repository,
        )
    except ReleaseNotesError as error:
        print(f"::error::{error}")
        return 1
    args.output.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
