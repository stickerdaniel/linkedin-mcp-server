"""Compose a GitHub release body from CHANGELOG.md and the install template.

The body is the version's CHANGELOG section without its heading, with its
categories promoted to H2 and its bug fixes folded when other categories sit
beside them, then the install instructions, then the contributors, then the
compare link. Every pull request link gets its author's login, so GitHub
shows its Contributors block, and an author whose first merged pull request
is in this release is named as such. Runs in the release workflow before
anything is built or published, so a missing section stops the release while
nothing exists yet that would have to be withdrawn.
"""

from __future__ import annotations

import argparse
import json
import re
import string
import tomllib
from pathlib import Path

# The fragment directory's own documentation, which towncrier also skips.
_README = "README.md"
_INSTALL_HEADING = re.compile(r"^## Install or update$", re.MULTILINE)
_CATEGORY = "### "
_FIX_TYPE = "fix"
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(\[bot\])?")
_PR_NUMBER = re.compile(r"[0-9]+")
_BOT_SUFFIX = "[bot]"
_FIRST_CONTRIBUTION = " (first contribution 🎉)"


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


def _pr_link(repository: str) -> re.Pattern[str]:
    # Only links towncrier writes from issue_format; other URLs stay as they are.
    return re.compile(
        rf"\[#([0-9]+)\]\(https://github\.com/{re.escape(repository)}/pull/\1\)"
    )


def _pull_requests(section: str, repository: str) -> list[str]:
    """The linked pull request numbers, in order of first appearance."""
    return list(dict.fromkeys(_pr_link(repository).findall(section)))


def _fix_heading(pyproject: str) -> str:
    try:
        types = tomllib.loads(pyproject)["tool"]["towncrier"]["type"]
        [name] = [entry["name"] for entry in types if entry["directory"] == _FIX_TYPE]
    except (tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
        raise ReleaseNotesError(
            f"pyproject.toml has no single towncrier type with directory `{_FIX_TYPE}`."
        ) from None
    if not isinstance(name, str) or not name:
        raise ReleaseNotesError(
            f"The towncrier type with directory `{_FIX_TYPE}` has no name."
        )
    return name


def _authors(text: str) -> dict[str, str]:
    try:
        authors = json.loads(text)
    except json.JSONDecodeError:
        authors = None
    if not isinstance(authors, dict):
        raise ReleaseNotesError(
            "The pull request authors are not a JSON object of numbers to logins."
        )
    for number, login in authors.items():
        if not _PR_NUMBER.fullmatch(number):
            raise ReleaseNotesError(
                "The pull request authors have a key that is not a number."
            )
        # The login is not echoed: it could carry a workflow command.
        if not isinstance(login, str) or not _LOGIN.fullmatch(login):
            raise ReleaseNotesError(
                f"The author of #{number} is not a valid GitHub login."
            )
    return authors


def _first_contributors(text: str) -> list[str]:
    try:
        logins = json.loads(text)
    except json.JSONDecodeError:
        logins = None
    if not isinstance(logins, list):
        raise ReleaseNotesError(
            "The first contributors are not a JSON array of logins."
        )
    # The login is not echoed: it could carry a workflow command.
    if not all(isinstance(login, str) and _LOGIN.fullmatch(login) for login in logins):
        raise ReleaseNotesError("A first contributor is not a valid GitHub login.")
    return logins


def _owner(repository: str) -> str:
    # GitHub logins are case-insensitive.
    return repository.split("/", 1)[0].casefold()


def _marked(first: list[str], contributors: list[str], owner: str) -> set[str]:
    """The first contributors, checked against this section's own authors."""
    marked = {login.casefold() for login in first}
    if owner in marked:
        raise ReleaseNotesError(
            "The repository owner is listed as a first contributor."
        )
    if not marked <= {login.casefold() for login in contributors}:
        raise ReleaseNotesError(
            "A first contributor is not among this release's contributors."
        )
    return marked


def _credit(
    section: str, repository: str, authors: dict[str, str]
) -> tuple[str, list[str]]:
    """Name each external author after their link; return every non-bot author."""
    missing = [
        number
        for number in _pull_requests(section, repository)
        if number not in authors
    ]
    if missing:
        raise ReleaseNotesError(
            "The pull request authors have no entry for "
            f"{', '.join(f'#{number}' for number in missing)}."
        )
    owner = _owner(repository)
    contributors: list[str] = []

    def credit(link: re.Match[str]) -> str:
        login = authors[link[1]]
        if login.endswith(_BOT_SUFFIX):
            return link[0]
        if login not in contributors:
            contributors.append(login)
        if login.casefold() == owner:
            return link[0]
        return f"{link[0]} by @{login}"

    return _pr_link(repository).sub(credit, section), contributors


def _layout(section: str, fixes: str) -> str:
    """Promote the categories to H2 and fold the fixes beside other categories."""
    lines = section.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith(_CATEGORY)]
    if not starts:
        return section
    parts = ["\n".join(lines[: starts[0]]).strip()]
    categories = [
        (
            lines[start].removeprefix(_CATEGORY),
            "\n".join(lines[start + 1 : end]).strip(),
        )
        for start, end in zip(starts, [*starts[1:], len(lines)], strict=True)
    ]
    names = [name.strip() for name, _ in categories]
    fold = fixes in names and any(name != fixes for name in names)
    for name, body in categories:
        if fold and name.strip() == fixes:
            count = sum(line.startswith("- ") for line in body.splitlines())
            parts.append(
                f"<details>\n<summary><b>{fixes} ({count})</b></summary>\n\n"
                f"{body}\n\n</details>"
            )
        else:
            parts.append(f"## {name}\n\n{body}".rstrip())
    return "\n\n".join(part for part in parts if part)


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
    authors: dict[str, str],
    first_contributors: list[str],
    pyproject: str,
) -> str:
    if leftovers:
        raise ReleaseNotesError(
            "Fragments remain after the version bump: "
            f"{', '.join(leftovers)}. They arrived after `towncrier build` ran; "
            "fold them into the CHANGELOG section for "
            f"{version} and delete them."
        )
    owner = _owner(repository)
    section, contributors = _credit(_section(changelog, version), repository, authors)
    marked = _marked(first_contributors, contributors, owner)
    # The owner comes last; a stable sort keeps everyone else in order.
    contributors.sort(key=lambda login: login.casefold() == owner)
    parts = [_layout(section, _fix_heading(pyproject)), _install(template, version)]
    if contributors:
        parts.append(
            "**Contributors:** "
            + ", ".join(
                f"@{login}"
                + (_FIRST_CONTRIBUTION if login.casefold() in marked else "")
                for login in contributors
            )
        )
    parts.append(
        f"**Full Changelog**: https://github.com/{repository}/compare/"
        f"v{previous_version}...v{version}"
    )
    return "\n\n".join(parts) + "\n"


_COMPOSE_ARGS = (
    "template",
    "fragments_dir",
    "previous_version",
    "output",
    "pr_authors",
    "first_contributors",
    "pyproject",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-pull-requests",
        action="store_true",
        help="print the pull requests linked in the version's section and exit",
    )
    parser.add_argument("--changelog", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--fragments-dir", type=Path)
    parser.add_argument("--previous-version")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--pr-authors",
        type=Path,
        help="JSON object mapping pull request numbers to GitHub logins",
    )
    parser.add_argument(
        "--first-contributors",
        type=Path,
        help="JSON array of logins whose first merged pull request is in this release",
    )
    parser.add_argument("--pyproject", type=Path)
    args = parser.parse_args()
    if not args.list_pull_requests:
        missing = [
            f"--{name.replace('_', '-')}"
            for name in _COMPOSE_ARGS
            if getattr(args, name) is None
        ]
        if missing:
            parser.error(f"the following arguments are required: {', '.join(missing)}")
    return args


def main() -> int:
    args = _parse_args()
    try:
        changelog = args.changelog.read_text(encoding="utf-8")
        if args.list_pull_requests:
            for number in _pull_requests(
                _section(changelog, args.version), args.repository
            ):
                print(number)
            return 0
        body = compose(
            changelog,
            args.template.read_text(encoding="utf-8"),
            _leftover_fragments(args.fragments_dir),
            args.version,
            args.previous_version,
            args.repository,
            _authors(args.pr_authors.read_text(encoding="utf-8")),
            _first_contributors(args.first_contributors.read_text(encoding="utf-8")),
            args.pyproject.read_text(encoding="utf-8"),
        )
    except ReleaseNotesError as error:
        print(f"::error::{error}")
        return 1
    args.output.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
